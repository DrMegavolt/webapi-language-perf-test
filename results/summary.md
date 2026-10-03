## Baseline — 200 rps constant arrival, 5 min, 1 CPU / 1Gi per app

Latency from Prometheus `histogram_quantile` over the app's own `http_request_duration_seconds` histogram (server-side, excludes network).

| stack | p50 ms | p95 ms | p99 ms | p99.9 ms | served rps | 5xx | avg CPU cores | max pod RAM MB | p95 gate <500ms | p99 gate <1s | err gate <1% | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Go (Gin + pgx) | 0.6 | 0.96 | 1.0 | 2.4 | 171.96 | 0.0% | 0.044 | 15.2 | ✅ | ✅ | ✅ | **PASS** |
| Rust (Actix-web + tokio-postgres) | 0.74 | 1.39 | 1.9 | 3.88 | 177.75 | 0.0% | 0.044 | 8.2 | ✅ | ✅ | ✅ | **PASS** |
| TypeScript (Express 5 + pg) | 0.84 | 1.83 | 1.98 | 4.48 | 173.57 | 0.0% | 0.103 | 39.4 | ✅ | ✅ | ✅ | **PASS** |
| TypeScript (NestJS 11 Fastify + pg) | 0.75 | 1.57 | 1.96 | 4.5 | 173.03 | 0.0% | 0.072 | 51.4 | ✅ | ✅ | ✅ | **PASS** |
| Python (FastAPI + uvicorn + asyncpg) | 0.89 | 1.87 | 1.98 | 4.19 | 172.85 | 0.0% | 0.098 | 42.3 | ✅ | ✅ | ✅ | **PASS** |
| Ruby (Rails 8 + Puma + pg) | 0.87 | 1.95 | 4.15 | 4.94 | 175.79 | 0.0% | 0.147 | 108.7 | ✅ | ✅ | ✅ | **PASS** |
| .NET (ASP.NET Core minimal APIs + Npgsql) | 0.78 | 1.69 | 1.95 | 4.0 | 179.46 | 0.0% | 0.119 | 73.2 | ✅ | ✅ | ✅ | **PASS** |

## Stress ramp — 200→1800 rps over 4 min (per-30s buckets, gates from Prometheus)

The ramp tops out at the rig's offered-load ceiling; no stack breached a gate within it.

| stack | max rps passing all gates | passed whole ramp |
|---|---|---|
| Go (Gin + pgx) | 1742.5 | ✅ |
| Rust (Actix-web + tokio-postgres) | 1672.9 | ✅ |
| TypeScript (Express 5 + pg) | 1725.7 | ✅ |
| TypeScript (NestJS 11 Fastify + pg) | 1698.7 | ✅ |
| Python (FastAPI + uvicorn + asyncpg) | 1701.3 | ✅ |
| Ruby (Rails 8 + Puma + pg) | 1615.7 | ✅ |
| .NET (ASP.NET Core minimal APIs + Npgsql) | 1651.0 | ✅ |

> Note: k6 (v2.3.0) served purely as the open-model load generator. Its client-side
> percentile export changed shape in k6 v2, so the server-side Prometheus histograms
> above are the single source of record for latency (as intended).


## Per-endpoint p95 / p99.9 (ms) at baseline, from Prometheus

| stack | GET /feed p95 | GET /feed p99.9 | GET /posts/:id p95 | GET /posts/:id p99.9 | POST /posts p95 | POST /posts p99.9 | POST like p95 | POST like p99.9 |
|---|---|---|---|---|---|---|---|---|
| Go (Gin + pgx) | 0.98 | 3.25 | 0.48 | 1.0 | 0.48 | 1.56 | 0.97 | 1.97 |
| Rust (Actix-web + tokio-postgres) | 1.29 | 4.0 | 0.98 | 3.54 | 0.87 | 2.63 | 1.87 | 4.63 |
| TypeScript (Express 5 + pg) | 1.87 | 4.55 | 1.38 | 4.24 | 0.98 | 1.99 | 1.93 | 4.77 |
| TypeScript (NestJS 11 Fastify + pg) | 1.63 | 4.64 | 1.07 | 4.3 | 0.95 | 1.96 | 1.85 | 4.62 |
| Python (FastAPI + uvicorn + asyncpg) | 1.92 | 4.5 | 0.98 | 2.12 | 0.98 | 1.95 | 1.87 | 3.57 |
| Ruby (Rails 8 + Puma + pg) | 2.0 | 4.96 | 1.7 | 4.86 | 1.19 | 4.74 | 1.95 | 4.93 |
| .NET (ASP.NET Core minimal APIs + Npgsql) | 1.69 | 4.17 | 1.11 | 2.63 | 0.97 | 3.65 | 1.92 | 4.24 |
