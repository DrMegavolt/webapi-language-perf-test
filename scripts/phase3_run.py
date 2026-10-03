#!/usr/bin/env python3
"""Phase 3: fixed-load test — one app at a time at LEVEL rps (default 10000),
1 CPU per app, POOL_SIZE=32, fresh-seeded DB, RUNS runs per app.

Usage: python3 scripts/phase3_run.py <app> [level]
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import k6run_lib as K  # noqa: E402

LEVEL = int(os.environ.get("LEVEL", "10000"))
RUNS = int(os.environ.get("RUNS", "3"))
POOL_SIZE = os.environ.get("POOL_SIZE", "32")
OUTDIR = os.path.join(K.REPO, "results", "phase3")


def deploy(app):
    K.log(f"deploy langperf-{app} (POOL_SIZE={POOL_SIZE}, 1 CPU)")
    K.sh(f"kubectl apply -f apps/{app}/k8s.yaml")
    r = K.sh(f"kubectl -n {K.NS} set env deploy/langperf-{app} POOL_SIZE={POOL_SIZE}")
    if r.returncode != 0:
        raise RuntimeError(f"set env failed: {r.stderr[:300]}")
    if K.sh(f"kubectl -n {K.NS} rollout status deploy/langperf-{app} --timeout=300s").returncode != 0:
        raise RuntimeError("rollout failed")
    env = K.sh(f"kubectl -n {K.NS} get deploy langperf-{app} -o "
               f"jsonpath={{.spec.template.spec.containers[0].env}}").stdout
    if "POOL_SIZE" not in env:
        raise RuntimeError("POOL_SIZE env missing after set — aborting rather than measuring pool=8")
    K.log(f"env verified: {env}")
    time.sleep(5)


def main(app, level):
    K.refresh_script_configmap()
    deploy(app)
    K.reseed()

    runs = []
    for i in range(1, RUNS + 1):
        time.sleep(20)
        runs.append(K.run_k6(app, level, f"run{i}", OUTDIR, duration_s=150))

    passing = sum(1 for r in runs if r["gate_pass"])
    p99s = [r.get("p99_ms") for r in runs]
    ratios = [r.get("served_ratio") for r in runs]
    summary = {
        "app": app,
        "level_rps": level,
        "pool_size": int(POOL_SIZE),
        "runs": RUNS,
        "gate_pass_runs": passing,
        "p99_ms": p99s,
        "served_ratio": ratios,
        "error_5xx": [r.get("error_rate_5xx") for r in runs],
        "cpu_avg_cores": [r.get("cpu_avg_cores") for r in runs],
        "pg_cpu_cores": [r.get("pg_cpu_cores") for r in runs],
        "k6_dropped": [r.get("k6_dropped") for r in runs],
        "note": "1 CPU per app, POOL_SIZE=32, fresh DB per app, k6 mix 75/25 read/write",
    }
    with open(f"{OUTDIR}/{app}.json", "w") as f:
        json.dump(summary, f, indent=2)
    K.log(f"{app} @ {level}: gate {passing}/{RUNS} pass, p99s={p99s}, served={ratios}")

    K.sh(f"kubectl delete -f apps/{app}/k8s.yaml --ignore-not-found")
    K.log(f"teardown langperf-{app} done")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else LEVEL)
