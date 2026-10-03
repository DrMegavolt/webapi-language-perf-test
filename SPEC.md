# App spec — one tiny Twitter-style API, 7 stacks

Every stack MUST be behaviorally identical and expose identical Prometheus metrics so
results are directly comparable. When in doubt, follow this file.

## Canonical stack per app

| app dir     | language    | framework                                   | db driver                          |
|-------------|-------------|---------------------------------------------|------------------------------------|
| apps/go     | Go 1.25     | Gin v1                                      | pgx v5 (`pgxpool`)                 |
| apps/rust   | Rust stable | Actix-web 4 (`workers(1)` explicit)         | `tokio-postgres` + `deadpool-postgres` |
| apps/python | Python 3.12 | FastAPI + `uvicorn[standard]` single worker | `asyncpg`                          |
| apps/express| Node 22     | Express 5                                   | `pg` (node-postgres)               |
| apps/nestjs | Node 22     | NestJS 11 + `FastifyAdapter`                | `pg` (node-postgres)               |
| apps/rails  | Ruby 3.3    | Rails 8 API-only, Puma single process       | `pg` gem                           |
| apps/dotnet | .NET 10     | ASP.NET Core minimal APIs                   | Npgsql                             |

## Runtime contract

- Listen on `0.0.0.0:8080` (honor env `PORT`, default 8080). Never bind 127.0.0.1.
- DB: env `DATABASE_URL`, e.g. `postgres://bench:bench@postgres.langperf.svc.cluster.local:5432/bench`
- Connection pool: max 8 connections for every stack (db pool sizing must not differ).
- JSON only. Timestamps ISO-8601 UTC (e.g. `2026-01-02T03:04:05.123456Z` — `Z` or `+00:00` both fine).
- All numeric fields are JSON **numbers**, never strings — including 64-bit counts
  (`like_count`, `post_count`, `likes_received`). If a driver returns int8 as a string
  (node-pg), cast in SQL (`::int` is fine, counts are small) or convert in code.
- Disable per-request access logging in all stacks (Gin: no Logger middleware, ReleaseMode;
  uvicorn: `--no-access-log`; Nest: `logger: false`; Rails: `config.log_level = :warn`;
  Express/Nest-Fastify: no request logger; .NET: default is fine, no request logging added).

## Database schema (already deployed in the cluster — do not run migrations)

```sql
CREATE TABLE users (
  id         BIGSERIAL PRIMARY KEY,
  username   TEXT        NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE posts (
  id         BIGSERIAL PRIMARY KEY,
  user_id    BIGINT      NOT NULL REFERENCES users(id),
  content    TEXT        NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE likes (
  id         BIGSERIAL PRIMARY KEY,
  post_id    BIGINT      NOT NULL REFERENCES posts(id),
  user_id    BIGINT      NOT NULL REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT likes_post_user_key UNIQUE (post_id, user_id)
);
-- indexes: posts(created_at DESC, id DESC), posts(user_id), likes(post_id), likes(user_id)
```

## Canonical SQL — `sql/queries.sql` (binding)

`sql/queries.sql` at the repo root is the single source of truth for the exact
statements per endpoint (Q1 feed, Q2 single post, Q3 create post, Q4a-c like).
Every stack must issue **exactly those statements** with parameter binding — same
joins, same casts (`::bigint AS like_count`), same `ON CONFLICT` clause, no extra
queries, joins, or round-trips. Only allowed substitution: computing
`offset = (page-1)*20` in the app and binding it as one parameter.

## Endpoints

### GET /feed?page=N  (default page 1; 20 items per page)  (canonical Q1 — CTE form is required, see sql/queries.sql)
```sql
SELECT p.id, p.user_id, u.username, p.content, p.created_at,
       COUNT(l.id)::bigint AS like_count
FROM posts p
JOIN users u ON u.id = p.user_id
LEFT JOIN likes l ON l.post_id = p.id
GROUP BY p.id, p.user_id, u.username, p.content, p.created_at
ORDER BY p.created_at DESC, p.id DESC
LIMIT 20 OFFSET ($1 - 1) * 20;
```
Response 200:
```json
{"page": 1, "posts": [{"id": 1, "user_id": 2, "username": "user_2", "content": "...", "like_count": 7, "created_at": "2026-01-02T03:04:05.123456+00:00"}]}
```
(≤20 items; empty array on pages past the end, still 200.)

