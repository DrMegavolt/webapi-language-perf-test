"""LangPerf Python app: FastAPI + asyncpg, single worker, Prometheus metrics.

All statements are byte-identical to the canonical sql/queries.sql
(Q1 feed, Q2 single post, Q3 create post, Q4a-c like).
"""

import os
import re
import time
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgres://bench:bench@localhost:5432/bench"
)

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

# Metrics contract (SPEC.md): exact names, labels, buckets. Do not improvise.
REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds.",
    labelnames=("method", "route", "status"),
    buckets=(
        0.0005, 0.001, 0.002, 0.005, 0.01, 0.025, 0.05,
        0.1, 0.25, 0.5, 1, 2.5, 5, 10,
    ),
)
REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total count of HTTP requests.",
    labelnames=("method", "route", "status"),
)
APP_MEMORY_RSS = Gauge(
    "app_memory_rss_bytes",
    "Process resident set size (VmRSS from /proc/self/status), in bytes.",
)

UNRECORDED_ROUTES = frozenset({"/metrics", "/healthz"})
_PATH_PARAM_RE = re.compile(r"\{[^}]+\}")


def _route_label(scope: dict) -> str | None:
    """Map the FastAPI route template to the spec's route label, or None to skip."""
    path = getattr(scope.get("route"), "path", None)
    if not path or path in UNRECORDED_ROUTES:
        return None
    # "/posts/{post_id}/like" -> "/posts/:id/like", "/posts/{post_id}" -> "/posts/:id"
    return _PATH_PARAM_RE.sub(":id", path)


