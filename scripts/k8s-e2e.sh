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
#   3. the SAME image promotes to prod: autoscaled (HPA, min 2) + a PodDisruptionBudget,
#      and the per-client rate limit holds exactly across its replicas
#   4. a broken release is rolled back automatically, with ZERO failed requests meanwhile
#   5. a manual `helm rollback` restores a previous revision
#   6. under load, the HPA adds ready pods (scripts/hpa-drill.sh; skip with HPA_DRILL=0)
#   7. monitoring works end to end: scrape, rules, dashboard queries (scripts/monitoring-check.sh),
#      and a real alert routed by Alertmanager (scripts/alert-drill.sh, run in the background
#      during steps 3-6; both skipped when the stack isn't installed)
#   8. admission refuses shortly images without release provenance, even from hand-run
#      kubectl (scripts/admission-check.sh; skipped when Kyverno isn't installed)
#   9. network policies are enforced: the metrics port and Postgres refuse other pods
#  10. the cleanup CronJob deletes links past their grace period, and only those
set -euo pipefail

IMAGE="${IMAGE:?set IMAGE, e.g. IMAGE=shortly:e2e}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PF_PIDS=()
ALERT_PID="" ALERT_LOG=$(mktemp)

pass() { echo "ok   $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

on_exit() {
  status=$?
  for pid in "${PF_PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
  if [[ -n "$ALERT_PID" ]]; then # still running (or never collected): stop it, show its output
    kill "$ALERT_PID" 2>/dev/null || true
    wait "$ALERT_PID" 2>/dev/null || true # its EXIT trap restores staging
    sed 's/^/     alert drill: /' "$ALERT_LOG"
  fi
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

# Port-forward a service port to a local port; wait until it answers.
forward() {
  local ns=$1 port=$2 svc_port=${3:-80} path=${4:-/healthz}
  kubectl -n "$ns" port-forward svc/shortly "$port:$svc_port" >/dev/null 2>&1 &
  PF_PIDS+=($!)
  for _ in $(seq 1 30); do
    curl -sf "http://127.0.0.1:$port$path" >/dev/null 2>&1 && return 0
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
# "sha256:..." that only the API server rejected.) The real release digest, because the
# admission policy (step 8) refuses any shortly digest without release provenance.
release_digest=$(sed -nE 's/^\| Current release \| v[0-9.]+, `(sha256:[0-9a-f]{64})`.*/\1/p' "$ROOT/docs/RUNBOOK.md")
[[ -n "$release_digest" ]] || fail "no 'Current release' digest in docs/RUNBOOK.md"
for ns in staging prod; do
  helm template shortly "$ROOT/deploy/helm/shortly" -n "$ns" -f "$ROOT/deploy/envs/$ns.yaml" \
    --set image.repository=ghcr.io/statsguy/shortly --set "image.digest=$release_digest" \
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
# /metrics lives on its own port (9100); the app port must not serve it.
[[ "$(curl -s -o /dev/null -w '%{http_code}' $S/metrics)" == 404 ]] || fail "staging serves /metrics on the app port"
forward staging 18081 9100 /metrics
curl -sf http://127.0.0.1:18081/metrics | grep -q 'links_created_total 1.0' || fail "staging metrics"
pass "staging: migrated, ready, create → 307, metrics on port 9100 only"

# ---------------------------------------------------------------- 7a. alert drill, in the background
# It scales staging to zero and waits about 90 s for ShortlyNoAvailablePods to reach
# Alertmanager. Steps 3-6 only touch prod, so it runs alongside them instead of after them.
# Collected in step 7, before anything uses staging again.
monitoring=0
kubectl get crd prometheusrules.monitoring.coreos.com >/dev/null 2>&1 && monitoring=1
if ((monitoring)); then
  "$ROOT/scripts/alert-drill.sh" >"$ALERT_LOG" 2>&1 &
  ALERT_PID=$!
  echo "     alert drill started in the background (staging scaled to 0)"
fi

# ---------------------------------------------------------------- 3. promote to prod
"$ROOT/scripts/deploy.sh" prod "$IMAGE" >/dev/null
ready=$(kubectl -n prod get deploy shortly -o jsonpath='{.status.readyReplicas}')
[[ "$ready" == 2 ]] || fail "prod has $ready ready replicas, want 2"
kubectl -n prod get pdb shortly >/dev/null || fail "prod PodDisruptionBudget missing"
kubectl -n prod get hpa shortly >/dev/null || fail "prod HorizontalPodAutoscaler missing"
[[ "$(deployed_image prod)" == "$(deployed_image staging)" ]] || fail "prod runs a different image than staging"
pass "promoted the same image to prod: 2/2 ready, PDB and HPA present"

in_cluster prod /readyz || fail "prod not reachable through its Service"

# ---------------------------------------------------------------- 3b. rate limit
# One client, through the Service, so kube-proxy spreads the requests over both pods: the
# limit must still be one burst (plus what refills meanwhile), because its state is in
# Postgres. Per-pod counters would let all burst + 5 through.
# X-Forwarded-For picks the client (prod trusts the pod network as kind's stand-in ingress).
create_as() {
  kubectl -n prod exec postgres-0 -- wget -S -O /dev/null -T 3 \
    --header 'content-type: application/json' --header "X-Forwarded-For: $1" \
    --post-data '{"url":"https://example.com/rate"}' http://shortly/links 2>&1 \
    | grep -m1 -oE 'HTTP/1\.[01] [0-9]{3}' | cut -d' ' -f2
}
limits=$(helm -n prod get values shortly --all -o json \
  | python3 -c 'import json,sys; r=json.load(sys.stdin)["config"]["rateLimit"]; print(r["burst"], r["createsPerMinute"])')
read -r burst per_minute <<<"$limits"
# GCRA refills while we send (one token per 60/per_minute s), and each kubectl exec takes
# ~0.1 s, so time the run: allowed must be at least the burst, at most burst + refill.
client="203.0.113.$((RANDOM % 250 + 1))"
created=0 refused=0 t0=$(date +%s.%N)
for _ in $(seq 1 $((burst + 5))); do
  case "$(create_as "$client")" in
    201) created=$((created + 1)) ;;
    429) refused=$((refused + 1)) ;;
  esac
done
max=$(python3 -c "import math,sys; print($burst + math.ceil(($(date +%s.%N) - $t0) * $per_minute / 60))")
((created >= burst)) || fail "rate limit: only $created creates succeeded, the burst is $burst"
((created <= max)) || fail "rate limit: $created creates succeeded, at most $max allowed (burst + refill)"
((refused > 0)) || fail "rate limit: $((burst + 5)) creates from one client, none refused"
[[ "$(create_as "198.51.100.$((RANDOM % 250 + 1))")" == 201 ]] || fail "rate limit: another client was refused too"
pass "rate limit: one client got $created creates (burst $burst, at most $max with refill), then 429s, across $(kubectl -n prod get deploy shortly -o jsonpath='{.status.readyReplicas}') pods; other clients unaffected"

good_rev=$(revision prod)
good_image=$(deployed_image prod)

# ---------------------------------------------------------------- 4. automatic rollback
# A bad config value makes the new pods fail at boot (Settings validation). They never
# become ready, so the rollout can't progress; maxUnavailable=0 keeps the old pods serving;
# helm times out and rolls back. Meanwhile we hit prod's Service and count failures, from
# before the upgrade until the rollback is done.
# 30 s is plenty for the timeout: the new pods crash within seconds of starting, and the
# rollout cannot get unstuck. A longer wait proves nothing more.
errors_file=$(mktemp) stop_file=$(mktemp -u)
(
  errors=0
  end=$((SECONDS + 180)) # a backstop only
  while [[ ! -e "$stop_file" ]] && ((SECONDS < end)); do
    in_cluster prod /readyz || errors=$((errors + 1))
    sleep 0.2
  done
  echo "$errors" >"$errors_file"
) &
load_pid=$!

if DEPLOY_TIMEOUT=30s "$ROOT/scripts/deploy.sh" prod "$IMAGE" \
  --set config.logLevel=NOT_A_LEVEL --set migrations.enabled=false >/dev/null 2>&1; then
  fail "broken release was accepted"
fi
touch "$stop_file"
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
if ((monitoring)); then
  ALERT_DRILL=0 "$ROOT/scripts/monitoring-check.sh"
  pid=$ALERT_PID ALERT_PID=""
  wait "$pid" || { sed 's/^/     /' "$ALERT_LOG"; fail "alert drill (scripts/alert-drill.sh)"; }
  cat "$ALERT_LOG"
else
  echo "skip monitoring checks: kube-prometheus-stack not installed (MONITORING=0?)"
fi

# ---------------------------------------------------------------- 8. admission policy
if kubectl get crd imagevalidatingpolicies.policies.kyverno.io >/dev/null 2>&1; then
  "$ROOT/scripts/admission-check.sh"
else
  echo "skip admission checks: Kyverno not installed (POLICIES=0?)"
fi

# ---------------------------------------------------------------- 9. network policies
# The allowed paths are proven above: users reach the app port (every in_cluster call),
# the app reaches Postgres (/readyz), Prometheus scrapes port 9100 (step 7). Here, the
# refusals: these must FAIL. A CNI that ignored NetworkPolicy would fail this step, not
# pass it silently. Each refusal has a control that succeeds from the same place.
in_cluster prod /readyz || fail "control: prod app port unreachable from postgres-0"
if kubectl -n prod exec postgres-0 -- wget -q -O /dev/null -T 3 http://shortly:9100/metrics 2>/dev/null; then
  fail "network policy: a non-Prometheus pod scraped prod's metrics port"
fi
pass "network policy: the metrics port refuses pods other than Prometheus"

# A pod that isn't shortly must not reach Postgres. pg_isready exits 2 = no response.
probe_pg() {
  kubectl -n prod run "np-probe-$1" --image=postgres:17-alpine --restart=Never --rm -i --quiet \
    --labels="$2" --overrides='{"spec":{"automountServiceAccountToken":false,
      "securityContext":{"runAsNonRoot":true,"runAsUser":70,"seccompProfile":{"type":"RuntimeDefault"}},
      "containers":[{"name":"probe","image":"postgres:17-alpine","command":["pg_isready","-h","postgres","-t","4"],
      "securityContext":{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]}}}]}}' >/dev/null 2>&1
}
probe_pg part-of app.kubernetes.io/part-of=shortly \
  || fail "control: a pod labelled part-of=shortly could not reach Postgres"
