#!/usr/bin/env bash
# End-to-end on Kubernetes, including the rollback drill. Runs in CI on every PR and the
# same way on your laptop:
#
#   ../platform/scripts/cluster.sh up        # the platform repo, beside this one
#   docker build -t shortly:e2e .
#   IMAGE=shortly:e2e scripts/k8s-e2e.sh
#
# What it proves:
#   0. the digest-mode render (how releases deploy) passes API-server validation
#   1. the chart deploys under the "restricted" Pod Security Standard, migrations run as a hook
#   2. the app works in-cluster (readiness, create → redirect, metrics)
#   3. the SAME image promotes to prod: autoscaled (HPA, min 2) + a PodDisruptionBudget,
#      and the per-client rate limit holds exactly across its replicas
#  3a. users reach each environment through the Gateway, by hostname, and only their own
#  3c. the front door: the app on the laptop's :8080 and the dev UIs on :8090, no port-forward,
#      and neither listener serves the other's hostnames (skipped if FRONT_DOOR=0)
#   4. a broken release (never ready) is rolled back automatically, ZERO failed requests
#   5. prod's canary: a healthy release goes 10% → 50% → 100% on passing analysis; a
#      manual `helm rollback` restores the previous one at once (rollbackWindow)
#  5b. a release that is ready but failing half its requests is aborted by analysis at 10%
#   6. under load, the HPA adds ready pods (scripts/hpa-drill.sh; skip with HPA_DRILL=0)
#   7. monitoring works end to end: scrape, rules, dashboard queries (scripts/monitoring-check.sh),
#      and a real alert routed by Alertmanager (scripts/alert-drill.sh, run in the background
#      during steps 3b-6; both skipped when the stack isn't installed)
#   8. admission refuses shortly images without release provenance, even from hand-run
#      kubectl (scripts/admission-check.sh; skipped when Kyverno isn't installed)
#   9. network policies are enforced: the metrics port and Postgres refuse other pods
#  10. the cleanup CronJob deletes links past their grace period, and only those
#  11. prod's backup runs, only backup pods reach the bucket, and the dump restores into a
#      fresh database with every link (scripts/restore-drill.sh; skipped if BACKUPS=0)
#  11b. `cluster.sh wake` restarts the CronJob scheduler and takes an overdue backup at once
#  12. GitOps: Argo CD takes prod over from this commit, reverts a hand edit, and runs a
#      canary it started itself without fighting Argo Rollouts over the route's weights
#      (needs GITOPS_REVISION, a commit Argo CD can fetch; CI sets it)
#
# E2E_SUITE splits the run for CI's two parallel kind jobs. Both run steps 0-3a, then:
#   platform: 3b, 3c, 6-12 (rate limit, front door, autoscaling, monitoring and the alert drill, admission,
#             network policies, cleanup, backups, GitOps)
#   delivery: 4, 5, 5b (automatic and manual rollback, both canaries)
# (GitOps runs with the platform half: that half was about 4 minutes shorter without it.)
#   all:      everything in one cluster (the default; what a laptop runs)
# Every deploy also gets the platform's deploy/kind/e2e-values.yaml: faster scrapes and
# canary analysis.
set -euo pipefail

IMAGE="${IMAGE:?set IMAGE, e.g. IMAGE=shortly:e2e}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# ENVS, and the namespace each runs in: helpers below take an environment (staging, prod)
# shellcheck source=scripts/lib/platform.sh
source "$(dirname "$0")/lib/platform.sh"
PF_PIDS=()
ALERT_PID="" ALERT_LOG=$(mktemp)
SUITE="${E2E_SUITE:-all}"
case "$SUITE" in all | platform | delivery) ;; *) echo "E2E_SUITE must be all, platform or delivery" >&2; exit 2 ;; esac
suite() { [[ "$SUITE" == all || "$SUITE" == "$1" ]]; }
export DEPLOY_EXTRA_VALUES="${DEPLOY_EXTRA_VALUES-$PLATFORM_DIR/deploy/kind/e2e-values.yaml}"

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
    for env in "${ENVS[@]}"; do
      ns=$(ns_of "$env")
      kubectl -n "$ns" get pods,jobs -o wide || true
      kubectl -n "$ns" get events --sort-by=.lastTimestamp | tail -20 || true
      kubectl -n "$ns" logs -l app.kubernetes.io/name=shortly --all-containers --tail=30 || true
      helm -n "$ns" history shortly || true
      kubectl -n "$ns" get httproute -o wide || true
      kubectl -n "$ns" get rollout,analysisrun -o wide 2>/dev/null || true
    done
    kubectl -n gateway get gateway shortly -o wide || true
  fi
  exit "$status"
}
trap on_exit EXIT

