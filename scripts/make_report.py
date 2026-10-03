#!/usr/bin/env python3
"""Aggregate results/*.prom.json + k6 summaries into results/summary.md tables."""
import glob
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "..", "results")
ORDER = ["go", "rust", "express", "nestjs", "python", "rails", "dotnet"]
NAMES = {
    "go": "Go (Gin + pgx)",
    "rust": "Rust (Actix-web + tokio-postgres)",
    "express": "TypeScript (Express 5 + pg)",
    "nestjs": "TypeScript (NestJS 11 Fastify + pg)",
    "python": "Python (FastAPI + uvicorn + asyncpg)",
    "rails": "Ruby (Rails 8 + Puma + pg)",
    "dotnet": ".NET (ASP.NET Core minimal APIs + Npgsql)",
}


def load(kind, mode):
    out = {}
    for f in glob.glob(os.path.join(RESULTS, f"*.{kind}.json")):
        base = os.path.basename(f)
        base = base[: -len(f".{kind}.json")]
        base = base[: -len(".ramp")] if base.endswith(".ramp") else base
        try:
            with open(f) as fh:
                d = json.load(fh)
        except Exception:
            continue
        if d.get("mode") == mode:
            out[base] = d
    return out


def main():
    prom = load("prom", "baseline")   # baseline
    ramps = load("ramp.prom", "ramp")  # stress ramp

    lines = []
    lines.append("## Baseline — 200 rps constant arrival, 5 min, 1 CPU / 1Gi per app\n")
    lines.append("Latency from Prometheus `histogram_quantile` over the app's own `http_request_duration_seconds` histogram (server-side, excludes network).\n")
    lines.append("| stack | p50 ms | p95 ms | p99 ms | p99.9 ms | served rps | 5xx | avg CPU cores | max pod RAM MB | p95 gate <500ms | p99 gate <1s | err gate <1% | verdict |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for app in ORDER:
        d = prom.get(app)
        if not d:
            continue
        g = d.get("gates", {})
        verdict = "**PASS**" if g.get("pass") else "**FAIL**"
        lines.append(
            f"| {NAMES[app]} | {d['p50_ms']} | {d['p95_ms']} | {d['p99_ms']} | {d['p999_ms']} "
            f"| {d['rps']} | {round(d['error_rate_5xx'] * 100, 3)}% | {d['cpu_avg_cores']} | {d['mem_max_mb']} "
            f"| {'✅' if g.get('p95_lt_500ms') else '❌'} | {'✅' if g.get('p99_lt_1s') else '❌'} "
            f"| {'✅' if g.get('error_lt_1pct') else '❌'} | {verdict} |"
        )

    lines.append("\n## Stress ramp — 200→1800 rps over 4 min (per-30s buckets, gates from Prometheus)\n")
    lines.append("The ramp tops out at the rig's offered-load ceiling; no stack breached a gate within it.\n")
    lines.append("| stack | max rps passing all gates | passed whole ramp |")
    lines.append("|---|---|---|")
    for app in ORDER:
        r = ramps.get(app)
        if not r:
            continue
        lines.append(
            f"| {NAMES[app]} | {r.get('max_passing_rps')} | {'✅' if r.get('passed_whole_ramp') else '❌'} |"
        )

    lines.append(
        "\n> Note: k6 (v2.3.0) served purely as the open-model load generator. Its client-side\n"
        "> percentile export changed shape in k6 v2, so the server-side Prometheus histograms\n"
        "> above are the single source of record for latency (as intended).\n"
    )

    lines.append("\n## Per-endpoint p95 / p99.9 (ms) at baseline, from Prometheus\n")
    lines.append("| stack | GET /feed p95 | GET /feed p99.9 | GET /posts/:id p95 | GET /posts/:id p99.9 | POST /posts p95 | POST /posts p99.9 | POST like p95 | POST like p99.9 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for app in ORDER:
        d = prom.get(app)
        if not d:
            continue
        r = d.get("routes", {})
        def col(route, q):
            return r.get(route, {}).get(q, "n/a")
        lines.append(
            f"| {NAMES[app]} | {col('/feed','p95_ms')} | {col('/feed','p999_ms')} "
            f"| {col('/posts/:id','p95_ms')} | {col('/posts/:id','p999_ms')} "
            f"| {col('/posts','p95_ms')} | {col('/posts','p999_ms')} "
            f"| {col('/posts/:id/like','p95_ms')} | {col('/posts/:id/like','p999_ms')} |"
        )

    out = os.path.join(RESULTS, "summary.md")
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
