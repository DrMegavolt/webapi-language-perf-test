#!/usr/bin/env python3
"""Collect one app's benchmark stats from Prometheus for the load-test window.

--mode baseline: windowed percentiles + resource usage + pass/fail gates
                 (p95 < 500ms, p99 < 1s, error rate < 1%)
--mode ramp:     per-30s-bucket analysis of the stress ramp (breaking point)
"""
import argparse
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROUTES = ["/feed", "/posts", "/posts/:id", "/posts/:id/like"]


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class Prom:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def query(self, expr, t):
        qs = urllib.parse.urlencode({"query": expr, "time": t})
        return self._get(f"{self.base}/api/v1/query?{qs}")

    def query_range(self, expr, start, end, step):
        qs = urllib.parse.urlencode(
            {"query": expr, "start": start.timestamp(), "end": end.timestamp(), "step": step}
        )
        return self._get(f"{self.base}/api/v1/query_range?{qs}")

    def _get(self, url):
        with urllib.request.urlopen(url, timeout=60) as r:
            d = json.load(r)
        if d.get("status") != "success":
            raise RuntimeError(f"prom query failed: {d}")
        return d["data"]["result"]


def one(prom, expr, t, default=None):
    res = prom.query(expr, t)
    if not res:
        return default
    return float(res[0]["value"][1])


def pct_expr(q, window, app, extra=""):
    sel = f'app="{app}"{extra}'
    return (
        f"histogram_quantile({q}, sum by (le) "
        f"(rate(http_request_duration_seconds_bucket{{{sel}}}[{window}])))"
    )


def collect_baseline(args, start, end, end_epoch, w, prom, app):
    total_rate = f'sum(rate(http_requests_total{{app="{app}"}}[{w}]))'
    err_rate = (
        f'sum(rate(http_requests_total{{app="{app}",status=~"5.."}}[{w}])) / '
        f'sum(rate(http_requests_total{{app="{app}"}}[{w}]))'
    )
    cpu = (
        f'sum(rate(container_cpu_usage_seconds_total{{namespace="langperf",'
        f'pod=~"langperf-{app}-.*",container!=""}}[{w}]))'
    )
    mem = (
        f'max_over_time((sum(container_memory_working_set_bytes{{namespace="langperf",'
        f'pod=~"langperf-{app}-.*",container!=""}}))[{w}:])'
    )
    rss = f'max_over_time(app_memory_rss_bytes{{app="{app}"}}[{w}])'

    p50 = round(one(prom, pct_expr(0.50, w, app), end_epoch, 0.0) * 1000, 2)
    p95 = round(one(prom, pct_expr(0.95, w, app), end_epoch, 0.0) * 1000, 2)
    p99 = round(one(prom, pct_expr(0.99, w, app), end_epoch, 0.0) * 1000, 2)
    p999 = round(one(prom, pct_expr(0.999, w, app), end_epoch, 0.0) * 1000, 2)
    err = one(prom, err_rate, end_epoch, 0.0) or 0.0
    err = round(err if err == err else 0.0, 6)

    result = {
        "app": app,
        "mode": "baseline",
        "window": {"start": args.start, "end": args.end, "seconds": w and int(w[:-1]) - 30},
        "scrape_up": one(prom, 'up{job=~".*langperf-apps"}', end_epoch, 0.0),
        "p50_ms": p50,
        "p95_ms": p95,
        "p99_ms": p99,
        "p999_ms": p999,
        "rps": round(one(prom, total_rate, end_epoch, 0.0), 2),
        "error_rate_5xx": err,
        "cpu_avg_cores": round(one(prom, cpu, end_epoch, 0.0) or 0.0, 3),
        "mem_max_mb": round((one(prom, mem, end_epoch, 0.0) or 0.0) / 1048576, 1),
        "rss_self_max_mb": round((one(prom, rss, end_epoch, 0.0) or 0.0) / 1048576, 1),
        "routes": {},
        "gates": {
            "p95_lt_500ms": p95 < 500,
            "p99_lt_1s": p99 < 1000,
            "error_lt_1pct": err < 0.01,
        },
    }
    result["gates"]["pass"] = all(result["gates"].values())
    for route in ROUTES:
        result["routes"][route] = {
            "p50_ms": round(one(prom, pct_expr(0.50, w, app, f',route="{route}"'), end_epoch, 0.0) * 1000, 2),
            "p95_ms": round(one(prom, pct_expr(0.95, w, app, f',route="{route}"'), end_epoch, 0.0) * 1000, 2),
            "p999_ms": round(one(prom, pct_expr(0.999, w, app, f',route="{route}"'), end_epoch, 0.0) * 1000, 2),
            "rps": round(one(prom, f'sum(rate(http_requests_total{{app="{app}",route="{route}"}}[{w}]))', end_epoch, 0.0), 2),
        }
    return result


def collect_ramp(args, start, end, prom, app):
    step = "30s"
    p95_expr = pct_expr(0.95, "45s", app)
    p99_expr = pct_expr(0.99, "45s", app)
    rps_expr = f'sum(rate(http_requests_total{{app="{app}"}}[{step}]))'
    err_expr = (
        f'sum(rate(http_requests_total{{app="{app}",status=~"5.."}}[{step}])) / '
        f'sum(rate(http_requests_total{{app="{app}"}}[{step}]))'
    )

    def series(expr):
        res = prom.query_range(expr, start, end, step)
        return {float(v[0]): v[1] for r in res for v in r["values"]}

    p95s, p99s, rpss, errs = series(p95_expr), series(p99_expr), series(rps_expr), series(err_expr)
    buckets = []
    for t in sorted(rpss):
        rps = rpss.get(t)
        p95 = float(p95s.get(t, 0)) * 1000
        p99 = float(p99s.get(t, 0)) * 1000
        err = float(errs.get(t, 0.0))
        err = err if err == err else 0.0  # NaN (0/0) when the app served nothing
        ok = p95 < 500 and p99 < 1000 and err < 0.01
        buckets.append(
            {
                "t": datetime.fromtimestamp(t, tz=timezone.utc).strftime("%H:%M:%S"),
                "rps": round(rps, 1),
                "p95_ms": round(p95, 1),
                "p99_ms": round(p99, 1),
                "err_rate": round(err, 5),
                "pass": ok,
            }
        )
    passing = [b["rps"] for b in buckets if b["pass"]]
    failing = [b["rps"] for b in buckets if not b["pass"]]
    return {
        "app": app,
        "mode": "ramp",
        "window": {"start": args.start, "end": args.end},
        "buckets": buckets,
        "max_passing_rps": round(max(passing), 1) if passing else 0.0,
        "first_failing_rps": round(min(failing), 1) if failing else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True)
    ap.add_argument("--start", required=True, help="UTC, %Y-%m-%dT%H:%M:%SZ")
    ap.add_argument("--end", required=True)
    ap.add_argument("--prom", default="http://192.168.1.174:9090")
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", default="baseline", choices=["baseline", "ramp"])
    args = ap.parse_args()

    start, end = parse_ts(args.start), parse_ts(args.end)
    end_epoch = end.timestamp()
    window = max(120, int((end - start).total_seconds()) + 30)
    w = f"{window}s"
    prom = Prom(args.prom)
    app = args.app

    if args.mode == "ramp":
        result = collect_ramp(args, start, end, prom, app)
    else:
        result = collect_baseline(args, start, end, end_epoch, w, prom, app)

    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
