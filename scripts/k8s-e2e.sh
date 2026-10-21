#!/usr/bin/env bash
# End-to-end on Kubernetes, including the rollback drill. Runs in CI on every PR and the
# same way on your laptop:
#
#   scripts/cluster.sh up
#   docker build -t shortly:e2e .
#   IMAGE=shortly:e2e scripts/k8s-e2e.sh
#
# What it proves:
#   0. the digest-mode render (how releases deploy) passes API-server validation
#   1. the chart deploys under the "restricted" Pod Security Standard, migrations run as a hook
#   2. the app works in-cluster (readiness, create → redirect, metrics)
#   3. the SAME image promotes to prod: autoscaled (HPA, min 2) + a PodDisruptionBudget
#   4. a broken release is rolled back automatically, with ZERO failed requests meanwhile
#   5. a manual `helm rollback` restores a previous revision
#   6. under load, the HPA adds ready pods (scripts/hpa-drill.sh; skip with HPA_DRILL=0)
#   7. monitoring works end to end: scrape, rules, dashboard queries, and a real alert routed
#      by Alertmanager (scripts/monitoring-check.sh; skipped when the stack isn't installed)
set -euo pipefail

IMAGE="${IMAGE:?set IMAGE, e.g. IMAGE=shortly:e2e}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PF_PIDS=()

pass() { echo "ok   $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

on_exit() {
  status=$?
  for pid in "${PF_PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
  if [[ $status -ne 0 ]]; then
    echo "--- diagnostics ---"
    for ns in staging prod; do
      kubectl -n "$ns" get pods,jobs -o wide || true
      kubectl -n "$ns" get events --sort-by=.lastTimestamp | tail -20 || true
      kubectl -n "$ns" logs -l app.kubernetes.io/name=shortly --all-containers --tail=30 || true
      helm -n "$ns" history shortly || true
    done
  fi
  exit "$status"
}
trap on_exit EXIT

# Port-forward a service to a local port; wait until it answers.
forward() {
  local ns=$1 port=$2
  kubectl -n "$ns" port-forward svc/shortly "$port:80" >/dev/null 2>&1 &
  PF_PIDS+=($!)
  for _ in $(seq 1 30); do
    curl -sf "http://127.0.0.1:$port/healthz" >/dev/null 2>&1 && return 0
    sleep 1
  done
  fail "port-forward to $ns never became ready"
}

# GET a path through the Service from INSIDE the cluster (kube-proxy load-balancing, like a
# real client). Port-forward can't be used for this: it pins one pod and dies with it.
# Uses busybox wget in the Postgres pod; exit 0 only on a 2xx.
in_cluster() { kubectl -n "$1" exec postgres-0 -- wget -q -O /dev/null -T 2 "http://shortly$2" 2>/dev/null; }

revision() { helm -n "$1" status shortly -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])'; }
deployed_image() { kubectl -n "$1" get deploy shortly -o jsonpath='{.spec.template.spec.containers[0].image}'; }

# ---------------------------------------------------------------- 0. digest-mode render
# Releases deploy by digest, but this test deploys a local image by tag. Render the digest
# path too and let the real API server validate it (server-side dry run). JSON-schema tools
# like kubeconform don't check label syntax; the API server does. (v0.1.0 shipped a label
# "sha256:..." that only the API server rejected.)
fake_digest="sha256:$(printf '0%.0s' {1..64})"
for ns in staging prod; do
  helm template shortly "$ROOT/deploy/helm/shortly" -n "$ns" -f "$ROOT/deploy/envs/$ns.yaml" \
    --set image.repository=ghcr.io/statsguy/shortly --set "image.digest=$fake_digest" \
    | kubectl -n "$ns" apply --dry-run=server -f - >/dev/null \
    || fail "API server rejected the digest-mode render for $ns"
done
pass "digest-mode render accepted by the API server (server-side dry run)"

# ---------------------------------------------------------------- 1-2. staging
"$ROOT/scripts/deploy.sh" staging "$IMAGE" >/dev/null
pass "staging deployed under restricted Pod Security (revision $(revision staging))"