if probe_pg stranger app=stranger; then
  fail "network policy: a pod that isn't shortly reached Postgres"
fi
# The old peer (name=shortly, charts before 0.6.0) was removed: such a pod must be refused.
if probe_pg old-label app.kubernetes.io/name=shortly; then
  fail "network policy: Postgres still admits the old app.kubernetes.io/name=shortly label"
fi
pass "network policy: Postgres accepts part-of=shortly only (not strangers, not the old label)"

# ---------------------------------------------------------------- 10. expired-link cleanup
# Plant links in staging's database, run the CronJob now, and check that exactly the right
# rows went: expired 30 days ago (past the 7-day grace) gone; expired yesterday (410 Gone
# still) and not yet expired kept. The Job runs the release image, under the restricted
# PSS and its own NetworkPolicy, so this also proves it can reach Postgres.
psql_staging() { kubectl -n staging exec postgres-0 -- psql -U shortly -d shortly -v ON_ERROR_STOP=1 -tAc "$1"; }
status_of() {
  kubectl -n "$1" exec postgres-0 -- wget -S -O /dev/null -T 3 "http://shortly$2" 2>&1 \
    | grep -m1 -oE 'HTTP/1\.[01] [0-9]{3}' | cut -d' ' -f2
}
[[ "$(psql_staging "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = 'ix_links_expires_at'")" == t ]] \
  || fail "cleanup: index ix_links_expires_at missing or invalid (did the concurrent build fail?)"
