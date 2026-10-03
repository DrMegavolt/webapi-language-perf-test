#!/usr/bin/env python3
"""Confirm one stack's limit with a FRESH-SEEDED database: reseed, then 3 runs at
the given level; merge results into results/limits/<app>.limit.json (replacing any
previous confirm entries).

Usage: python3 scripts/confirm_at_limit.py <app> <level>
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import limit_search as ls  # noqa: E402


def main(app, level):
    out = f"{ls.OUTDIR}/{app}.limit.json"
    summary = json.load(open(out))

    ls.log(f"deploy langperf-{app}")
    ls.sh(f"kubectl apply -f apps/{app}/k8s.yaml")
    if ls.sh(f"kubectl -n {ls.NS} rollout status deploy/langperf-{app} --timeout=300s").returncode != 0:
        raise RuntimeError("rollout failed")
    time.sleep(5)
    ls.reseed()

    confirms = []
    for i in range(3):
        time.sleep(20)
        confirms.append(ls.run_k6(app, level, f"confirm{i+1}"))

    # replace old confirm curve entries, keep everything else
    curve = [c for c in summary["curve"] if not str(c.get("label", "")).startswith("confirm")]
    for c in confirms:
        curve.append({k: c.get(k) for k in (
            "offered_rps", "label", "p50_ms", "p95_ms", "p99_ms", "p999_ms", "rps",
            "k6_rps", "served_ratio", "error_rate_5xx", "cpu_avg_cores", "pg_cpu_cores",
            "k6_cpu_cores", "k6_dropped", "k6_failed_rate", "gate_pass")})
    summary["curve"] = curve
    summary["confirm_p99_ms"] = [c.get("p99_ms") for c in confirms]
    summary["confirm_err"] = [c.get("error_rate_5xx") for c in confirms]
    summary["note"] = f"confirms re-run on fresh-seeded DB at {level} rps"
    with open(out, "w") as f:
        json.dump(summary, f, indent=2)

    ls.sh(f"kubectl delete -f apps/{app}/k8s.yaml --ignore-not-found")
    ls.log(f"{app} @ {level}: p99s={[c.get('p99_ms') for c in confirms]} "
           f"pass={[c['gate_pass'] for c in confirms]}")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]))
