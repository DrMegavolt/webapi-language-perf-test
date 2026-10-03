// langperf/bun — Bun native HTTP (Bun.serve) + Bun's built-in SQL client (Postgres).
// Zero npm dependencies. Plain JS, no build step.
//
// Contract: SPEC.md + sql/queries.sql (canonical statements, binding).
// - Pool capped at 8 (Bun SQL `max` option).
// - Metrics: hand-rolled Prometheus text exposition with the exact names,
//   labels and buckets from the spec. /metrics and /healthz are not recorded.
// - app_memory_rss_bytes is refreshed on every recorded request from
//   /proc/self/status VmRSS (kB) * 1024.

import { SQL } from "bun";
import { readFileSync } from "node:fs";

const PORT = Number(process.env.PORT || 8080);

// ---------------------------------------------------------------------------
// Canonical SQL — sql/queries.sql is binding. Statement text, joins and casts
// are kept identical. Only the substitution explicitly allowed by the spec is
// used: offset = (page - 1) * 20 is computed in the app and bound as one
// parameter ("LIMIT 20 OFFSET $1").
// ---------------------------------------------------------------------------

// Q1 (GET /feed?page=N)
const Q1_FEED = `
WITH feed AS (
  SELECT p.id, p.user_id, p.content, p.created_at
  FROM posts p
  ORDER BY p.created_at DESC, p.id DESC
  LIMIT 20 OFFSET $1
)
SELECT f.id, f.user_id, u.username, f.content, f.created_at,
       COUNT(l.id)::bigint AS like_count
FROM feed f
JOIN users u ON u.id = f.user_id
LEFT JOIN likes l ON l.post_id = f.id
GROUP BY f.id, f.user_id, u.username, f.content, f.created_at
ORDER BY f.created_at DESC, f.id DESC`;

// Q2 (GET /posts/:id)
const Q2_POST = `
SELECT p.id, p.user_id, u.username, p.content, p.created_at,
       COUNT(l.id)::bigint AS like_count
FROM posts p
JOIN users u ON u.id = p.user_id
LEFT JOIN likes l ON l.post_id = p.id
WHERE p.id = $1
GROUP BY p.id, p.user_id, u.username, p.content, p.created_at`;

// Q3 (POST /posts) — exactly one statement; like_count: 0 is computed in the app.
const Q3_INSERT = `
INSERT INTO posts (user_id, content, created_at)
VALUES ($1, $2, now())
RETURNING id, user_id, content, created_at`;

// Q4 (POST /posts/:id/like) — exactly these three statements, in this order.
const Q4A_EXISTS = `SELECT 1 FROM posts WHERE id = $1`;

const Q4B_LIKE = `
INSERT INTO likes (post_id, user_id, created_at)
VALUES ($1, $2, now())
ON CONFLICT (post_id, user_id) DO NOTHING`;

const Q4C_COUNT = `SELECT COUNT(*)::bigint AS like_count FROM likes WHERE post_id = $1`;

// ---------------------------------------------------------------------------
// DB client — one pooled client, max 8 connections (same as every stack).
// ---------------------------------------------------------------------------
const sql = new SQL({
  url: process.env.DATABASE_URL || "postgres://localhost:5432/bench",
  max: 8,
  idleTimeout: 30,
  maxLifetime: 0,
});

// ---------------------------------------------------------------------------
// Metrics (hand-rolled Prometheus exposition — exact names/labels/buckets).
// Bun.serve is single-threaded, so no locking is needed for these maps.
// ---------------------------------------------------------------------------
const BUCKETS = [0.0005, 0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10];
const HIST = new Map(); // "method|route|status" -> { obs, count, sum }
let rssBytes = 0;

function readRssBytes() {
  try {
    const status = readFileSync("/proc/self/status", "utf8");
    const m = /VmRSS:\s+(\d+)\s+kB/.exec(status);
    if (m) return Number(m[1]) * 1024;
  } catch {
    // /proc not available (non-Linux dev); fall back to Bun's own RSS number.
  }
  return process.memoryUsage().rss;
}