psql_staging "INSERT INTO links (code, target, created_at, expires_at) VALUES
  ('e2eold1', 'https://example.com/old', now() - interval '40 days', now() - interval '30 days'),
  ('e2enew1', 'https://example.com/new', now() - interval '2 days',  now() - interval '1 day'),
  ('e2efut1', 'https://example.com/fut', now(),                      now() + interval '1 day')" >/dev/null
[[ "$(status_of staging /e2eold1)" == 410 ]] || fail "cleanup: an expired link should answer 410 before cleanup"
kubectl -n staging create job cleanup-e2e --from=cronjob/shortly-cleanup >/dev/null
kubectl -n staging wait --for=condition=complete job/cleanup-e2e --timeout=180s >/dev/null \
  || { kubectl -n staging logs job/cleanup-e2e --tail=20 || true; fail "cleanup: the Job did not complete"; }
left=$(psql_staging "SELECT string_agg(code, ',' ORDER BY code) FROM links WHERE code LIKE 'e2e%'")
[[ "$left" == "e2efut1,e2enew1" ]] || fail "cleanup: left $left, want e2efut1,e2enew1"
[[ "$(status_of staging /e2eold1)" == 404 && "$(status_of staging /e2enew1)" == 410 ]] \
  || fail "cleanup: want 404 for the deleted link and 410 for the one in its grace period"
summary=$(kubectl -n staging logs job/cleanup-e2e | grep '"cleanup finished"' || true)
[[ "$summary" == *'"deleted": 1'* ]] || fail "cleanup: no 'cleanup finished' log with deleted=1: $summary"
kubectl -n staging delete job cleanup-e2e --wait=false >/dev/null
pass "cleanup: the Job deleted only the link past its grace period (404), kept the rest (410, live)"

echo "k8s e2e passed"