# Port-forward a service port to a local port; wait until it answers.
forward() {
  local ns port=$2 svc_port=${3:-80} path=${4:-/healthz}
  ns=$(ns_of "$1")
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
in_cluster() { kubectl -n "$(ns_of "$1")" exec postgres-0 -- wget -q -O /dev/null -T 2 "http://shortly$2" 2>/dev/null; }

revision() { helm -n "$(ns_of "$1")" status shortly -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])'; }
# prod runs an Argo Rollout (canary.enabled), staging a Deployment: same pod template.
workload() { if kubectl -n "$(ns_of "$1")" get rollout shortly >/dev/null 2>&1; then echo rollout/shortly; else echo deploy/shortly; fi; }
deployed_image() { kubectl -n "$(ns_of "$1")" get "$(workload "$1")" -o jsonpath='{.spec.template.spec.containers[0].image}'; }
pod_env() { kubectl -n "$(ns_of "$1")" get "$(workload "$1")" -o jsonpath="{.spec.template.spec.containers[0].env[?(@.name==\"$2\")].value}"; }
ro() { kubectl -n "$PROD_NS" get rollout shortly -o jsonpath="$1"; }

# ---------------------------------------------------------------- 0. digest-mode render
# Releases deploy by digest, but this test deploys a local image by tag. Render the digest
# path too and let the real API server validate it (server-side dry run). JSON-schema tools
# like kubeconform don't check label syntax; the API server does. (v0.1.0 shipped a label
# "sha256:..." that only the API server rejected.) The real release digest, because the
# admission policy (step 8) refuses any shortly digest without release provenance.
release_digest=$(sed -nE 's/^\| Current release \| v[0-9.]+, `(sha256:[0-9a-f]{64})`.*/\1/p' "$ROOT/docs/RUNBOOK.md")
[[ -n "$release_digest" ]] || fail "no 'Current release' digest in docs/RUNBOOK.md"
for env in "${ENVS[@]}"; do
  ns=$(ns_of "$env")
  mapfile -t values < <(values_args "$env")
  helm template shortly "$(chart_ref "$env")" -n "$ns" "${values[@]}" \
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

# ---------------------------------------------------------------- 3. promote to prod
"$ROOT/scripts/deploy.sh" prod "$IMAGE" >/dev/null
[[ "$(workload prod)" == rollout/shortly ]] || fail "prod is not an Argo Rollout (canary.enabled?)"
ready=$(kubectl -n "$PROD_NS" get rollout shortly -o jsonpath='{.status.readyReplicas}')
[[ "$ready" == 2 ]] || fail "prod has $ready ready replicas, want 2"
kubectl -n "$PROD_NS" get pdb shortly >/dev/null || fail "prod PodDisruptionBudget missing"
kubectl -n "$PROD_NS" get hpa shortly >/dev/null || fail "prod HorizontalPodAutoscaler missing"
[[ "$(deployed_image prod)" == "$(deployed_image staging)" ]] || fail "prod runs a different image than staging"
[[ "$(kubectl -n "$PROD_NS" get hpa shortly -o jsonpath='{.spec.scaleTargetRef.kind}')" == Rollout ]] || fail "prod HPA doesn't scale the Rollout"
pass "promoted the same image to prod: a Rollout, 2/2 ready, PDB and HPA (on the Rollout) present"

in_cluster prod /readyz || fail "prod not reachable through its Service"

# ---------------------------------------------------------------- 3a. through the gateway
# Users come in through the Gateway (the platform's deploy/kind/gateway.yaml), routed by hostname. From a
# pod, as a client would: the Envoy proxy's Service, with the Host header deciding where to.
GW=http://shortly-gateway.envoy-gateway-system.svc
gw() { # host path [extra wget args...] -> prints "STATUS LOCATION"
  local host=$1 path=$2; shift 2
  kubectl -n "$PROD_NS" exec postgres-0 -- wget -S -O /dev/null -T 5 --header "Host: $host" "$@" "$GW$path" 2>&1 \
    | awk '/^  HTTP\//{if (!s) s=$2} /^  [Ll]ocation:/{if (!l) l=$2} END{print s, l}' # first response only: wget follows redirects
}
[[ "$(gw sho.rt.localhost /readyz)" == "200 " ]] || fail "gateway: prod /readyz via sho.rt.localhost"
[[ "$(gw staging.sho.rt.localhost /readyz)" == "200 " ]] || fail "gateway: staging /readyz via staging.sho.rt.localhost"
gw_code=$(kubectl -n "$PROD_NS" exec postgres-0 -- wget -q -O - -T 5 --header "Host: sho.rt.localhost" \
  --header 'content-type: application/json' --header "X-Forwarded-For: 203.0.113.250" \
  --post-data '{"url":"https://gateway-e2e.invalid/via-gateway"}' "$GW/links" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["code"])' 2>/dev/null || true)
