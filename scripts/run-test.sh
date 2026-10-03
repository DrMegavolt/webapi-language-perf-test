#!/usr/bin/env bash
# Run the full benchmark cycle for ONE app:
#   deploy -> k6 baseline (RATE rps, DURATION) -> collect -> teardown
#   RAMP=1 also runs the stress ramp (200->1800 rps over 4 min) afterwards.
# Usage: scripts/run-test.sh <app>   (env: RATE=200 DURATION=300s RAMP=1)
set -euo pipefail
cd "$(dirname "$0")/.."

APP=${1:?usage: run-test.sh <app>}
RATE=${RATE:-200}
DURATION=${DURATION:-300s}
RAMP=${RAMP:-1}
RAMP_DURATION=${RAMP_DURATION:-240s}
NS=langperf
PROM=${PROM:-http://192.168.1.174:9090}

ts() { date -u +%H:%M:%S; }

# always run the current script version, never a stale configmap
kubectl -n $NS create configmap langperf-k6-script \
  --from-file=loadtest.js=k6/loadtest.js --dry-run=client -o yaml | kubectl apply -f - >/dev/null

run_k6() {  # $1 = mode, $2 = duration, $3 = results basename
  local MODE=$1 DUR=$2 BASENAME=$3
  kubectl -n $NS delete job langperf-k6 --ignore-not-found >/dev/null 2>&1
  sed "s|__BASE_URL__|http://langperf-$APP.$NS.svc.cluster.local|; s|__RATE__|$RATE|; s|__DURATION__|$DUR|; s|__MODE__|$MODE|" \
    k8s/k6-job.yaml | kubectl apply -f - >/dev/null
  local START
  START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  if ! kubectl -n $NS wait --for=condition=complete job/langperf-k6 --timeout=1200s >/dev/null 2>&1; then
    echo "K6 JOB FAILED for $APP (mode=$MODE); last log lines:" >&2
    kubectl -n $NS logs job/langperf-k6 --tail=25 >&2 || true
    return 1
  fi
  kubectl -n $NS logs job/langperf-k6 > results/$BASENAME.k6.log 2>&1 || true
  grep 'K6SUMMARY ' results/$BASENAME.k6.log | sed 's/^.*K6SUMMARY //' > results/$BASENAME.k6.json || true
  echo "$START"
}

echo "== [$(ts)] deploy langperf-$APP =="
kubectl apply -f apps/$APP/k8s.yaml
kubectl -n $NS rollout status deploy/langperf-$APP --timeout=300s

echo "== smoke =="
kubectl -n $NS run smoke-$APP --image=curlimages/curl:8.10.1 --rm -i -q --restart=Never --command -- \
  sh -c "curl -s -m 10 http://langperf-$APP.$NS/healthz && echo && \
         curl -s -m 10 'http://langperf-$APP.$NS/feed?page=1' | head -c 160 && echo && \
         curl -s -m 10 http://langperf-$APP.$NS/posts/1 | head -c 160 && echo && \
         curl -s -m 10 http://langperf-$APP.$NS/metrics | grep -cE 'http_request_duration_seconds_bucket|app_memory_rss_bytes' || true"

echo "== [$(ts)] k6 baseline: $RATE rps constant arrival, $DURATION =="
START=$(run_k6 baseline "$DURATION" "$APP")
sleep 15   # let the final scrape land in prometheus
END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "== collect baseline from prometheus =="
python3 scripts/collect_results.py --app "$APP" --start "$START" --end "$END" \
  --prom "$PROM" --out "results/$APP.prom.json"

if [ "$RAMP" = "1" ]; then
  echo "== [$(ts)] k6 ramp: 200->1800 rps, $RAMP_DURATION =="
  sleep 30  # cool down
  RSTART=$(run_k6 ramp "$RAMP_DURATION" "$APP.ramp")
  sleep 15
  REND=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  echo "== collect ramp from prometheus =="
  python3 scripts/collect_results.py --app "$APP" --start "$RSTART" --end "$REND" \
    --prom "$PROM" --out "results/$APP.ramp.prom.json" --mode ramp
fi

echo "== [$(ts)] teardown langperf-$APP =="
kubectl delete -f apps/$APP/k8s.yaml --ignore-not-found
echo "done: results/$APP.prom.json (+ ramp)"
