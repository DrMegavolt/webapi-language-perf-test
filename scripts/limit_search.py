#!/usr/bin/env python3
"""Adaptive per-stack limit search.

Ladder: start at START_LEVEL (2k). Pass easily (p99<150ms, cpu<0.85) -> x4 jump,
otherwise x2. On first failure, geometric bisection between last pass and first
fail until the bracket is <=15% (max 4 refinement runs). Then 3 confirmation runs
at the limit (and 2 extra at 2k if 2k passed) for averages.

Gate ("no errors, p99<1s"): server p99 < 1000ms AND 5xx == 0 AND k6 failed == 0
AND k6 dropped_iterations == 0.

Usage: python3 scripts/limit_search.py <app>
"""
import glob
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

NS = "langperf"
PROM = "http://192.168.1.174:9090"
DURATION_S = 150
START_LEVEL = int(os.environ.get("START_LEVEL", "2000"))
MAX_LEVEL = int(os.environ.get("MAX_LEVEL", "64000"))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTDIR = os.path.join(REPO, "results", "limits")


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=REPO)


def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def prom_one(expr, t):
    qs = urllib.parse.urlencode({"query": expr, "time": t})
    try:
        with urllib.request.urlopen(f"{PROM}/api/v1/query?{qs}", timeout=60) as r:
            d = json.load(r)
        res = d["data"]["result"]
        return float(res[0]["value"][1]) if res else 0.0
    except Exception:
        return 0.0


def run_k6(app, rate, label):
    """Run one k6 job at `rate`; returns merged record dict."""
    sh(f"kubectl -n {NS} delete job langperf-k6 --ignore-not-found")
    start = now_ts()
    sed = (
        f"sed 's|__BASE_URL__|http://langperf-{app}.{NS}.svc.cluster.local|; "
        f"s|__RATE__|{rate}|; s|__DURATION__|{DURATION_S}s|; s|__MODE__|baseline|' "
        f"k8s/k6-job.yaml | kubectl apply -f -"
    )
    r = sh(sed)
    if r.returncode != 0:
        raise RuntimeError(f"kubectl apply k6 failed: {r.stderr[:300]}")
    done = sh(f"kubectl -n {NS} wait --for=condition=complete job/langperf-k6 --timeout=1500s")
    ok = done.returncode == 0
    end = now_ts()
    logs = sh(f"kubectl -n {NS} logs job/langperf-k6").stdout
    base = f"{OUTDIR}/{app}.L{rate}.{label}"
    with open(f"{base}.k6.log", "w") as f:
        f.write(logs)
    k6 = {}
    for line in logs.splitlines():
        if "K6SUMMARY " in line:
            try:
                k6 = json.loads(line.split("K6SUMMARY ", 1)[1])
            except Exception:
                pass
            break

    prom_file = f"{base}.prom.json"
    c = sh(
        f"python3 scripts/collect_results.py --app {app} --start {start} --end {end} "
        f"--prom {PROM} --out {prom_file}"
    )
    rec = {}
    try:
        rec = json.load(open(prom_file))
    except Exception:
        rec = {"app": app, "collect_error": c.stderr[-300:]}
    w = f"{max(120, DURATION_S + 30)}s"
    rec.update(
        {
            "offered_rps": rate,
            "label": label,
            "k6_completed": ok,
            "k6_rps": k6.get("rps"),
            "k6_iterations": k6.get("iterations"),
            "k6_dropped": k6.get("dropped_iterations", 0),
            "k6_failed_rate": k6.get("failed_rate", 0),
            "k6_p99_ms": k6.get("p99_ms"),
            "pg_cpu_cores": round(prom_one(
                'sum(rate(container_cpu_usage_seconds_total{namespace="langperf",pod="postgres-0",container!=""}['
                + w
                + "]))",
                end,
            ), 3),
            "k6_cpu_cores": round(prom_one(
                'sum(rate(container_cpu_usage_seconds_total{namespace="langperf",pod=~"langperf-k6-.*",container!=""}['
                + w
                + "]))",
                end,
            ), 3),
        }
    )
    served = k6.get("rps") or 0
    rec["served_ratio"] = round(served / rate, 3) if rate else 0
    rec["gate_pass"] = bool(
        ok
        and rec.get("p99_ms", 1e9) < 1000
        and rec.get("error_rate_5xx", 1) == 0
        and rec.get("k6_failed_rate", 1) == 0
        and rec.get("k6_dropped", 1) == 0
        and rec["served_ratio"] >= 0.95
    )
    with open(prom_file, "w") as f:
        json.dump(rec, f, indent=2)
    log(
        f"rate={rate:>6} {label:<8} -> p99={rec.get('p99_ms')}ms err={rec.get('error_rate_5xx')} "
        f"served={served:.0f} ({rec['served_ratio']*100:.0f}%) dropped={rec.get('k6_dropped')} "
        f"appcpu={rec.get('cpu_avg_cores')} pgcpu={rec.get('pg_cpu_cores')} "
        f"k6cpu={rec.get('k6_cpu_cores')} GATE={'PASS' if rec['gate_pass'] else 'FAIL'}"
    )
    return rec