[[ -n "$gw_code" ]] || fail "gateway: create through sho.rt.localhost failed"
[[ "$(gw sho.rt.localhost "/$gw_code")" == "307 https://gateway-e2e.invalid/via-gateway" ]] || fail "gateway: prod redirect"
# The same code under staging's hostname is staging's database: not found. Routes don't leak.
[[ "$(gw staging.sho.rt.localhost "/$gw_code")" == "404 " ]] || fail "gateway: a prod link resolved through staging's hostname"
[[ "$(gw unknown.example /readyz)" == "404 " ]] || fail "gateway: an unknown hostname was routed somewhere"
pass "gateway: sho.rt.localhost → prod, staging.sho.rt.localhost → staging (create → 307 via the gateway), no cross-environment leaks, unknown hosts 404"

# ---------------------------------------------------------------- 7a. alert drill, in the background
# It scales staging to zero and waits about 90 s for ShortlyNoAvailablePods to reach
# Alertmanager. Steps 3b and 6 only touch prod, so it runs alongside them instead of after them.
# It starts after 3a, which needs staging up (the gateway checks reach both environments).
# Collected in step 7, before anything uses staging again.
monitoring=0
kubectl get crd prometheusrules.monitoring.coreos.com >/dev/null 2>&1 && monitoring=1
if ((monitoring)) && suite platform; then
  "$ROOT/scripts/alert-drill.sh" >"$ALERT_LOG" 2>&1 &
  ALERT_PID=$!
  echo "     alert drill started in the background (staging scaled to 0)"
fi

