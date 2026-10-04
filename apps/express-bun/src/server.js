'use strict';

// langperf — Express 5 + pg (node-postgres) implementation, run on the Bun
// runtime. Canonical SQL lives in sql/queries.sql at the repo root; the
// statements below must stay byte-identical to Q1, Q2, Q3 and Q4a-c
// (parameter binding only).

const fs = require('node:fs');
const express = require('express');
const otelApi = require('@opentelemetry/api');
const {
  BasicTracerProvider,
  BatchSpanProcessor,
  AlwaysOnSampler,
} = require('@opentelemetry/sdk-trace-base');
const { resourceFromAttributes } = require('@opentelemetry/resources');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-http');
const promClient = require('prom-client');
const pg = require('pg');

// pg returns int8 (BIGSERIAL ids, COUNT(*)::bigint) as strings; parse them to
// JS numbers so every numeric field is a JSON number (ids/counts are small).
pg.types.setTypeParser(20, (v) => parseInt(v, 10));

const PORT = Number(process.env.PORT) || 8080;
const DATABASE_URL =
  process.env.DATABASE_URL || 'postgres://bench:bench@127.0.0.1:5432/bench';

// Pool cap: POOL_SIZE env var, default 8 (per SPEC); non-numeric → 8.
const poolSize = Number.parseInt(process.env.POOL_SIZE, 10) || 8;

const pool = new pg.Pool({
  connectionString: DATABASE_URL,
  max: poolSize,
});

// ---------------------------------------------------------------------------
// Tracing (OTLP/HTTP, contract): one fresh SERVER root span per API request,
// one CLIENT child span per SQL statement. No context extraction is performed,
// so incoming trace headers are never propagated — roots are always fresh.
// Base endpoint from env; spans are POSTed to <endpoint>/v1/traces in batches
// every 500ms (prompt flush), 100% sampled. Spans use explicit parent
// contexts, so no global context manager is registered.
// ---------------------------------------------------------------------------
const OTEL_EXPORTER_ENDPOINT = (
  process.env.OTEL_EXPORTER_OTLP_ENDPOINT ||
  'http://otel-gateway-collector.observability.svc.cluster.local:4318'
).replace(/\/+$/, '');

const tracerProvider = new BasicTracerProvider({
  resource: resourceFromAttributes({ 'service.name': 'langperf-express-bun' }),
  sampler: new AlwaysOnSampler(), // 100% sampling
  spanProcessors: [
    new BatchSpanProcessor(
      new OTLPTraceExporter({ url: `${OTEL_EXPORTER_ENDPOINT}/v1/traces` }),
      { scheduledDelayMillis: 500 },
    ),
  ],
});
const tracer = tracerProvider.getTracer('langperf.express-bun');

// ---------------------------------------------------------------------------
// Metrics (SPEC.md "Metrics contract")
// ---------------------------------------------------------------------------
const register = new promClient.Registry();

const httpDuration = new promClient.Histogram({
  name: 'http_request_duration_seconds',
  help: 'Duration of HTTP requests in seconds.',
  labelNames: ['method', 'route', 'status'],
  buckets: [0.0005, 0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10],
});

const httpRequestsTotal = new promClient.Counter({
  name: 'http_requests_total',
  help: 'Total number of HTTP requests.',
  labelNames: ['method', 'route', 'status'],
});

const appMemoryRssBytes = new promClient.Gauge({
  name: 'app_memory_rss_bytes',
  help: 'Resident memory of the process in bytes (VmRSS from /proc/self/status).',
});

register.registerMetric(httpDuration);
register.registerMetric(httpRequestsTotal);
register.registerMetric(appMemoryRssBytes);

function readRssBytes() {
  try {
    const status = fs.readFileSync('/proc/self/status', 'utf8');
    const m = /VmRSS:\s+(\d+)\s+kB/.exec(status);
    if (m) return Number(m[1]) * 1024;
  } catch {
    // No /proc (e.g. running on macOS) — fall back to process.memoryUsage().
  }
  return process.memoryUsage().rss;
}

// route label values are the *pattern*, exactly (SPEC.md)
const ROUTE_LABELS = {
  '/feed': '/feed',
  '/posts': '/posts',
  '/posts/:id': '/posts/:id',
  '/posts/:id/like': '/posts/:id/like',
};

