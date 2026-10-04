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
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import SpanKind
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

# OTLP/HTTP trace export (contract): base endpoint from env, spans POSTed to
# <endpoint>/v1/traces in batches every 500ms, 100% sampled. No context
# extraction is ever performed, so every request span is a fresh root.
OTEL_ENDPOINT = os.environ.get(
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "http://otel-gateway-collector.observability.svc.cluster.local:4318",
).rstrip("/")

_tracer_provider = TracerProvider(
    resource=Resource.create({"service.name": "langperf-python"}),
    sampler=ALWAYS_ON,
)
_tracer_provider.add_span_processor(
    BatchSpanProcessor(
        OTLPSpanExporter(endpoint=f"{OTEL_ENDPOINT}/v1/traces"),
        schedule_delay_millis=500,
    )
)
trace.set_tracer_provider(_tracer_provider)
tracer = trace.get_tracer("langperf.python")

# Pool cap: POOL_SIZE env var, default 8 (per SPEC); non-numeric → 8.
try:
    POOL_SIZE = int(os.environ["POOL_SIZE"])
except (KeyError, ValueError):
    POOL_SIZE = 8

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
_PATH_ID_RE = re.compile(r"^/posts/[^/]+$")
_PATH_LIKE_RE = re.compile(r"^/posts/[^/]+/like$")


def _route_label(scope: dict) -> str | None:
    """Map the FastAPI route template to the spec's route label, or None to skip."""
    path = getattr(scope.get("route"), "path", None)
    if not path or path in UNRECORDED_ROUTES:
        return None
    # "/posts/{post_id}/like" -> "/posts/:id/like", "/posts/{post_id}" -> "/posts/:id"
    return _PATH_PARAM_RE.sub(":id", path)


def _route_from_path(path: str) -> str | None:
    """Normalize a raw request path to one of the 4 span routes, else None.

    Used to open the SERVER span before routing runs (scope["route"] only
    exists once the router has dispatched). Covers exactly the four API
    routes; /healthz and /metrics (and anything unmatched) get no spans.
    """
    if path == "/feed" or path == "/posts":
        return path
    if _PATH_ID_RE.match(path):
        return "/posts/:id"
    if _PATH_LIKE_RE.match(path):
        return "/posts/:id/like"
    return None


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
    """Pure ASGI middleware: one observation per request, after the response is sent.

    Also owns tracing: one fresh SERVER root span per API request, started
    before the app runs and ended after the response has been sent. Handlers
    run inside the span's context, so their DB child spans parent to it.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        route = _route_from_path(scope.get("path", ""))
        if route is None:
            # Not one of the four API routes (e.g. /healthz, /metrics): no spans.
            await self._handle(scope, receive, send)
            return

        method = scope["method"]
        with tracer.start_as_current_span(
            f"HTTP {method} {route}",
            kind=SpanKind.SERVER,
            attributes={"http.method": method, "http.route": route},
        ) as span:
            await self._handle(scope, receive, send, span)

    async def _handle(self, scope, receive, send, span=None):
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
            if (
                span is not None
                and route is not None
                and route != _route_from_path(scope.get("path", ""))
            ):
                # The route template is authoritative; re-check at span end.
                span.update_name(f"HTTP {scope['method']} {route}")
                span.set_attribute("http.route", route)
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
        DATABASE_URL, min_size=2, max_size=POOL_SIZE
    )
    try:
        yield
    finally:
        await app.state.pool.close()
        _tracer_provider.shutdown()  # flush pending spans promptly on exit


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(MetricsMiddleware)


def _db_span(name: str):
    """CLIENT span around a single DB round-trip (wall time start -> end)."""
    return tracer.start_as_current_span(
        name, kind=SpanKind.CLIENT, attributes={"db.system": "postgresql"}
    )


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
    with _db_span("DB Q1 feed"):
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
    with _db_span("DB Q2 single post"):
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
        with _db_span("DB Q3 create post"):
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
        with _db_span("DB Q4a post exists"):
            exists = await conn.fetchval(LIKE_CHECK_POST_SQL, post_id_val)
        if exists is None:
            return JSONResponse({"error": "post not found"}, status_code=404)
        # Q4b
        try:
            with _db_span("DB Q4b insert like"):
                await conn.execute(LIKE_INSERT_SQL, post_id_val, user_id)
        except asyncpg.ForeignKeyViolationError:
            return _bad_user_id()
        # Q4c
        with _db_span("DB Q4c like count"):
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