def reseed():
    sh(f"kubectl -n {NS} delete job seed --ignore-not-found")
    sh(f"kubectl -n {NS} delete pod langperf-seed-helper --ignore-not-found --force --grace-period=0")
    sh(f"kubectl apply -f k8s/seed-job.yaml")
    for _ in range(120):
        if sh(f"kubectl -n {NS} wait --for=condition=complete job/seed --timeout=10s").returncode == 0:
            log("reseeded (50k/500k/2.2M fresh)")
            return
        time.sleep(5)
    raise RuntimeError("reseed timed out")


def classify(rec):
    if rec is None:
        return "n/a"
    app_cpu = rec.get("cpu_avg_cores") or 0
    pg = rec.get("pg_cpu_cores") or 0
    k6c = rec.get("k6_cpu_cores") or 0
    if k6c >= 5.0:
        return "k6 generator saturated"
    if app_cpu >= 0.9:
        return "app CPU (1 core)"
    if pg >= 5.5:
        return "postgres ceiling"
    return "latency collapse (queueing)"


def main(app):
    os.makedirs(OUTDIR, exist_ok=True)
    curve = []

    # always run the current script version, never a stale configmap
    sh("kubectl -n langperf create configmap langperf-k6-script "
       "--from-file=loadtest.js=k6/loadtest.js --dry-run=client -o yaml | kubectl apply -f -")

    log(f"deploy langperf-{app}")
    sh(f"kubectl apply -f apps/{app}/k8s.yaml")
    if sh(f"kubectl -n {NS} rollout status deploy/langperf-{app} --timeout=300s").returncode != 0:
        raise RuntimeError("rollout failed")
    time.sleep(5)
    reseed()

    last_pass = 200.0  # known-pass from the baseline run
    first_fail = None
    fail_rec = None

    # --- ladder ---
    level = START_LEVEL
    while level <= MAX_LEVEL:
        rec = run_k6(app, level, "search")
        curve.append(rec)
        if rec["gate_pass"]:
            last_pass = level
            easy = rec.get("p99_ms", 1e9) < 150 and (rec.get("cpu_avg_cores") or 0) < 0.85
            nxt = min(MAX_LEVEL, int(level * (4 if easy else 2)))
            if nxt <= level:
                break
            level = nxt
        else:
            first_fail = level
            fail_rec = rec
            break

    # --- bisection ---
    iters = 0
    if first_fail is not None:
        while first_fail / max(last_pass, 1) > 1.15 and iters < 4:
            mid = int(round((last_pass * first_fail) ** 0.5 / 100.0) * 100)
            if mid <= last_pass or mid >= first_fail:
                break
            rec = run_k6(app, mid, "bisect")
            curve.append(rec)
            iters += 1
            if rec["gate_pass"]:
                last_pass = mid
            else:
                first_fail = mid
                fail_rec = rec

    limit = last_pass

    # --- confirmation runs (averages) ---
    confirms = []
    if limit >= START_LEVEL:
        for i in range(3):
            time.sleep(20)
            rec = run_k6(app, limit, f"confirm{i+1}")
            confirms.append(rec)
            curve.append(rec)
    if limit > START_LEVEL:
        # top up the 2k level to 3 runs for averages
        have2k = sum(1 for c in curve if c["offered_rps"] == START_LEVEL)
        for i in range(3 - have2k):
            time.sleep(20)
            rec = run_k6(app, START_LEVEL, f"extra{i+1}")
            curve.append(rec)

    summary = {
        "app": app,
        "limit_rps": limit,
        "first_fail_rps": first_fail,
        "gate": "p99<1000ms, 0 5xx, 0 k6 failed, 0 dropped",
        "bottleneck_at_first_fail": classify(fail_rec) if fail_rec else "not reached (rig ceiling)",
        "confirm_p99_ms": [c.get("p99_ms") for c in confirms],
        "confirm_err": [c.get("error_rate_5xx") for c in confirms],
        "curve": [
            {k: c.get(k) for k in ("offered_rps", "label", "p50_ms", "p95_ms", "p99_ms", "p999_ms",
                                    "rps", "k6_rps", "served_ratio", "error_rate_5xx",
                                    "cpu_avg_cores", "pg_cpu_cores",
                                    "k6_cpu_cores", "k6_dropped", "k6_failed_rate", "gate_pass")}
            for c in curve
        ],
    }
    with open(f"{OUTDIR}/{app}.limit.json", "w") as f:
        json.dump(summary, f, indent=2)
    log(f"LIMIT for {app}: {limit} rps (first fail {first_fail}) — {summary['bottleneck_at_first_fail']}")

    sh(f"kubectl delete -f apps/{app}/k8s.yaml --ignore-not-found")
    log(f"teardown langperf-{app} done")


if __name__ == "__main__":
    main(sys.argv[1])