# ---------------------------------------------------------------- 3b. rate limit
if suite platform; then
# One client, through the Service, so kube-proxy spreads the requests over both pods: the
# limit must still be one burst (plus what refills meanwhile), because its state is in
# Postgres. Per-pod counters would let all burst + 5 through.
# X-Forwarded-For picks the client (prod trusts the pod network as kind's stand-in ingress).
create_as() {
  kubectl -n "$PROD_NS" exec postgres-0 -- wget -S -O /dev/null -T 3 \
    --header 'content-type: application/json' --header "X-Forwarded-For: $1" \
    --post-data '{"url":"https://example.com/rate"}' http://shortly/links 2>&1 \
    | grep -m1 -oE 'HTTP/1\.[01] [0-9]{3}' | cut -d' ' -f2
}
limits=$(helm -n "$PROD_NS" get values shortly --all -o json \
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
pass "rate limit: one client got $created creates (burst $burst, at most $max with refill), then 429s, across $(kubectl -n "$PROD_NS" get rollout shortly -o jsonpath='{.status.readyReplicas}') pods; other clients unaffected"

fi # 3b

# ---------------------------------------------------------------- 3c. front door
# The laptop's 8080 (users) and 8090 (dev tools) reach the Gateway through the front-door
# containers (the platform's scripts/cluster.sh), with no port-forward. And the listeners stay apart: a
# tool's hostname gets nothing on the users' port, and the app's nothing on the tools' port.
if suite platform && [[ "${FRONT_DOOR:-1}" != 0 ]]; then
front() { # port host path -> HTTP status
  curl -s -o /dev/null -w '%{http_code}' -m 5 -H "Host: $2" "http://127.0.0.1:$1$3" || true
}
expect_front() { # port host path status
  local got=""
  for _ in $(seq 1 30); do
    got=$(front "$1" "$2" "$3")
    [[ "$got" == "$4" ]] && return 0
    sleep 2
  done
  fail "front door: http://$2:$1$3 answered $got, expected $4"
}
# Prod only: the alert drill (step 7) scales staging to 0 in the background while this runs,
# so a staging check here fails or passes by timing (seen in CI). Step 3a already proves the
# Gateway routes staging.
expect_front 8080 sho.rt.localhost /healthz 200
tools=(grafana.localhost/api/health prometheus.localhost/-/ready alertmanager.localhost/-/ready)
if kubectl get ns argocd >/dev/null 2>&1; then tools+=(argocd.localhost/healthz); fi
for t in "${tools[@]}"; do
  expect_front 8090 "${t%%/*}" "/${t#*/}" 200
  expect_front 8080 "${t%%/*}" "/${t#*/}" 404
done
expect_front 8090 sho.rt.localhost /healthz 404
pass "front door: the app on :8080, ${#tools[@]} dev tools on :8090, and neither listener serves the other's hostnames"
fi # 3c

good_rev=$(revision prod)
good_image=$(deployed_image prod)

# Canary analysis needs Prometheus; its CI timings come from the platform's deploy/kind/e2e-values.yaml
# (every deploy gets it). Without monitoring, steps are short plain pauses instead.
canary_fast=(--set canary.pauseWithoutAnalysis=5s)
# Every measurement of every AnalysisRun for one pod hash: "name=Phase ..." per run.
analysis_runs() {
  kubectl -n "$PROD_NS" get analysisrun -l "rollouts-pod-template-hash=$1" --sort-by=.metadata.creationTimestamp \
    -o jsonpath='{range .items[*]}{.status.phase}{" "}{end}'
}

if suite delivery; then
# ---------------------------------------------------------------- 4. automatic rollback
# A bad config value makes the new pods fail at boot (Settings validation). They never
# become ready, so the canary never gets traffic and can't progress; the stable pods keep
# serving; deploy.sh aborts the Rollout when CANARY_TIMEOUT runs out and rolls helm back.
# Meanwhile we hit prod through its Service AND the Gateway (where the canary's weight
# lives), and count failures, from before the upgrade until the rollback is done.
# 45 s is plenty: the new pod crashes within seconds and cannot get unstuck.
in_gateway() { [[ "$(gw sho.rt.localhost "$1")" == "200 " ]]; }
errors_file=$(mktemp) stop_file=$(mktemp -u)
(
  errors=0
  end=$((SECONDS + 240)) # a backstop only
  while [[ ! -e "$stop_file" ]] && ((SECONDS < end)); do
    in_cluster prod /readyz || errors=$((errors + 1))
    in_gateway /readyz || errors=$((errors + 1))
    sleep 0.2
  done
  echo "$errors" >"$errors_file"
) &
load_pid=$!

if CANARY_TIMEOUT=45s CANARY_TRAFFIC=0 "$ROOT/scripts/deploy.sh" prod "$IMAGE" \
  --set config.logLevel=NOT_A_LEVEL --set migrations.enabled=false >/dev/null 2>&1; then
  fail "broken release was accepted"
fi
touch "$stop_file"
wait "$load_pid"
errors=$(cat "$errors_file")

[[ "$(deployed_image prod)" == "$good_image" ]] || fail "prod not back on the good image"
[[ "$(pod_env prod SHORTLY_LOG_LEVEL)" == INFO ]] || fail "prod's Rollout not back on the good config"
[[ "$(ro '{.status.phase}')" == Healthy ]] || fail "Rollout is $(ro '{.status.phase}') after the rollback"
status=$(helm -n "$PROD_NS" status shortly -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["status"])')
[[ "$status" == deployed ]] || fail "release status is $status"
[[ "$errors" == 0 ]] || fail "$errors failed requests during the bad rollout"
pass "broken release auto-rolled back (now revision $(revision prod)); 0 failed requests during it"

# ---------------------------------------------------------------- 5. good canary, manual rollback
# A healthy new version (only the log level differs) goes 10% → 50% → 100%, passing analysis
# at each step, with deploy.sh's synthetic traffic through the Gateway.
start=$SECONDS
"$ROOT/scripts/deploy.sh" prod "$IMAGE" --set config.logLevel=DEBUG "${canary_fast[@]}" >/dev/null \
  || fail "a healthy canary was not promoted"
took=$((SECONDS - start))
[[ "$(pod_env prod SHORTLY_LOG_LEVEL)" == DEBUG ]] || fail "DEBUG release didn't apply"
hash=$(ro '{.status.stableRS}')
weight=$(ro '{.status.canary.weights.stable.weight}')
[[ "$weight" == 100 || -z "$weight" ]] || fail "stable weight is $weight after promotion, want 100"
runs="no analysis (monitoring off)"
if ((monitoring)); then
  runs=$(analysis_runs "$hash")
  [[ "$runs" == "Successful Successful " ]] || fail "analysis runs for the promoted canary: '$runs', want two Successful"
  runs="analysis Successful at 10% and 50%"
fi
pass "healthy canary promoted 10% → 50% → 100% in ${took}s ($runs)"

# Back to the good revision with helm: inside rollbackWindow, so no steps, no analysis.
start=$SECONDS
helm -n "$PROD_NS" rollback shortly "$good_rev" --force-conflicts --wait=legacy --timeout 2m >/dev/null
for _ in $(seq 1 60); do
  [[ "$(ro '{.status.phase}')" == Healthy && "$(ro '{.status.stableRS}')" == "$(ro '{.status.currentPodHash}')" ]] && break
  sleep 2
done
took=$((SECONDS - start))
level=$(pod_env prod SHORTLY_LOG_LEVEL)
[[ "$level" == INFO ]] || fail "rollback didn't restore config (log level $level)"
[[ "$(ro '{.status.phase}')" == Healthy ]] || fail "Rollout is $(ro '{.status.phase}') after helm rollback"
((took < 60)) || fail "helm rollback took ${took}s: it should skip the canary steps (rollbackWindow)"
in_cluster prod /readyz || fail "prod unhealthy after rollback"
in_gateway /readyz || fail "prod unreachable through the Gateway after rollback"
pass "manual helm rollback to revision $good_rev restored the previous release in ${took}s (rollbackWindow: no canary steps)"

# ---------------------------------------------------------------- 5b. bad canary is aborted
# The release that probes can't catch: ready, but half of its creates and redirects fail
# (config.faultErrorRatio, a drill-only switch). Analysis must see the canary's error ratio
# and abort at the first step, while users mostly never notice: only the canary's share
# of traffic (10%) could fail at all. We measure that share through the Gateway.
if ((monitoring)); then
  gw_code2=$(kubectl -n "$PROD_NS" exec postgres-0 -- wget -q -O - -T 5 --header "Host: sho.rt.localhost" \
    --header 'content-type: application/json' --header "X-Forwarded-For: 203.0.113.251" \
    --post-data '{"url":"https://gateway-e2e.invalid/canary"}' "$GW/links" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["code"])')
  counts_file=$(mktemp) stop_file=$(mktemp -u)
  (
    ok=0 bad=0
    end=$((SECONDS + 300)) # a backstop only
    while [[ ! -e "$stop_file" ]] && ((SECONDS < end)); do
      if [[ "$(gw sho.rt.localhost "/$gw_code2")" == "307 "* ]]; then ok=$((ok + 1)); else bad=$((bad + 1)); fi
    done
    echo "$ok $bad" >"$counts_file"
  ) &
  load_pid=$!
  start=$SECONDS
  if "$ROOT/scripts/deploy.sh" prod "$IMAGE" --set config.faultErrorRatio=0.5 "${canary_fast[@]}" \
    >/dev/null 2>&1; then
    fail "a canary failing half its requests was promoted"
  fi
  took=$((SECONDS - start))
  touch "$stop_file"
  wait "$load_pid"
  read -r ok bad <"$counts_file"
  [[ -z "$(pod_env prod SHORTLY_FAULT_ERROR_RATIO)" ]] || fail "prod still has fault injection after the abort"
  [[ "$(ro '{.status.phase}')" == Healthy ]] || fail "Rollout is $(ro '{.status.phase}') after the abort"
  bad_hash=$(kubectl -n "$PROD_NS" get analysisrun --sort-by=.metadata.creationTimestamp \
    -o jsonpath='{.items[-1:].metadata.labels.rollouts-pod-template-hash}')
  [[ "$(analysis_runs "$bad_hash")" == "Failed " ]] || fail "bad canary's analysis: '$(analysis_runs "$bad_hash")', want one Failed"
  failed_metric=$(kubectl -n "$PROD_NS" get analysisrun -l "rollouts-pod-template-hash=$bad_hash" \
    -o jsonpath='{.items[0].status.metricResults[?(@.phase=="Failed")].name}')
  [[ "$failed_metric" == *error-ratio* ]] || fail "analysis failed on '$failed_metric', want error-ratio"
  total=$((ok + bad))
  ((total > 50)) || fail "only $total requests measured through the Gateway"
  # 10% of traffic x 50% faulty = 5% expected; 15% would mean more than the canary's share.
  ((bad * 100 <= total * 15)) || fail "$bad of $total redirects failed: more than the canary's 10% share"
  pass "faulty canary aborted at 10% in ${took}s (analysis: error-ratio Failed), rolled back; users saw $bad of $total redirects fail ($((bad * 1000 / total / 10)).$((bad * 1000 / total % 10))%)"
else
  echo "skip bad-canary drill: no Prometheus, so no analysis (MONITORING=0?)"
fi

fi # 4-5b

if suite platform; then
# ---------------------------------------------------------------- 6. autoscaling
if [[ "${HPA_DRILL:-1}" != 0 ]]; then
  NS="$PROD_NS" "$ROOT/scripts/hpa-drill.sh"
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
if kubectl -n "$PROD_NS" exec postgres-0 -- wget -q -O /dev/null -T 3 http://shortly:9100/metrics 2>/dev/null; then
  fail "network policy: a non-Prometheus pod scraped prod's metrics port"
fi
pass "network policy: the metrics port refuses pods other than Prometheus"

# A pod that isn't shortly must not reach Postgres. pg_isready exits 2 = no response.
probe_pg() {
  kubectl -n "$PROD_NS" run "np-probe-$1" --image=postgres:17-alpine --restart=Never --rm -i --quiet \
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
psql_staging() { kubectl -n "$STAGING_NS" exec postgres-0 -- psql -U shortly -d shortly -v ON_ERROR_STOP=1 -tAc "$1"; }
status_of() {
  kubectl -n "$(ns_of "$1")" exec postgres-0 -- wget -S -O /dev/null -T 3 "http://shortly$2" 2>&1 \
    | grep -m1 -oE 'HTTP/1\.[01] [0-9]{3}' | cut -d' ' -f2
}
[[ "$(psql_staging "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = 'ix_links_expires_at'")" == t ]] \
  || fail "cleanup: index ix_links_expires_at missing or invalid (did the concurrent build fail?)"
psql_staging "INSERT INTO links (code, target, created_at, expires_at) VALUES
  ('e2eold1', 'https://example.com/old', now() - interval '40 days', now() - interval '30 days'),
  ('e2enew1', 'https://example.com/new', now() - interval '2 days',  now() - interval '1 day'),
  ('e2efut1', 'https://example.com/fut', now(),                      now() + interval '1 day')" >/dev/null
[[ "$(status_of staging /e2eold1)" == 410 ]] || fail "cleanup: an expired link should answer 410 before cleanup"
kubectl -n "$STAGING_NS" create job cleanup-e2e --from=cronjob/shortly-cleanup >/dev/null
kubectl -n "$STAGING_NS" wait --for=condition=complete job/cleanup-e2e --timeout=180s >/dev/null \
  || { kubectl -n "$STAGING_NS" logs job/cleanup-e2e --tail=20 || true; fail "cleanup: the Job did not complete"; }
left=$(psql_staging "SELECT string_agg(code, ',' ORDER BY code) FROM links WHERE code LIKE 'e2e%'")
[[ "$left" == "e2efut1,e2enew1" ]] || fail "cleanup: left $left, want e2efut1,e2enew1"
[[ "$(status_of staging /e2eold1)" == 404 && "$(status_of staging /e2enew1)" == 410 ]] \
  || fail "cleanup: want 404 for the deleted link and 410 for the one in its grace period"
summary=$(kubectl -n "$STAGING_NS" logs job/cleanup-e2e | grep '"cleanup finished"' || true)
[[ "$summary" == *'"deleted": 1'* ]] || fail "cleanup: no 'cleanup finished' log with deleted=1: $summary"
kubectl -n "$STAGING_NS" delete job cleanup-e2e --wait=false >/dev/null
pass "cleanup: the Job deleted only the link past its grace period (404), kept the rest (410, live)"

# ---------------------------------------------------------------- 11. backup and restore
# Back up prod now (the CronJob's own template), with a canary link written just before,
# then restore that dump into a scratch database and check every link came back. A backup
# is only proven by a restore; this one is proven on every PR.
if kubectl -n "$PROD_NS" get cronjob postgres-backup >/dev/null 2>&1; then
  psql_prod() { kubectl -n "$PROD_NS" exec postgres-0 -- psql -U shortly -d shortly -v ON_ERROR_STOP=1 -tAc "$1"; }
  psql_prod "INSERT INTO links (code, target, created_at) VALUES ('e2ebkp1', 'https://example.com/backup', now())" >/dev/null
  want_links=$(psql_prod "SELECT count(*) FROM links")
  kubectl -n "$PROD_NS" create job backup-e2e --from=cronjob/postgres-backup >/dev/null
  if ! kubectl -n "$PROD_NS" wait --for=condition=complete job/backup-e2e --timeout=180s >/dev/null; then
    kubectl -n "$PROD_NS" logs job/backup-e2e --all-containers --tail=20 || true
    fail "backup: the Job did not complete"
  fi
  uploaded=$(kubectl -n "$PROD_NS" logs job/backup-e2e -c upload | grep '"backup uploaded"' || true)
  [[ -n "$uploaded" ]] || fail "backup: no 'backup uploaded' log line"
  pass "backup: pg_dump checked and uploaded ($(kubectl -n "$PROD_NS" logs job/backup-e2e -c dump | grep -o '"bytes": [0-9]*'))"
  # The bucket is for backup pods only: postgres-0 (no component=backup label) is refused.
  # The control is the Job above, which reached it from the same namespace.
  if kubectl -n "$PROD_NS" exec postgres-0 -- nc -z -w 3 seaweedfs.backups.svc 8333 2>/dev/null; then
    fail "network policy: a pod that isn't a backup pod reached the backup bucket"
  fi
  pass "network policy: the backup bucket refuses pods other than backup and restore pods"
  SOURCE="$PROD_NS" EXPECT_CODE=e2ebkp1 EXPECT_LINKS="$want_links" "$ROOT/scripts/restore-drill.sh"
  kubectl -n "$PROD_NS" delete job backup-e2e --wait=false >/dev/null

  # 11b. `cluster.sh wake` (after a laptop slept): the controller restarts, and an overdue
  # backup starts at once. MAX_AGE=0 makes every backup "overdue" here.
  restarts() { kubectl -n kube-system get pod -l component=kube-controller-manager \
    -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}'; }
  before=$(restarts)
  WAKE_BACKUP_MAX_AGE=0 "$PLATFORM_DIR/scripts/cluster.sh" wake
  (($(restarts) == before + 1)) || fail "wake: kube-controller-manager was not restarted"
  wake_job=$(kubectl -n "$PROD_NS" get jobs -o name | grep -m1 'job.batch/backup-wake-' || true)
  [[ -n "$wake_job" ]] || fail "wake: no backup-wake Job in prod"
  kubectl -n "$PROD_NS" wait --for=condition=complete "$wake_job" --timeout=180s >/dev/null \
    || fail "wake: $wake_job did not complete"
  pass "wake: kube-controller-manager restarted, and the overdue prod backup ran at once ($wake_job)"
else
  echo "skip backup checks: no postgres-backup CronJob (BACKUPS=0?)"
fi

fi # 6-11

# ---------------------------------------------------------------- 12. GitOps (Argo CD)
# Prod's Application as the platform makes it: its ApplicationSet (apps/applicationset.yaml at
# platform.lock) and shortly's AppProject, applied as they are, with three CI-only changes:
#   - the generator reads only prod's env file, at the locked platform commit (not main);
#   - the values come from this commit (GITOPS_REVISION) instead of main, plus the
#     platform's e2e-values (CI clocks, as deploy.sh) and the local build instead of the
#     release digest; the project also allows the platform repo, for that values file;
#   - applicationsSync: create-only, so the change made through Argo CD below (an edit to
#     the Application, as a merged PR would make) isn't reverted by the ApplicationSet.
# Everything else is the real template: auto-sync, self-heal, the canary's ignored weights.
if suite platform && [[ -n "${GITOPS_REVISION:-}" ]] && kubectl get crd applicationsets.argoproj.io >/dev/null 2>&1; then
  app() { kubectl -n argocd get application shortly-prod -o jsonpath="$1" 2>/dev/null; }
  synced_healthy() { [[ "$(app '{.status.sync.status}/{.status.health.status}')" == Synced/Healthy ]]; }
  wait_app() { # seconds what -> waits until Synced and Healthy on the newest spec
    for _ in $(seq 1 "$(($1 / 3))"); do synced_healthy && return 0; sleep 3; done
    { kubectl -n argocd get application shortly-prod -o jsonpath='{range .status.resources[?(@.status!="Synced")]}{.kind}/{.name}: {.status}{"\n"}{end}{.status.operationState.message}{"\n"}{range .status.conditions[*]}{.type}: {.message}{"\n"}{end}' || true; } >&2
    fail "GitOps: $2: application shortly-prod is $(app '{.status.sync.status}/{.status.health.status}')"
  }
  platform_repo=https://github.com/easypeasyops/platform.git
  # shortly's project, as JSON (kubectl converts; the runner's python has no YAML module).
  kubectl create --dry-run=client -o json -f "$PLATFORM_DIR/apps/shortly/project.yaml" \
    | python3 -c '
import json, sys
project = json.load(sys.stdin)
project["spec"]["sourceRepos"].append(sys.argv[1])  # CI only: the platform e2e-values
print(json.dumps(project))
' "$platform_repo" | kubectl apply -f - >/dev/null
  kubectl create --dry-run=client -o json -f "$PLATFORM_DIR/apps/applicationset.yaml" \
    | python3 "$ROOT/scripts/lib/e2e-appset.py" "$PLATFORM_LOCK" "$GITOPS_REVISION" \
      "$platform_repo" "${IMAGE%:*}" "${IMAGE##*:}" \
    | kubectl apply -f - >/dev/null
  for _ in $(seq 1 40); do [[ -n "$(app '{.metadata.name}')" ]] && break; sleep 3; done
  [[ "$(app '{.metadata.ownerReferences[0].name}')" == apps ]] \
    || fail "GitOps: the ApplicationSet didn't generate shortly-prod: $(kubectl -n argocd get applicationset apps -o jsonpath='{.status.conditions}')"
  start=$SECONDS
  wait_app 240 "taking over prod"
  [[ "$(deployed_image prod)" == "$IMAGE" ]] || fail "GitOps: prod runs $(deployed_image prod), want $IMAGE"
  pass "GitOps: Argo CD took prod over from $GITOPS_REVISION (Synced, Healthy) in $((SECONDS - start))s"

  # Self-heal: a hand edit to something the chart sets is put back.
  kubectl -n "$PROD_NS" patch pdb shortly --type merge -p '{"spec":{"minAvailable":0}}' >/dev/null
  start=$SECONDS healed=0
  for _ in $(seq 1 30); do
    [[ "$(kubectl -n "$PROD_NS" get pdb shortly -o jsonpath='{.spec.minAvailable}')" == 1 ]] && { healed=1; break; }
    sleep 2
  done
  ((healed)) || fail "GitOps: self-heal did not restore the PodDisruptionBudget within 60s"
  pass "GitOps: a hand edit (PDB minAvailable 1 → 0) was reverted in $((SECONDS - start))s"

  # A change through Argo CD (as a merged PR would make): a full canary, with its own
  # synthetic traffic, while Argo CD keeps watching. It must not reset the weights.
  old_hash=$(ro '{.status.stableRS}')
  kubectl -n argocd patch application shortly-prod --type json -p \
    '[{"op":"add","path":"/spec/sources/0/helm/valuesObject/config","value":{"logLevel":"DEBUG"}}]' >/dev/null
  start=$SECONDS max_weight=0 out_of_sync=0 drifted=""
  while ((SECONDS - start < 420)); do
    w=$(kubectl -n "$PROD_NS" get httproute shortly -o jsonpath='{.spec.rules[0].backendRefs[1].weight}')
    ((${w:-0} > max_weight)) && max_weight=${w:-0}
    if [[ "$(app '{.status.sync.status}')" == OutOfSync ]]; then
      out_of_sync=$((out_of_sync + 1))
      # Which resources, so a failure below says what Argo CD and the canary disagreed on.
      drifted+=" $(app '{range .status.resources[?(@.status=="OutOfSync")]}{.kind}/{.name},{end}')@$((SECONDS - start))s"
    fi
    new_hash=$(ro '{.status.stableRS}')
    [[ -n "$new_hash" && "$new_hash" != "$old_hash" && "$(ro '{.status.phase}')" == Healthy ]] && break
    [[ "$(ro '{.status.phase}')" == Degraded ]] && fail "GitOps: the canary was aborted: $(ro '{.status.message}')"
    sleep 3
  done
  [[ "$new_hash" != "$old_hash" ]] || fail "GitOps: the canary did not finish in 420 s ($(ro '{.status.phase}'))"
  [[ "$(pod_env prod SHORTLY_LOG_LEVEL)" == DEBUG ]] || fail "GitOps: the change did not reach the Rollout"
  ((max_weight >= 10)) || fail "GitOps: the canary never got traffic (max weight $max_weight%)"
  if ((monitoring)); then
    [[ "$(analysis_runs "$new_hash")" == *Successful*Successful* ]] \
      || fail "GitOps: analysis runs for the canary: '$(analysis_runs "$new_hash")'"
  fi
  # A sample or two of OutOfSync is the sync itself; repeated ones mean a fight over weights.
  ((out_of_sync <= 3)) \
    || fail "GitOps: Argo CD saw prod OutOfSync $out_of_sync times during the canary (out of sync, by seconds into it:$drifted)"
  wait_app 60 "after the canary"
  pass "GitOps: a change through Argo CD ran a full canary (weights up to ${max_weight}%, analysis Successful twice, synthetic traffic included) in $((SECONDS - start))s; Argo CD stayed in sync"
else
  echo "skip GitOps checks: no Argo CD, or GITOPS_REVISION unset (CI sets it)"
fi

echo "k8s e2e passed"
