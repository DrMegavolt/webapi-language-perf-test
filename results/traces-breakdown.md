# Trace breakdown (mean per request, from OTel spans via Tempo)

| stack | traces | HTTP mean | HTTP p95 | DB total | DB % | app self | |
|---|---|---|---|---|---|---|---|
| Python (FastAPI) | 120 | 315.64 ms | 1206.56 ms | 0.0 ms | 0.0% | 315.64 ms | |

## Mean DB span time per query (ms)

| stack | Q1 feed | Q2 post | Q3 create | Q4a exists | Q4b insert | Q4c count |
|---|---|---|---|---|---|---|
| Python (FastAPI) | — | — | — | — | — | — |
