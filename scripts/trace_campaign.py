#!/usr/bin/env python3
"""Trace campaign v2: capture per-stack traces, exporting DIRECTLY to Tempo
(bypassing the flaky shared gateway whose DNS failures drop batches).

Phase 1: all 9 stacks (manual SDK spans) at 500 rps x 180s.
Phase 2: auto-instrumented runs (OTel operator pod-injection) for
         python/express/nestjs/dotnet with OTEL_SERVICE_NAME=<app>-auto.
"""
import json
import os
import sys
import time

sys.path.insert(0, "/langperf/scripts")
import k6run_lib as K  # noqa: E402

TEMPO = "http://tempo.observability.svc.cluster.local:4318"
POOL_SIZE = os.environ.get("POOL_SIZE", "16")
RATE = int(os.environ.get("TRACE_RATE", "500"))
DURATION = int(os.environ.get("TRACE_DURATION", "180"))
RESULTS = "/results/trace-campaign"
MANUAL = ["go", "rust", "bun", "express", "express-bun", "nestjs", "python", "rails", "dotnet"]
AUTO = {"python": "inject-python", "express": "inject-nodejs",
        "nestjs": "inject-nodejs", "dotnet": "inject-dotnet"}


def note(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open("/results/trace-campaign-progress.txt", "a") as f:
        f.write(line + "\n")


def deploy(app, service_name, auto=False):
    K.sh(f"kubectl apply -f apps/{app}/k8s.yaml")
    r = K.sh(f"kubectl -n {K.NS} set env deploy/langperf-{app} "
             f"POOL_SIZE={POOL_SIZE} OTEL_SERVICE_NAME={service_name} "
             f"OTEL_EXPORTER_OTLP_ENDPOINT={TEMPO}")
    if r.returncode != 0:
        raise RuntimeError(f"set env failed: {r.stderr[:200]}")
    if auto:
        ann = f'instrumentation.opentelemetry.io/{AUTO[app]}=langperf-auto'
        r = K.sh(f'kubectl -n {K.NS} annotate deploy/langperf-{app} {ann} --overwrite')
        if r.returncode != 0:
            raise RuntimeError(f"annotate failed: {r.stderr[:200]}")
    if K.sh(f"kubectl -n {K.NS} rollout status deploy/langperf-{app} --timeout=300s").returncode != 0:
        raise RuntimeError("rollout failed")
    env = K.sh(f"kubectl -n {K.NS} get deploy langperf-{app} -o "
               f"jsonpath={{.spec.template.spec.containers[0].env}}").stdout
    if "OTEL_SERVICE_NAME" not in env or "tempo.observability" not in env:
        raise RuntimeError("otel env missing after set")
    time.sleep(5)


def run_stack(app, label, service_name, auto=False):
    deploy(app, service_name, auto)
    K.reseed()
    K.run_k6(app, RATE, label, os.path.join(RESULTS, label), duration_s=DURATION)
    K.sh(f"kubectl delete -f apps/{app}/k8s.yaml --ignore-not-found")


def main():
    os.makedirs(RESULTS, exist_ok=True)
    K.refresh_script_configmap()
    windows = {}

    note("=== phase 1: manual SDK spans, direct to Tempo ===")
    for app in MANUAL:
        note(f"{app}: capture run")
        start = K.now_ts()
        rec = run_stack(app, "manual", f"langperf-{app}")
        end = K.now_ts()
        windows[app] = {"start": start, "end": end, "rate": RATE,
                        "p99_ms": rec.get("p99_ms"), "served_ratio": rec.get("served_ratio")}
        json.dump(windows, open(os.path.join(RESULTS, "windows-manual.json"), "w"), indent=2)

    note("=== phase 2: operator auto-instrumentation (pod injection) ===")
    for app, inject in AUTO.items():
        note(f"{app}: capture run (auto-instrumented)")
        start = K.now_ts()
        rec = run_stack(app, "auto", f"langperf-{app}-auto", auto=True)
        end = K.now_ts()
        windows[f"{app}-auto"] = {"start": start, "end": end, "rate": RATE,
                                  "p99_ms": rec.get("p99_ms"), "served_ratio": rec.get("served_ratio")}
        json.dump(windows, open(os.path.join(RESULTS, "windows-auto.json"), "w"), indent=2)

    note("trace campaign complete")


if __name__ == "__main__":
    main()