function normalizeRoute(rawPath) {
  if (typeof rawPath === 'string' && ROUTE_LABELS[rawPath]) return ROUTE_LABELS[rawPath];
  if (typeof rawPath === 'string') {
    // Express 5 keeps the pattern string for string routes; fall back in case
    // it ever hands us a path-to-regexp source instead.
    if (/^\/posts\/[^/]+$/.test(rawPath)) return '/posts/:id';
    if (/^\/posts\/[^/]+\/like$/.test(rawPath)) return '/posts/:id/like';
  }
  return undefined;
}

// Pre-routing span name: normalize the raw URL path to one of the 4 span
// routes. Covers exactly the four API routes; /healthz and /metrics (and
// anything unmatched) get no spans. The authoritative req.route.path is
// re-checked when the span is closed (see recordMetrics).
function routeFromPath(path) {
  if (path === '/feed' || path === '/posts') return path;
  if (/^\/posts\/[^/]+$/.test(path)) return '/posts/:id';
  if (/^\/posts\/[^/]+\/like$/.test(path)) return '/posts/:id/like';
  return undefined;
}

// CLIENT span around one DB round-trip (wall time start -> end), parented
// explicitly on the request's SERVER span. Returns the query's promise.
function withDbSpan(parentSpan, name, run) {
  const parentContext = parentSpan
    ? otelApi.trace.setSpan(otelApi.context.active(), parentSpan)
    : undefined;
  const span = tracer.startSpan(
    name,
    { kind: otelApi.SpanKind.CLIENT, attributes: { 'db.system': 'postgresql' } },
    parentContext,
  );
  let result;
  try {
    result = run();
  } catch (err) {
    span.recordException(err);
    span.end();
    throw err;
  }
  return Promise.resolve(result).then(
    (value) => {
      span.end();
      return value;
    },
    (err) => {
      span.recordException(err);
      span.end();
      throw err;
    },
  );
}