### POST /posts  body `{"user_id": 123, "content": "..."}`  (canonical Q3)
Exactly one statement — the `INSERT ... RETURNING` from `sql/queries.sql`. No user
existence pre-check, no join, no extra round-trip (a missing `user_id` violates the
FK → map that driver error to 400 `{"error":"invalid user_id"}`; k6 always sends valid ids).
- 201 with EXACTLY: `{"id": <n>, "user_id": <n>, "content": "...", "created_at": "...", "like_count": 0}`
  — `like_count: 0` is computed in the app, never queried. No `username` field.

### POST /posts/:id/like  body `{"user_id": 456}`  (canonical Q4a → Q4b → Q4c, in order)
- 4a `SELECT 1 FROM posts WHERE id = $1` — no row → 404 `{"error":"post not found"}`
- 4b `INSERT INTO likes ... ON CONFLICT (post_id, user_id) DO NOTHING`
- 4c `SELECT COUNT(*)::bigint AS like_count FROM likes WHERE post_id = $1`
- 200 `{"post_id": 5, "like_count": 12}` (idempotent per (post_id, user_id)).
- Non-numeric `:id` → 400 `{"error":"invalid post id"}` (never reaches the DB).

### GET /posts/:id  (canonical Q2)
```sql
SELECT p.id, p.user_id, u.username, p.content, p.created_at,
       COUNT(l.id)::bigint AS like_count
FROM posts p
JOIN users u ON u.id = p.user_id
LEFT JOIN likes l ON l.post_id = p.id
WHERE p.id = $1
GROUP BY p.id, p.user_id, u.username, p.content, p.created_at;
```
- 200 with the single post item (same shape as feed items): `{"id": 5, "user_id": 2, "username": "user_2", "content": "...", "like_count": 12, "created_at": "..."}`
- Missing → 404 `{"error":"post not found"}`. Non-numeric → 400 `{"error":"invalid post id"}`.

### GET /healthz
`{"status":"ok"}` after a successful `SELECT 1` against the DB. Not recorded in metrics.

### GET /metrics
Prometheus text format (exposition below). Not recorded in metrics.

## Metrics contract (MUST match exactly)

Metric names, label names and label **values** are the comparison key — do not improvise:

- Histogram `http_request_duration_seconds`, labels: `method`, `route`, `status`.
  Buckets: `0.0005, 0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10` (+Inf implied).
- Counter `http_requests_total`, labels: `method`, `route`, `status`.
- `route` label values are the *pattern*, exactly: `/feed`, `/posts`, `/posts/:id`, `/posts/:id/like`
- `method`: `GET` / `POST`. `status`: string status code, e.g. `"200"`.
- Observe each request **exactly once**, after the response is sent, with elapsed = wall time
  around the full request handling (middleware/hook level, includes body parse + DB time).
- Do NOT record `/metrics` or `/healthz`.
- The `app` label is added by the cluster's PodMonitor relabeling — apps do not add it.
- **RAM tracking (required):** a gauge `app_memory_rss_bytes` (no labels), updated inside the
  same metrics middleware on every recorded request, with the process's current resident
  memory read from `/proc/self/status` → `VmRSS:` line (kB × 1024 = bytes). This is identical
  semantics across all stacks; the cluster additionally records pod-level cgroup memory
  (container_memory_working_set_bytes) which is what the final report uses.

## Kubernetes manifest — `apps/<app>/k8s.yaml`

