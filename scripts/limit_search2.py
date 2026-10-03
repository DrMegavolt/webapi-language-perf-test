#!/usr/bin/env python3
"""Limit search v2 — pool 32, 2s k6 timeout (overqueue = error).

Adaptive ladder starting at START_LEVEL (default 10k): jump x2 (x4 when a level
is easy: p99<150ms and cpu<0.85); on first failure geometric bisection between
last pass and first fail (<=4 refinements, bracket <=15%); then 3 confirmation
runs at the found limit.

Gate: server p99 < 1s AND 0 5xx AND 0 k6 failed (incl. 2s timeouts) AND
0 dropped iterations AND served >= 95% of offered.

Usage: python3 scripts/limit_search2.py <app>
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import k6run_lib as K  # noqa: E402

START_LEVEL = int(os.environ.get("START_LEVEL", "10000"))
MAX_LEVEL = int(os.environ.get("MAX_LEVEL", "64000"))
POOL_SIZE = os.environ.get("POOL_SIZE", "32")
DURATION_S = 150
OUTDIR = os.path.join(K.REPO, "results", "limits-v2")


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
        raise RuntimeError("POOL_SIZE env missing after set — aborting")
    time.sleep(5)


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
    mem = rec.get("mem_max_mb") or 0
    if mem >= 950:
        return "app RAM (1Gi pressure)"
    return "overqueue collapse (latency, not CPU)"


def main(app):
    os.makedirs(OUTDIR, exist_ok=True)
    K.refresh_script_configmap()
    deploy(app)
    K.reseed()

    curve = []
    last_pass = 0.0
    first_fail = None
    fail_rec = None

    level = START_LEVEL
    while level <= MAX_LEVEL:
        rec = K.run_k6(app, level, "search", OUTDIR, duration_s=DURATION_S)
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

    iters = 0
    if first_fail is not None and last_pass > 0:
        while first_fail / max(last_pass, 1) > 1.15 and iters < 4:
            mid = int(round((last_pass * first_fail) ** 0.5 / 100.0) * 100)
            if mid <= last_pass or mid >= first_fail:
                break
            rec = K.run_k6(app, mid, "bisect", OUTDIR, duration_s=DURATION_S)
            curve.append(rec)
            iters += 1
            if rec["gate_pass"]:
                last_pass = mid
            else:
                first_fail = mid
                fail_rec = rec

    limit = last_pass

    confirms = []
    if limit >= START_LEVEL:
        for i in range(1, 4):
            time.sleep(20)
            rec = K.run_k6(app, limit, f"confirm{i}", OUTDIR, duration_s=DURATION_S)
            confirms.append(rec)
            curve.append(rec)
    else:
        K.log(f"{app}: failed at START_LEVEL {START_LEVEL} — no bracket above, limit set to 0 "
              f"(never passed at start; see curve)")

    summary = {
        "app": app,
        "limit_rps": limit,
        "first_fail_rps": first_fail,
        "pool_size": int(POOL_SIZE),
        "timeout_budget": "2s (k6 client)",
        "gate": "server p99<1s, 0 5xx, 0 k6 failed (incl timeouts), 0 dropped, served>=95%",
        "bottleneck_at_first_fail": classify(fail_rec) if fail_rec else "not reached",
        "confirm_p99_ms": [c.get("p99_ms") for c in confirms],
        "confirm_err": [c.get("error_rate_5xx") for c in confirms],
        "confirm_mem_max_mb": [c.get("mem_max_mb") for c in confirms],
        "curve": [
            {k: c.get(k) for k in ("offered_rps", "label", "p50_ms", "p95_ms", "p99_ms", "p999_ms",
                                    "rps", "k6_rps", "served_ratio", "error_rate_5xx",
                                    "cpu_avg_cores", "pg_cpu_cores", "k6_cpu_cores", "mem_max_mb",
                                    "rss_self_max_mb", "k6_dropped", "k6_failed_rate", "gate_pass")}
            for c in curve
        ],
    }
    with open(f"{OUTDIR}/{app}.limit.json", "w") as f:
        json.dump(summary, f, indent=2)
    K.log(f"LIMIT for {app}: {limit} rps (first fail {first_fail}) — {summary['bottleneck_at_first_fail']}")

    K.sh(f"kubectl delete -f apps/{app}/k8s.yaml --ignore-not-found")
    K.log(f"teardown langperf-{app} done")


if __name__ == "__main__":
    main(sys.argv[1])
