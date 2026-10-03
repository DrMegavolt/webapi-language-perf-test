'use strict';

// langperf — Express 5 + pg (node-postgres) implementation.
// Canonical SQL lives in sql/queries.sql at the repo root; the statements below
// must stay byte-identical to Q1, Q2, Q3 and Q4a-c (parameter binding only).

const fs = require('node:fs');
const express = require('express');
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
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------
const app = express();
app.disable('x-powered-by');
app.disable('etag');

// Wall-clock start around the full request handling (before body parsing).
app.use((req, res, next) => {
  req._startAt = process.hrtime.bigint();
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

  const { rows } = await pool.query(
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
  );
  res.status(200).json({ page, posts: rows });
  next();
});

// Q2 (GET /posts/:id): single post with author + like count
app.get('/posts/:id', async (req, res, next) => {
  const id = parsePostId(req.params.id);
  if (id === undefined) return invalidPostId(res, next);

  const { rows } = await pool.query(
    `SELECT p.id, p.user_id, u.username, p.content, p.created_at,
            COUNT(l.id)::bigint AS like_count
     FROM posts p
     JOIN users u ON u.id = p.user_id
     LEFT JOIN likes l ON l.post_id = p.id
     WHERE p.id = $1
     GROUP BY p.id, p.user_id, u.username, p.content, p.created_at`,
    [id],
  );
  if (rows.length === 0) return postNotFound(res, next);
  res.status(200).json(rows[0]);
  next();
});

// Q3 (POST /posts): create post — exactly one statement, no pre-check.
app.post('/posts', async (req, res, next) => {
  const body = req.body || {};
  try {
    const { rows } = await pool.query(
      `INSERT INTO posts (user_id, content, created_at)
       VALUES ($1, $2, now())
       RETURNING id, user_id, content, created_at`,
      [body.user_id, body.content],
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
  const post = await pool.query('SELECT 1 FROM posts WHERE id = $1', [id]);
  if (post.rowCount === 0) return postNotFound(res, next);

  // 4b: idempotent insert
  await pool.query(
    `INSERT INTO likes (post_id, user_id, created_at)
     VALUES ($1, $2, now())
     ON CONFLICT (post_id, user_id) DO NOTHING`,
    [id, (req.body || {}).user_id],
  );

  // 4c: fresh count for the response
  const count = await pool.query(
    'SELECT COUNT(*)::bigint AS like_count FROM likes WHERE post_id = $1',
    [id],
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
  console.log(`langperf-express listening on 0.0.0.0:${PORT}`);
});
