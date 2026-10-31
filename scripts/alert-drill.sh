#!/usr/bin/env bash
# Alert drill: a real outage travels Prometheus -> Alertmanager and is routed to the right
# receiver. staging has one fixed replica and no HPA, so scaling it to zero is a clean,
# reversible outage; ShortlyNoAvailablePods must reach Alertmanager, routed to "staging".
# staging is restored afterwards, on failure too.
#
#   scripts/alert-drill.sh        # about 90 s, most of it the alert's `for: 1m`
#
# scripts/k8s-e2e.sh starts it in the background while the prod drills run (they never
# touch staging), so its wait costs CI no time. scripts/monitoring-check.sh runs it too,
# unless ALERT_DRILL=0.
set -euo pipefail
# shellcheck source=scripts/lib/platform.sh
source "$(dirname "$0")/lib/platform.sh"
export STAGING_NS

pass() { echo "ok   $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }
api() { kubectl get --raw "/api/v1/namespaces/monitoring/services/http:$1/proxy$2"; }

restore() { kubectl -n "$STAGING_NS" scale deploy/shortly --replicas=1 >/dev/null 2>&1 || true; }
trap restore EXIT
trap 'exit 143' TERM # so a killed drill still restores staging (EXIT trap)

kubectl -n monitoring get prometheus >/dev/null 2>&1 \
  || fail "kube-prometheus-stack is not installed (scripts/cluster.sh up)"

kubectl -n "$STAGING_NS" scale deploy/shortly --replicas=0 >/dev/null
start=$SECONDS receiver=""
while ((SECONDS - start < 300)); do
  receiver=$(api kps-alertmanager:9093 /api/v2/alerts | python3 -c '
import json, os, sys
for a in json.load(sys.stdin):
    if a["labels"].get("alertname") == "ShortlyNoAvailablePods" and a["labels"].get("namespace") == os.environ["STAGING_NS"]:
        print(",".join(r["name"] for r in a["receivers"])); break')
  [[ -n "$receiver" ]] && break
  sleep 5
done
[[ -n "$receiver" ]] || fail "ShortlyNoAvailablePods for staging never reached Alertmanager"
[[ "$receiver" == staging ]] || fail "staging alert routed to '$receiver', want 'staging'"
took=$((SECONDS - start))
restore
kubectl -n "$STAGING_NS" rollout status deploy/shortly --timeout=120s >/dev/null
trap - EXIT
pass "staging scaled to 0: ShortlyNoAvailablePods reached Alertmanager after ${took}s, routed to 'staging'; restored"