def _read_rss_bytes() -> float:
    """Current process RSS in bytes: /proc/self/status VmRSS line, kB x 1024."""
    try:
        with open("/proc/self/status", "rb") as f:
            for line in f:
                if line.startswith(b"VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


class MetricsMiddleware:
    """Pure ASGI middleware: one observation per request, after the response is sent."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = None

        async def send_wrapper(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = _route_label(scope)
            if route is not None and status is not None:
                labels = (scope["method"], route, str(status))
                REQUEST_DURATION.labels(*labels).observe(
                    time.perf_counter() - started
                )
                REQUESTS_TOTAL.labels(*labels).inc()
                APP_MEMORY_RSS.set(_read_rss_bytes())


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(
        DATABASE_URL, min_size=2, max_size=8
    )
    try:
        yield
    finally:
        await app.state.pool.close()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(MetricsMiddleware)


def _parse_id(raw: str) -> int | None:
    """Parse a path id; None when non-numeric (caller maps to 400)."""
    try:
        return int(raw)
    except ValueError:
        return None


def _bad_user_id() -> JSONResponse:
    return JSONResponse({"error": "invalid user_id"}, status_code=400)


# --- Canonical SQL: byte-identical to sql/queries.sql (Q1-Q4). ---

# Q1 (GET /feed?page=N): home feed, newest 20 posts with author + like count
# CTE form: pick 20 posts via index BEFORE joining/counting likes
FEED_SQL = """
WITH feed AS (
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
ORDER BY f.created_at DESC, f.id DESC;
"""

# Q2 (GET /posts/:id): single post with author + like count
SINGLE_POST_SQL = """
SELECT p.id, p.user_id, u.username, p.content, p.created_at,
       COUNT(l.id)::bigint AS like_count
FROM posts p
JOIN users u ON u.id = p.user_id
LEFT JOIN likes l ON l.post_id = p.id
WHERE p.id = $1
GROUP BY p.id, p.user_id, u.username, p.content, p.created_at;
"""

# Q3 (POST /posts): create post. Response = RETURNING row + "like_count": 0
CREATE_POST_SQL = """
INSERT INTO posts (user_id, content, created_at)
VALUES ($1, $2, now())
RETURNING id, user_id, content, created_at;
"""

# Q4a: missing post -> 404 {"error":"post not found"}
LIKE_CHECK_POST_SQL = "SELECT 1 FROM posts WHERE id = $1;"

# Q4b: idempotent insert
LIKE_INSERT_SQL = """
INSERT INTO likes (post_id, user_id, created_at)
VALUES ($1, $2, now())
ON CONFLICT (post_id, user_id) DO NOTHING;
"""

# Q4c: fresh count for the response
LIKE_COUNT_SQL = "SELECT COUNT(*)::bigint AS like_count FROM likes WHERE post_id = $1;"


@app.get("/feed")
async def get_feed(request: Request, page: int = 1):
    if page < 1:
        page = 1
    rows = await request.app.state.pool.fetch(FEED_SQL, page)
    posts = [
        {
            "id": row["id"],
            "user_id": row["user_id"],
            "username": row["username"],
            "content": row["content"],
            "like_count": row["like_count"],
            "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]
    return JSONResponse({"page": page, "posts": posts})


@app.get("/posts/{post_id}")
async def get_post(request: Request, post_id: str):
    post_id_val = _parse_id(post_id)
    if post_id_val is None:
        return JSONResponse({"error": "invalid post id"}, status_code=400)
    if not INT64_MIN <= post_id_val <= INT64_MAX:
        return JSONResponse({"error": "post not found"}, status_code=404)
    row = await request.app.state.pool.fetchrow(SINGLE_POST_SQL, post_id_val)
    if row is None:
        return JSONResponse({"error": "post not found"}, status_code=404)
    return JSONResponse(
        {
            "id": row["id"],
            "user_id": row["user_id"],
            "username": row["username"],
            "content": row["content"],
            "like_count": row["like_count"],
            "created_at": row["created_at"].isoformat(),
        }
    )


@app.post("/posts")
async def create_post(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        body = {}
    user_id = body.get("user_id")
    content = body.get("content")
    if (
        isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or not INT64_MIN <= user_id <= INT64_MAX
    ):
        return _bad_user_id()
    if not isinstance(content, str):
        return JSONResponse({"error": "invalid content"}, status_code=400)
    try:
        row = await request.app.state.pool.fetchrow(
            CREATE_POST_SQL, user_id, content
        )
    except asyncpg.ForeignKeyViolationError:
        return _bad_user_id()
    return JSONResponse(
        {
            "id": row["id"],
            "user_id": row["user_id"],
            "content": row["content"],
            "created_at": row["created_at"].isoformat(),
            "like_count": 0,
        },
        status_code=201,
    )


@app.post("/posts/{post_id}/like")
async def like_post(request: Request, post_id: str):
    post_id_val = _parse_id(post_id)
    if post_id_val is None:
        return JSONResponse({"error": "invalid post id"}, status_code=400)
    if not INT64_MIN <= post_id_val <= INT64_MAX:
        return JSONResponse({"error": "post not found"}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        body = {}
    user_id = body.get("user_id")
    if (
        isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or not INT64_MIN <= user_id <= INT64_MAX
    ):
        return _bad_user_id()

    async with request.app.state.pool.acquire() as conn:
        # Q4a
        exists = await conn.fetchval(LIKE_CHECK_POST_SQL, post_id_val)
        if exists is None:
            return JSONResponse({"error": "post not found"}, status_code=404)
        # Q4b
        try:
            await conn.execute(LIKE_INSERT_SQL, post_id_val, user_id)
        except asyncpg.ForeignKeyViolationError:
            return _bad_user_id()
        # Q4c
        like_count = await conn.fetchval(LIKE_COUNT_SQL, post_id_val)
    return JSONResponse({"post_id": post_id_val, "like_count": like_count})


@app.get("/healthz")
async def healthz(request: Request):
    try:
        await request.app.state.pool.fetchval("SELECT 1")
    except Exception:
        return JSONResponse({"status": "error"}, status_code=503)
    return JSONResponse({"status": "ok"})


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
