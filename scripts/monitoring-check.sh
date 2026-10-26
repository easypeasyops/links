#!/usr/bin/env bash
# Monitoring, end to end, inside the kind cluster. Run by scripts/k8s-e2e.sh (and so by CI)
# after the deploys, or on its own:  scripts/monitoring-check.sh
#
# Proves, in order:
#   1. Prometheus scrapes the prod pods through the chart's ServiceMonitor
#   2. the chart's PrometheusRules are loaded and evaluate without errors
#   3. the SLO recording rules produce series for prod
#   4. every query on the SLO dashboard is valid PromQL
#   5. a real alert travels Prometheus -> Alertmanager and is routed to the right receiver
#      (scripts/alert-drill.sh: staging is scaled to zero for about 90 s, then restored;
#      ALERT_DRILL=0 skips it)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/lib/namespaces.sh
source "$(dirname "$0")/lib/namespaces.sh"
export PROD_NS
pass() { echo "ok   $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

# The Prometheus and Alertmanager HTTP APIs, through the API server's service proxy: no
# port-forward to babysit, and their images are distroless (no shell or wget to exec).
api() { kubectl get --raw "/api/v1/namespaces/monitoring/services/http:$1/proxy$2"; }
prom() { api kps-prometheus:9090 "$1"; }
query() {
  prom "/api/v1/query?query=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$1")"
}
count() { python3 -c 'import json, sys; d = json.load(sys.stdin); print(len(d["data"]["result"]) if d["status"] == "success" else -1)'; }

kubectl -n monitoring get prometheus >/dev/null 2>&1 \
  || fail "kube-prometheus-stack is not installed (scripts/cluster.sh up)"

# ---------------------------------------------------------------- 1. scrape
n=0
for _ in $(seq 1 40); do
  n=$(query "up{job=\"shortly\", namespace=\"$PROD_NS\"} == 1" | count)
  ((n >= 2)) && break
  sleep 3
done
((n >= 2)) || fail "Prometheus scrapes $n healthy prod pods, want at least 2 (ServiceMonitor?)"
pass "Prometheus scrapes $n prod pods through the ServiceMonitor (job=shortly)"

# ---------------------------------------------------------------- 2. rules
want="shortly-health slo-create-availability slo-create-latency slo-redirect-availability slo-redirect-latency"
for _ in $(seq 1 30); do
  state=$(prom /api/v1/rules | python3 -c '
import json, sys
groups = [g for g in json.load(sys.stdin)["data"]["groups"] if g["name"].startswith(("slo-", "shortly-"))]
bad = [r["name"] for g in groups for r in g["rules"] if r.get("health") != "ok"]
print(" ".join(sorted(g["name"] for g in groups)) + "|" + " ".join(bad))')
  [[ "${state%%|*}" == "$want" && -z "${state#*|}" ]] && break
  sleep 3
done
[[ "${state%%|*}" == "$want" ]] || fail "rule groups loaded: '${state%%|*}', want '$want'"
[[ -z "${state#*|}" ]] || fail "rules not evaluating cleanly: ${state#*|}"
pass "chart's PrometheusRules loaded: 5 groups, every rule health=ok"

# ---------------------------------------------------------------- 3. recording rules
# A little traffic so the 5m rates exist even when the HPA drill was skipped.
for _ in $(seq 1 20); do
  kubectl -n "$PROD_NS" exec postgres-0 -- wget -q -O /dev/null -T 2 http://shortly/no-such-code 2>/dev/null || true
done
n=0
for _ in $(seq 1 40); do
  n=$(query "slo:sli_error:ratio_rate5m{namespace=\"$PROD_NS\", slo=\"redirect-availability\"}" | count)
  ((n == 1)) && break
  sleep 3
done
((n == 1)) || fail "no slo:sli_error:ratio_rate5m series for prod"
remaining=$(query "slo:error_budget_remaining:ratio{namespace=\"$PROD_NS\", slo=\"redirect-availability\"}" \
  | python3 -c 'import json, sys; r = json.load(sys.stdin)["data"]["result"]; print(r[0]["value"][1] if r else "none")')
pass "SLO recording rules evaluate for prod (redirect budget remaining: $remaining)"

# ---------------------------------------------------------------- 4. dashboard queries
python3 - "$ROOT/deploy/helm/shortly/dashboards" <<'PY' || fail "a dashboard query is invalid"
import json, os, pathlib, subprocess, sys, urllib.parse

for path in sorted(pathlib.Path(sys.argv[1]).glob("*.json")):
    exprs = [t["expr"] for p in json.loads(path.read_text())["panels"] for t in p.get("targets", [])]
    for expr in exprs:
        q = expr.replace("$namespace", os.environ["PROD_NS"]).replace("$__rate_interval", "5m")
        url = "/api/v1/namespaces/monitoring/services/http:kps-prometheus:9090/proxy/api/v1/query?query="
        out = subprocess.run(["kubectl", "get", "--raw", url + urllib.parse.quote(q)],
                             capture_output=True, text=True)
        if out.returncode != 0 or json.loads(out.stdout).get("status") != "success":
            sys.exit(f"{path.name}: {q}\n{out.stdout}{out.stderr}")
    print(f"ok   {path.name}: all {len(exprs)} dashboard queries are valid PromQL")
PY

# ---------------------------------------------------------------- 5. alert end to end
# scripts/alert-drill.sh. k8s-e2e.sh runs it in the background earlier and sets ALERT_DRILL=0.
if [[ "${ALERT_DRILL:-1}" != 0 ]]; then
  "$ROOT/scripts/alert-drill.sh"
fi