Copy this template, replacing `__APP__` with the app name. Do not change resources, probes,
labels, ports or env (they are the experiment's constants):

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: langperf-__APP__
  namespace: langperf
  labels:
    app.kubernetes.io/name: __APP__
    app.kubernetes.io/part-of: langperf
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: __APP__
  template:
    metadata:
      labels:
        app: __APP__
        app.kubernetes.io/name: __APP__
        app.kubernetes.io/part-of: langperf
    spec:
      containers:
        - name: app
          image: localhost:32000/langperf/__APP__:v1
          imagePullPolicy: IfNotPresent
          ports:
            - name: http
              containerPort: 8080
          env:
            - name: DATABASE_URL
              value: postgres://bench:bench@postgres.langperf.svc.cluster.local:5432/bench
          resources:
            requests:
              cpu: "1"
              memory: 1Gi
            limits:
              cpu: "1"
              memory: 1Gi
          readinessProbe:
            httpGet:
              path: /healthz
              port: 8080
            initialDelaySeconds: 2
            periodSeconds: 5
            timeoutSeconds: 5
            failureThreshold: 30
---
apiVersion: v1
kind: Service
metadata:
  name: langperf-__APP__
  namespace: langperf
  labels:
    app.kubernetes.io/name: __APP__
    app.kubernetes.io/part-of: langperf
spec:
  selector:
    app.kubernetes.io/name: __APP__
  ports:
    - name: http
      port: 80
      targetPort: 8080
```

Add only extra env vars your runtime needs (e.g. `GOMAXPROCS: "1"`, `WEB_CONCURRENCY: "0"`,
`RAILS_MAX_THREADS: "8"`, `RUBY_YJIT_ENABLE: "1"`) in the same env list. Set thread/worker
counts so the app uses ONE core, not host core counts (actix: `.workers(1)` in code).

## Docker image

- Build (from repo root or app dir):
  `docker buildx build --platform linux/amd64 --provenance=false --sbom=false -t localhost:32000/langperf/<app>:v1 --load apps/<app>`
- Multi-stage: final image is runtime-only (no compilers). The binary/app must actually run
  under linux/amd64 emulation — you will test exactly that.
- Push: `docker push localhost:32000/langperf/<app>:v1`
  (a registry port-forward on localhost:32000 is already running; if it errors, tell me —
  do NOT modify docker config or try the node IP).

## Local verification (REQUIRED before reporting done)

Ports: postgres `554XX`, app `180XX` — use the ones assigned to your app.

1. `docker run -d --name langperf-pg-<app> -p 554XX:5432 -e POSTGRES_USER=bench -e POSTGRES_PASSWORD=bench -e POSTGRES_DB=bench postgres:16-alpine`
2. Small seed (same shape as prod seed):
   ```sql
   INSERT INTO users (username, created_at) SELECT 'user_'||g, now() FROM generate_series(1,50) g;
   INSERT INTO posts (user_id, content, created_at) SELECT (1+floor(random()*50))::bigint, 'seed post '||g, now() FROM generate_series(1,500) g;
   INSERT INTO likes (post_id, user_id, created_at) SELECT (1+floor(random()*500))::bigint, (1+floor(random()*50))::bigint, now() FROM generate_series(1,2000) g ON CONFLICT DO NOTHING;
   ```
3. `docker run --platform linux/amd64 -d --name langperf-app-<app> -p 180XX:8080 -e DATABASE_URL=postgres://bench:bench@host.docker.internal:554XX/bench localhost:32000/langperf/<app>:v1`
   (wait for it to boot; rails takes ~10-20s)
4. curl and verify, against `http://localhost:180XX`:
   - `GET /feed?page=1` → 200, exactly 20 posts, `like_count` is a JSON number
   - `POST /posts` valid body → 201; response is exactly `{id, user_id, content, created_at, like_count:0}` (no username)
   - `GET /posts/1` → 200 with `username` + `like_count` numbers; `GET /posts/999999` → 404
   - `POST /posts/1/like` → 200 with numeric `like_count`; `POST /posts/999999/like` → 404
   - `GET /healthz` → 200
   - `GET /metrics` contains `http_request_duration_seconds_bucket` with `route="/feed"` and
     `le="0.01"` labels, and `http_requests_total{...route="/posts"...}`; contains
     `app_memory_rss_bytes`; and does NOT contain `route="/metrics"` or `route="/healthz"`
5. `docker rm -f langperf-pg-<app> langperf-app-<app>` when done.

## Definition of done (report all of this back)

- [ ] Source + lockfile (go.sum / Cargo.lock / package-lock.json / Gemfile.lock / requirements.txt pinned / nuget) under `apps/<app>/`
- [ ] Image builds with the exact buildx command above
- [ ] All endpoints verified via curl against the **linux/amd64** container (paste status codes)
- [ ] `/metrics` shows both metric families, exact names/labels, no /metrics or /healthz entries (paste a 3-line grep)
- [ ] `apps/<app>/k8s.yaml` matches the template
- [ ] Image pushed: `localhost:32000/langperf/<app>:v1` (paste the pushed digest line)
- [ ] No git commits — the orchestrator handles git
