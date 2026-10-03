#!/usr/bin/env bash
# Import dashboards/langperf-dashboard.json into the cluster's Grafana.
# Uses the admin password from the helm secret; never prints it.
set -euo pipefail
GRAFANA=${GRAFANA:-http://192.168.1.173}
DASH_FILE=${1:-dashboards/langperf-dashboard.json}

PASS=$(kubectl -n observability get secret kube-prom-stack-grafana -o jsonpath='{.data.admin-password}' | base64 -d)
USER=$(kubectl -n observability get secret kube-prom-stack-grafana -o jsonpath='{.data.admin-user}' | base64 -d)

# resolve the prometheus datasource uid
DS_UID=$(curl -s -u "$USER:$PASS" "$GRAFANA/api/datasources" | python3 -c "
import json,sys
ds=json.load(sys.stdin)
p=[d for d in ds if d['type']=='prometheus']
print(p[0]['uid'] if p else '')
")
[ -n "$DS_UID" ] || { echo "no prometheus datasource found"; exit 1; }
echo "prometheus datasource uid: $DS_UID"

python3 - "$DASH_FILE" "$DS_UID" <<'EOF'
import json, sys
path, uid = sys.argv[1], sys.argv[2]
with open(path) as f:
    dash = json.load(f)
dash.pop("__inputs_note", None)
for tv in dash.get("templating", {}).get("list", []):
    if tv.get("name") == "datasource":
        tv["current"] = {"text": "Prometheus", "value": uid}
dash["templating"] = {"list": dash.get("templating", {}).get("list", [])}
payload = {"dashboard": dash, "overwrite": True, "message": "langperf benchmark"}
with open("/tmp/langperf-dash-payload.json", "w") as f:
    json.dump(payload, f)
print("payload ready")
EOF

curl -s -u "$USER:$PASS" -X POST \
  -H 'Content-Type: application/json' \
  --data-binary @/tmp/langperf-dash-payload.json \
  "$GRAFANA/api/dashboards/db"
echo
rm -f /tmp/langperf-dash-payload.json
echo "imported: $GRAFANA/d/langperf-benchmark"