function recordMetrics(req, res) {
  if (req._langperfRecorded) return; // observe each request exactly once
  const route = normalizeRoute(req.route && req.route.path);
  if (!route) return; // only the four API endpoints are recorded
  req._langperfRecorded = true;
  const elapsedSeconds = Number(process.hrtime.bigint() - req._startAt) / 1e9;
  const labels = { method: req.method, route, status: String(res.statusCode) };
  httpDuration.observe(labels, elapsedSeconds);
  httpRequestsTotal.inc(labels);
  appMemoryRssBytes.set(readRssBytes());
  // Close the request's SERVER span here: recordMetrics is invoked by the
  // post-route middleware AND by the error handler, so both paths end it.
  if (req._otelSpan) {
    req._otelSpan.updateName(`HTTP ${req.method} ${route}`);
    req._otelSpan.setAttribute('http.route', route);
    req._otelSpan.end();
    req._otelSpan = undefined;
  }
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------
const app = express();
app.disable('x-powered-by');
app.disable('etag');

// Wall-clock start around the full request handling (before body parsing).
// Tracing: open the fresh SERVER root span here (pre-route); the route comes
// from the raw path, the authoritative pattern is set at span close. Span is
// ended in recordMetrics, after the response has been sent.
app.use((req, res, next) => {
  req._startAt = process.hrtime.bigint();
  const route = routeFromPath(req.path);
  if (route) {
    req._otelSpan = tracer.startSpan(`HTTP ${req.method} ${route}`, {
      kind: otelApi.SpanKind.SERVER,
      attributes: { 'http.method': req.method, 'http.route': route },
    });
  }
  next();
});

app.use(express.json());

function parsePostId(raw) {
  if (typeof raw === 'string' && /^\d+$/.test(raw)) {
    const n = Number(raw);
    if (Number.isSafeInteger(n) && n > 0) return n;
  }
  return undefined;
}

function invalidPostId(res, next) {
  res.status(400).json({ error: 'invalid post id' });
  next();
}

function postNotFound(res, next) {
  res.status(404).json({ error: 'post not found' });
  next();
}

// Q1 (GET /feed?page=N): home feed, newest 20 posts with author + like count
app.get('/feed', async (req, res, next) => {
  let page = Number(req.query.page === undefined ? 1 : req.query.page);
  if (!Number.isFinite(page) || page < 1) page = 1;
  page = Math.floor(page);

  const { rows } = await withDbSpan(req._otelSpan, 'DB Q1 feed', () =>
    pool.query(
      `WITH feed AS (
       SELECT p.id, p.user_id, p.content, p.created_at
       FROM posts p
       ORDER BY p.created_at DESC, p.id DESC
       LIMIT 20 OFFSET ($1 - 1) * 20
     )
     SELECT f.id, f.user_id, u.username, f.content, f.created_at,
            COUNT(l.id)::bigint AS like_count
     FROM feed f
     JOIN users u ON u.id = f.user_id
     LEFT JOIN likes l ON l.post_id = f.id
     GROUP BY f.id, f.user_id, u.username, f.content, f.created_at
     ORDER BY f.created_at DESC, f.id DESC`,
      [page],
    ),
  );
  res.status(200).json({ page, posts: rows });
  next();
});

// Q2 (GET /posts/:id): single post with author + like count
app.get('/posts/:id', async (req, res, next) => {
  const id = parsePostId(req.params.id);
  if (id === undefined) return invalidPostId(res, next);

  const { rows } = await withDbSpan(req._otelSpan, 'DB Q2 single post', () =>
    pool.query(
      `SELECT p.id, p.user_id, u.username, p.content, p.created_at,
            COUNT(l.id)::bigint AS like_count
     FROM posts p
     JOIN users u ON u.id = p.user_id
     LEFT JOIN likes l ON l.post_id = p.id
     WHERE p.id = $1
     GROUP BY p.id, p.user_id, u.username, p.content, p.created_at`,
      [id],
    ),
  );
  if (rows.length === 0) return postNotFound(res, next);
  res.status(200).json(rows[0]);
  next();
});

// Q3 (POST /posts): create post — exactly one statement, no pre-check.
app.post('/posts', async (req, res, next) => {
  const body = req.body || {};
  try {
    const { rows } = await withDbSpan(req._otelSpan, 'DB Q3 create post', () =>
      pool.query(
        `INSERT INTO posts (user_id, content, created_at)
       VALUES ($1, $2, now())
       RETURNING id, user_id, content, created_at`,
        [body.user_id, body.content],
      ),
    );
    const row = rows[0];
    res.status(201).json({
      id: row.id,
      user_id: row.user_id,
      content: row.content,
      created_at: row.created_at,
      like_count: 0, // computed in the app, never queried
    });
    next();
  } catch (err) {
    // A missing/unknown user_id surfaces as FK (23503) or NOT NULL (23502).
    if (err.code === '23503' || (err.code === '23502' && err.column === 'user_id')) {
      res.status(400).json({ error: 'invalid user_id' });
      return next();
    }
    throw err;
  }
});

// Q4 (POST /posts/:id/like): exactly these three statements in this order.
app.post('/posts/:id/like', async (req, res, next) => {
  const id = parsePostId(req.params.id);
  if (id === undefined) return invalidPostId(res, next);

  // 4a: missing post -> 404 {"error":"post not found"}
  const post = await withDbSpan(req._otelSpan, 'DB Q4a post exists', () =>
    pool.query('SELECT 1 FROM posts WHERE id = $1', [id]),
  );
  if (post.rowCount === 0) return postNotFound(res, next);

  // 4b: idempotent insert
  await withDbSpan(req._otelSpan, 'DB Q4b insert like', () =>
    pool.query(
      `INSERT INTO likes (post_id, user_id, created_at)
     VALUES ($1, $2, now())
     ON CONFLICT (post_id, user_id) DO NOTHING`,
      [id, (req.body || {}).user_id],
    ),
  );

  // 4c: fresh count for the response
  const count = await withDbSpan(req._otelSpan, 'DB Q4c like count', () =>
    pool.query(
      'SELECT COUNT(*)::bigint AS like_count FROM likes WHERE post_id = $1',
      [id],
    ),
  );

  res.status(200).json({ post_id: id, like_count: count.rows[0].like_count });
  next();
});

app.get('/healthz', async (req, res) => {
  await pool.query('SELECT 1');
  res.status(200).json({ status: 'ok' });
});

app.get('/metrics', async (req, res) => {
  res.set('Content-Type', register.contentType);
  res.end(await register.metrics());
});

// Post-route metrics middleware: reached only by handlers that call next()
// after sending the response, so it only fires for matched API routes and can
// read req.route.path. /metrics and /healthz never reach it (no next()).
app.use((req, res, next) => {
  recordMetrics(req, res);
  next();
});

// Error handler (Express 5 forwards async handler rejections here).
// eslint-disable-next-line no-unused-vars
app.use((err, req, res, next) => {
  const status = Number(err && (err.status || err.statusCode)) || 500;
  if (!res.headersSent) {
    res.status(status).json({ error: status === 500 ? 'internal error' : 'bad request' });
  }
  recordMetrics(req, res); // covers 500s; no-op if no known route matched
});

app.listen(PORT, '0.0.0.0', () => {
  console.log(`langperf-express-bun listening on 0.0.0.0:${PORT}`);
});
