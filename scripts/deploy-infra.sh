#!/usr/bin/env bash
# Deploy the shared benchmark infra: namespace, postgres, seed, k6 script configmap, podmonitor.
set -euo pipefail
cd "$(dirname "$0")/.."
NS=langperf

kubectl apply -f k8s/namespace.yaml

kubectl -n $NS create configmap langperf-seed \
  --from-file=seed.sql=k8s/seed.sql --dry-run=client -o yaml | kubectl apply -f -
kubectl -n $NS create configmap langperf-k6-script \
  --from-file=loadtest.js=k6/loadtest.js --dry-run=client -o yaml | kubectl apply -f -

echo "== postgres =="
kubectl apply -f k8s/postgres.yaml
kubectl -n $NS rollout status statefulset/postgres --timeout=300s

echo "== seeding (idempotent, wipes + reseeds) =="
kubectl -n $NS delete job seed --ignore-not-found --wait=false
kubectl apply -f k8s/seed-job.yaml
kubectl -n $NS wait --for=condition=complete job/seed --timeout=900s

echo "== row counts =="
kubectl -n $NS exec statefulset/postgres -- \
  psql -U bench -d bench -tAc "SELECT 'users='||count(*) FROM users UNION ALL SELECT 'posts='||count(*) FROM posts UNION ALL SELECT 'likes='||count(*) FROM likes"

echo "== podmonitor (app metric scraping) =="
kubectl apply -f k8s/podmonitor.yaml
echo "infra ready"
