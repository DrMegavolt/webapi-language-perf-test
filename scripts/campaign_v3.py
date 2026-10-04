#!/usr/bin/env python3
"""In-cluster campaign runner (phase 3 v3: POOL_SIZE=16 + OTel tracing).

Runs INSIDE the orchestrator container. For each stack:
  1. deploy with POOL_SIZE (env, default 16)
  2. fresh seed
  3. adaptive limit search (ladder from START_LEVEL, bisection, 3 confirms)
     -> /results/limits-v3/<app>.limit.json
  4. trace-capture run: 500 rps x 180s at 100% tracing -> window recorded in
     /results/traces-window.json (spans flow to Tempo continuously)
  5. teardown

Crash-resume: skips stacks with /results/progress/<app>.done unless FORCE=1.
Progress log: /results/progress.txt (and stdout for kubectl logs).
"""
import json
import os
import sys
import time

os.environ.setdefault("POOL_SIZE", "16")
os.environ.setdefault("START_LEVEL", "5000")
os.environ.setdefault("MAX_LEVEL", "32000")
os.environ.setdefault("OUTDIR", "/results/limits-v3")

sys.path.insert(0, "/langperf/scripts")
import k6run_lib as K  # noqa: E402
import limit_search2 as LS  # noqa: E402

APPS = os.environ.get("APPS", "go,rust,bun,express,express-bun,nestjs,python,rails,dotnet").split(",")
RESULTS = "/results"
PROGRESS = os.path.join(RESULTS, "progress")
TRACE_RATE = int(os.environ.get("TRACE_RATE", "500"))
TRACE_DURATION = int(os.environ.get("TRACE_DURATION", "180"))


def note(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(os.path.join(RESULTS, "progress.txt"), "a") as f:
        f.write(line + "\n")


def trace_run(app):
    """Clean-window run at moderate load while tracing is 100% on."""
    os.makedirs(os.path.join(RESULTS, "traces"), exist_ok=True)
    start = K.now_ts()
    rec = K.run_k6(app, TRACE_RATE, "trace", os.path.join(RESULTS, "traces"), duration_s=TRACE_DURATION)
    end = K.now_ts()
    windows_path = os.path.join(RESULTS, "traces-window.json")
    windows = {}
    if os.path.exists(windows_path):
        windows = json.load(open(windows_path))
    windows[app] = {"start": start, "end": end, "rate": TRACE_RATE,
                    "p99_ms": rec.get("p99_ms"), "served_ratio": rec.get("served_ratio")}
    with open(windows_path, "w") as f:
        json.dump(windows, f, indent=2)
    note(f"{app}: trace window {start} -> {end}")


def main():
    os.makedirs(PROGRESS, exist_ok=True)
    force = os.environ.get("FORCE") == "1"
    K.refresh_script_configmap()
    note(f"campaign start: APPS={','.join(APPS)} POOL_SIZE={os.environ['POOL_SIZE']} "
         f"START_LEVEL={os.environ['START_LEVEL']}")

    for app in APPS:
        app = app.strip()
        marker = os.path.join(PROGRESS, f"{app}.done")
        if os.path.exists(marker) and not force:
            note(f"skip {app} (already done)")
            continue
        try:
            note(f"=== {app}: deploy (POOL_SIZE={os.environ['POOL_SIZE']}) ===")
            LS.deploy(app)
            K.reseed()

            note(f"=== {app}: limit search ===")
            LS.main(app)

            note(f"=== {app}: trace-capture run ({TRACE_RATE} rps x {TRACE_DURATION}s) ===")
            trace_run(app)

            K.sh(f"kubectl delete -f apps/{app}/k8s.yaml --ignore-not-found")
            open(marker, "w").write("done")
            note(f"=== {app}: DONE ===")
        except Exception as e:
            note(f"!!! {app} FAILED: {e}")
            K.sh(f"kubectl delete -f apps/{app}/k8s.yaml --ignore-not-found")

    note("campaign complete")


if __name__ == "__main__":
    main()
