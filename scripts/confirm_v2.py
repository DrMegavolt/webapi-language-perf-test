#!/usr/bin/env python3
"""Confirm one stack's v2 limit with a fresh-seeded DB: 3 runs at LEVEL, merged
into results/limits-v2/<app>.limit.json. Usage: confirm_v2.py <app> <level>"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import limit_search2 as LS  # noqa: E402


def main(app, level):
    out = f"{LS.OUTDIR}/{app}.limit.json"
    summary = json.load(open(out))

    LS.K.refresh_script_configmap()
    LS.deploy(app)
    LS.K.reseed()

    confirms = []
    for i in range(1, 4):
        time.sleep(20)
        confirms.append(LS.K.run_k6(app, level, f"confirm{i}", LS.OUTDIR, duration_s=LS.DURATION_S))

    curve = [c for c in summary["curve"] if not str(c.get("label", "")).startswith("confirm")]
    for c in confirms:
        curve.append({k: c.get(k) for k in (
            "offered_rps", "label", "p50_ms", "p95_ms", "p99_ms", "p999_ms", "rps",
            "k6_rps", "served_ratio", "error_rate_5xx", "cpu_avg_cores", "pg_cpu_cores",
            "k6_cpu_cores", "mem_max_mb", "rss_self_max_mb", "k6_dropped",
            "k6_failed_rate", "gate_pass")})
    summary["curve"] = curve
    summary["confirm_p99_ms"] = [c.get("p99_ms") for c in confirms]
    summary["confirm_err"] = [c.get("error_rate_5xx") for c in confirms]
    summary["confirm_mem_max_mb"] = [c.get("mem_max_mb") for c in confirms]
    summary["note"] = f"confirms re-run on fresh-seeded DB at {level} rps"
    with open(out, "w") as f:
        json.dump(summary, f, indent=2)

    LS.K.sh(f"kubectl delete -f apps/{app}/k8s.yaml --ignore-not-found")
    LS.K.log(f"{app} @ {level}: p99s={[c.get('p99_ms') for c in confirms]} "
             f"pass={[c['gate_pass'] for c in confirms]}")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]))