forward staging 18080
S=http://127.0.0.1:18080
[[ "$(curl -s -o /dev/null -w '%{http_code}' $S/readyz)" == 200 ]] || fail "staging /readyz"
code=$(curl -sf -X POST $S/links -H 'content-type: application/json' \
  -d '{"url":"https://example.com/k8s"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["code"])')
[[ "$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "$S/$code")" == "307 https://example.com/k8s" ]] \
  || fail "staging redirect"
curl -sf $S/metrics | grep -q 'links_created_total 1.0' || fail "staging metrics"
pass "staging: migrated, ready, create → 307, metrics"

# ---------------------------------------------------------------- 3. promote to prod
"$ROOT/scripts/deploy.sh" prod "$IMAGE" >/dev/null
ready=$(kubectl -n prod get deploy shortly -o jsonpath='{.status.readyReplicas}')
[[ "$ready" == 2 ]] || fail "prod has $ready ready replicas, want 2"
kubectl -n prod get pdb shortly >/dev/null || fail "prod PodDisruptionBudget missing"
kubectl -n prod get hpa shortly >/dev/null || fail "prod HorizontalPodAutoscaler missing"
[[ "$(deployed_image prod)" == "$(deployed_image staging)" ]] || fail "prod runs a different image than staging"
pass "promoted the same image to prod: 2/2 ready, PDB and HPA present"

in_cluster prod /readyz || fail "prod not reachable through its Service"
good_rev=$(revision prod)
good_image=$(deployed_image prod)

# ---------------------------------------------------------------- 4. automatic rollback
# A bad config value makes the new pods fail at boot (Settings validation). They never
# become ready, so the rollout can't progress; maxUnavailable=0 keeps the old pods serving;
# helm times out and rolls back. Meanwhile we hit prod's Service and count failures.
errors_file=$(mktemp)
(
  errors=0
  end=$((SECONDS + 75))
  while ((SECONDS < end)); do
    in_cluster prod /readyz || errors=$((errors + 1))
    sleep 0.2
  done
  echo "$errors" >"$errors_file"
) &
load_pid=$!

if DEPLOY_TIMEOUT=60s "$ROOT/scripts/deploy.sh" prod "$IMAGE" \
  --set config.logLevel=NOT_A_LEVEL --set migrations.enabled=false >/dev/null 2>&1; then
  fail "broken release was accepted"
fi
wait "$load_pid"
errors=$(cat "$errors_file")

[[ "$(deployed_image prod)" == "$good_image" ]] || fail "prod not back on the good image"
status=$(helm -n prod status shortly -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["status"])')
[[ "$status" == deployed ]] || fail "release status is $status"
[[ "$errors" == 0 ]] || fail "$errors failed requests during the bad rollout"
pass "broken release auto-rolled back (now revision $(revision prod)); 0 failed requests during it"

# ---------------------------------------------------------------- 5. manual rollback
"$ROOT/scripts/deploy.sh" prod "$IMAGE" --set config.logLevel=DEBUG >/dev/null
[[ "$(kubectl -n prod get deploy shortly -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="SHORTLY_LOG_LEVEL")].value}')" == DEBUG ]] \
  || fail "DEBUG release didn't apply"
helm -n prod rollback shortly "$good_rev" --wait --timeout 2m >/dev/null
level=$(kubectl -n prod get deploy shortly -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="SHORTLY_LOG_LEVEL")].value}')
[[ "$level" == INFO ]] || fail "rollback didn't restore config (log level $level)"
# The rollback replaced every pod; wait for the rollout, then check via the Service.
kubectl -n prod rollout status deploy/shortly --timeout=90s >/dev/null
in_cluster prod /readyz || fail "prod unhealthy after rollback"
pass "manual helm rollback to revision $good_rev restored the previous release"

# ---------------------------------------------------------------- 6. autoscaling
if [[ "${HPA_DRILL:-1}" != 0 ]]; then
  NS=prod "$ROOT/scripts/hpa-drill.sh"
fi

# ---------------------------------------------------------------- 7. monitoring
if kubectl get crd prometheusrules.monitoring.coreos.com >/dev/null 2>&1; then
  "$ROOT/scripts/monitoring-check.sh"
else
  echo "skip monitoring checks: kube-prometheus-stack not installed (MONITORING=0?)"
fi

echo "k8s e2e passed"