function observe(method, route, status, seconds) {
  const key = `${method}|${route}|${status}`;
  let s = HIST.get(key);
  if (s === undefined) {
    s = { obs: new Float64Array(BUCKETS.length + 1), count: 0, sum: 0 };
    HIST.set(key, s);
  }
  let i = 0;
  while (i < BUCKETS.length && seconds > BUCKETS[i]) i++;
  s.obs[i] += 1;
  s.count += 1;
  s.sum += seconds;
  rssBytes = readRssBytes();
}

function metricsText() {
  const lines = [];
  lines.push("# HELP http_request_duration_seconds HTTP request duration in seconds.");
  lines.push("# TYPE http_request_duration_seconds histogram");
  for (const [key, s] of HIST) {
    const [method, route, status] = key.split("|");
    const base = `method="${method}",route="${route}",status="${status}"`;
    let cum = 0;
    for (let i = 0; i < BUCKETS.length; i++) {
      cum += s.obs[i];
      lines.push(`http_request_duration_seconds_bucket{${base},le="${BUCKETS[i]}"} ${cum}`);
    }
    cum += s.obs[BUCKETS.length];
    lines.push(`http_request_duration_seconds_bucket{${base},le="+Inf"} ${cum}`);
    lines.push(`http_request_duration_seconds_sum{${base}} ${s.sum}`);
    lines.push(`http_request_duration_seconds_count{${base}} ${s.count}`);
  }
  lines.push("# HELP http_requests_total Total HTTP requests.");
  lines.push("# TYPE http_requests_total counter");
  for (const [key, s] of HIST) {
    const [method, route, status] = key.split("|");
    lines.push(`http_requests_total{method="${method}",route="${route}",status="${status}"} ${s.count}`);
  }
  lines.push("# HELP app_memory_rss_bytes Process resident set size in bytes (VmRSS).");
  lines.push("# TYPE app_memory_rss_bytes gauge");
  lines.push(`app_memory_rss_bytes ${rssBytes}`);
  return lines.join("\n") + "\n";
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function jsonResponse(status, obj) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function iso(value) {
  return value instanceof Date ? value.toISOString() : String(value);
}

// int8 columns arrive as JS numbers (when safe) or strings; counts must be
// JSON numbers, so normalize everything through Number().
function toNum(v) {
  return typeof v === "number" ? v : Number(v);
}

function postItem(r) {
  return {
    id: toNum(r.id),
    user_id: toNum(r.user_id),
    username: r.username,
    content: r.content,
    like_count: toNum(r.like_count),
    created_at: iso(r.created_at),
  };
}

function pgCode(e) {
  return typeof e?.code === "string" && /^\d{5}$/.test(e.code) ? e.code : "";
}

function isForeignKeyViolation(e) {
  return pgCode(e) === "23503" || (e?.message ?? "").includes("violates foreign key constraint");
}

function isConstraintInsertFailure(e) {
  // NOT NULL (23502) / FK (23503) on the INSERT — both mean the payload
  // referenced an invalid user (spec maps these to 400 invalid user_id).
  return isForeignKeyViolation(e) || pgCode(e) === "23502";
}

const ID_RE = /^\d+$/;

async function readJsonBody(req) {
  try {
    const body = await req.json();
    return body !== null && typeof body === "object" ? body : null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------
async function handleFeed(url) {
  const raw = url.searchParams.get("page");
  const n = raw === null ? 1 : Number(raw);
  const page = Number.isFinite(n) && n >= 1 ? Math.floor(n) : 1;
  const offset = (page - 1) * 20;
  const rows = await sql.unsafe(Q1_FEED, [offset]);
  return jsonResponse(200, { page, posts: rows.map(postItem) });
}

async function handleGetPost(id) {
  const rows = await sql.unsafe(Q2_POST, [id]);
  if (rows.length === 0) return jsonResponse(404, { error: "post not found" });
  return jsonResponse(200, postItem(rows[0]));
}

async function handleCreatePost(req) {
  const body = await readJsonBody(req);
  if (body === null) return jsonResponse(400, { error: "invalid body" });
  if (!Number.isInteger(body.user_id)) return jsonResponse(400, { error: "invalid user_id" });
  if (typeof body.content !== "string") return jsonResponse(400, { error: "invalid body" });

  let rows;
  try {
    rows = await sql.unsafe(Q3_INSERT, [body.user_id, body.content]);
  } catch (e) {
    if (isConstraintInsertFailure(e)) return jsonResponse(400, { error: "invalid user_id" });
    throw e;
  }
  const r = rows[0];
  return jsonResponse(201, {
    id: toNum(r.id),
    user_id: toNum(r.user_id),
    content: r.content,
    created_at: iso(r.created_at),
    like_count: 0,
  });
}

async function handleLike(id, req) {
  const body = await readJsonBody(req);
  if (body === null || !Number.isInteger(body.user_id)) {
    return jsonResponse(400, { error: "invalid user_id" });
  }

  // Q4a: post must exist
  const exists = await sql.unsafe(Q4A_EXISTS, [id]);
  if (exists.length === 0) return jsonResponse(404, { error: "post not found" });

  // Q4b: idempotent insert
  try {
    await sql.unsafe(Q4B_LIKE, [id, body.user_id]);
  } catch (e) {
    if (isConstraintInsertFailure(e)) return jsonResponse(400, { error: "invalid user_id" });
    throw e;
  }

  // Q4c: fresh count
  const counted = await sql.unsafe(Q4C_COUNT, [id]);
  return jsonResponse(200, { post_id: toNum(id), like_count: toNum(counted[0].like_count) });
}

// ---------------------------------------------------------------------------
// HTTP server — manual routing, no framework.
// ---------------------------------------------------------------------------
Bun.serve({
  port: PORT,
  hostname: "0.0.0.0",
  development: false,
  idleTimeout: 255,
  async fetch(req) {
    const url = new URL(req.url);
    const path = url.pathname;
    const method = req.method;

    // Not recorded in metrics.
    if (path === "/healthz" && method === "GET") {
      try {
        await sql`SELECT 1`;
      } catch {
        return jsonResponse(500, { error: "database unavailable" });
      }
      return jsonResponse(200, { status: "ok" });
    }
    if (path === "/metrics" && method === "GET") {
      return new Response(metricsText(), {
        status: 200,
        headers: { "Content-Type": "text/plain; version=0.0.4; charset=utf-8" },
      });
    }

    // Classify the canonical routes (route label = pattern, exactly as spec'd).
    let route = null;
    let idPart = null;
    if (path === "/feed" && method === "GET") {
      route = "/feed";
    } else if (path === "/posts" && method === "POST") {
      route = "/posts";
    } else if (path.startsWith("/posts/")) {
      const rest = path.slice("/posts/".length);
      if (method === "POST" && rest.endsWith("/like") && rest.length > "/like".length) {
        route = "/posts/:id/like";
        idPart = rest.slice(0, -"/like".length);
      } else if (method === "GET" && !rest.includes("/") && rest !== "") {
        route = "/posts/:id";
        idPart = rest;
      }
    }
    if (route === null) return jsonResponse(404, { error: "not found" });

    const start = performance.now();
    let res;
    try {
      if (idPart !== null && !ID_RE.test(idPart)) {
        // Non-numeric :id -> 400, never reaches the DB.
        res = jsonResponse(400, { error: "invalid post id" });
      } else if (route === "/feed") {
        res = await handleFeed(url);
      } else if (route === "/posts") {
        res = await handleCreatePost(req);
      } else if (route === "/posts/:id") {
        res = await handleGetPost(Number(idPart));
      } else {
        res = await handleLike(Number(idPart), req);
      }
    } catch {
      res = jsonResponse(500, { error: "internal error" });
    }
    // Observe exactly once, wall time around the full request handling
    // (includes body parse + DB time). /metrics and /healthz never get here.
    observe(method, route, String(res.status), (performance.now() - start) / 1000);
    return res;
  },
});
