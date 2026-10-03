#!/usr/bin/env python3
"""Shared runner for langperf load tests: k6 job management + collection."""
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

NS = "langperf"
PROM = "http://192.168.1.174:9090"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def refresh_script_configmap():
    sh("kubectl -n langperf create configmap langperf-k6-script "
       "--from-file=loadtest.js=k6/loadtest.js --dry-run=client -o yaml | kubectl apply -f -")


def reseed():
    sh(f"kubectl -n {NS} delete job seed --ignore-not-found")
    sh(f"kubectl apply -f k8s/seed-job.yaml")
    for _ in range(120):
        if sh(f"kubectl -n {NS} wait --for=condition=complete job/seed --timeout=10s").returncode == 0:
            log("reseeded (50k/500k/2.2M fresh)")
            return
        time.sleep(5)
    raise RuntimeError("reseed timed out")


def run_k6(app, rate, label, outdir, duration_s=150):
    """Run one k6 job; writes prom+k6 files into outdir; returns merged record."""
    os.makedirs(outdir, exist_ok=True)
    sh(f"kubectl -n {NS} delete job langperf-k6 --ignore-not-found")
    start = now_ts()
    sed = (
        f"sed 's|__BASE_URL__|http://langperf-{app}.{NS}.svc.cluster.local|; "
        f"s|__RATE__|{rate}|; s|__DURATION__|{duration_s}s|; s|__MODE__|baseline|' "
        f"k8s/k6-job.yaml | kubectl apply -f -"
    )
    r = sh(sed)
    if r.returncode != 0:
        raise RuntimeError(f"kubectl apply k6 failed: {r.stderr[:300]}")
    done = sh(f"kubectl -n {NS} wait --for=condition=complete job/langperf-k6 --timeout=1500s")
    ok = done.returncode == 0
    end = now_ts()
    logs = sh(f"kubectl -n {NS} logs job/langperf-k6").stdout
    base = f"{outdir}/{app}.L{rate}.{label}"
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
    try:
        rec = json.load(open(prom_file))
    except Exception:
        rec = {"app": app, "collect_error": c.stderr[-300:]}

    w = f"{max(120, duration_s + 30)}s"
    served = k6.get("rps") or 0
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
            "served_ratio": round(served / rate, 3) if rate else 0,
            "pg_cpu_cores": round(prom_one(
                'sum(rate(container_cpu_usage_seconds_total{namespace="langperf",pod="postgres-0",container!=""}['
                + w + "]))", end), 3),
            "k6_cpu_cores": round(prom_one(
                'sum(rate(container_cpu_usage_seconds_total{namespace="langperf",pod=~"langperf-k6-.*",container!=""}['
                + w + "]))", end), 3),
        }
    )
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
        f"rate={rate:>6} {label:<9} -> p99={rec.get('p99_ms')}ms err={rec.get('error_rate_5xx')} "
        f"served={served:.0f} ({rec['served_ratio']*100:.0f}%) dropped={rec.get('k6_dropped')} "
        f"appcpu={rec.get('cpu_avg_cores')} pgcpu={rec.get('pg_cpu_cores')} "
        f"GATE={'PASS' if rec['gate_pass'] else 'FAIL'}"
    )
    return rec
