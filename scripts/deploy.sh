#!/usr/bin/env bash
# Deploy one image to one environment. The SAME image reference is promoted from staging
# to prod — build once, deploy many; never rebuild for production.
#
#   scripts/deploy.sh staging ghcr.io/statsguy/shortly@sha256:...   # released image (preferred)
#   scripts/deploy.sh prod    ghcr.io/statsguy/shortly@sha256:...
#   scripts/deploy.sh staging shortly:dev                            # local image, kind only
#
# A digest is deployed only after scripts/verify-image.sh proves it was built by this repo's
# release workflow from a release tag. A tag is accepted only on a kind-* kube context.
#
# Where Argo CD manages an environment (scripts/cluster.sh applies deploy/argocd/), releases
# go through git instead (scripts/promote.sh) and this script refuses. It remains the way
# CI's e2e deploys local builds, and the break-glass path.
#
# Extra args are passed to helm (e.g. --set config.logLevel=DEBUG).
# A failed rollout is rolled back automatically (--rollback-on-failure). In an environment
# with canary.enabled (prod), the new version is a canary first; see below.
set -euo pipefail

ENV="${1:?usage: $0 <staging|prod> <image-ref> [helm args...]}"
REF="${2:?usage: $0 <staging|prod> <image-ref> [helm args...]}"
shift 2
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TIMEOUT="${DEPLOY_TIMEOUT:-3m}"

case "$ENV" in staging | prod) ;; *) echo "unknown env: $ENV" >&2; exit 2 ;; esac
# shellcheck source=scripts/lib/platform.sh
source "$(dirname "$0")/lib/platform.sh"
NS=$(ns_of "$ENV") # the namespace this environment runs in

context=$(kubectl config current-context 2>/dev/null || true)

# Where Argo CD manages the environment (deploy/argocd/), git is the deploy: a release
# reaches it by a merged PR to deploy/envs/$ENV.yaml (scripts/promote.sh), and anything
# deployed by hand here would be reverted by Argo CD's self-heal within a minute.
if [[ -n "$(kubectl -n argocd get application "shortly-$ENV" -o name 2>/dev/null)" && "${ARGOCD_OVERRIDE:-}" != 1 ]]; then
  echo "REFUSED: $ENV is deployed by Argo CD from git (application shortly-$ENV)." >&2
  echo "  Promote with: scripts/promote.sh $ENV <digest>, then merge the PR." >&2
  echo "  Break glass: RUNBOOK, \"Deploy by hand while Argo CD manages the environment\"." >&2
  exit 1
fi

if [[ "$REF" == *@sha256:* ]]; then
  # Released images deploy only if their provenance proves they came from this repo's
  # release workflow at a release tag (scripts/verify-image.sh). Break glass, loudly:
  # INSECURE_SKIP_VERIFY=1, e.g. when the registry or Sigstore is unreachable mid-incident.
  if [[ "${INSECURE_SKIP_VERIFY:-}" == 1 ]]; then
    echo "WARNING: provenance verification SKIPPED for $REF (INSECURE_SKIP_VERIFY=1)" >&2
  else
    "$ROOT/scripts/verify-image.sh" "$REF"
  fi
  image_args=(--set "image.repository=${REF%@*}" --set "image.digest=${REF#*@}")
else
  # A tag can't be verified (it can be re-pointed at any time), so tags are for local
  # builds on kind only. Anything else deploys a released digest.
  [[ "$context" == kind-* ]] \
    || { echo "REFUSED: $REF is a tag; outside kind, deploy a released digest (repo@sha256:...)" >&2; exit 1; }
  # image.digest="" too: the env file names a release digest, and a digest wins over a tag.
  image_args=(--set "image.repository=${REF%:*}" --set "image.tag=${REF##*:}" --set "image.digest=")
  # A local tag only exists on this machine; copy it into the kind node.
  if docker image inspect "$REF" >/dev/null 2>&1; then
    kind load docker-image "$REF" --name "${context#kind-}"
  fi
fi

# With canary.enabled (prod) the chart renders an Argo Rollout instead of a Deployment, and
# the release is not done when helm returns: the Rollout then walks its steps.
# DEPLOY_EXTRA_VALUES: one more values file, after the environment's (the e2e's shorter
# clocks, deploy/kind/e2e-values.yaml). Not for real deploys.
extra_values=()
[[ -n "${DEPLOY_EXTRA_VALUES:-}" ]] && extra_values=(--values "$DEPLOY_EXTRA_VALUES")
helm_args=(--namespace "$NS" --values "$ROOT/deploy/envs/$ENV.yaml" "${image_args[@]}" \
  ${extra_values[@]+"${extra_values[@]}"} "$@")
canary=0
if helm template shortly "$ROOT/deploy/helm/shortly" "${helm_args[@]}" \
  --show-only templates/rollout.yaml >/dev/null 2>&1; then
  canary=1
fi

echo "deploying $REF → $ENV$( ((canary)) && echo ' (canary)')"
if ((!canary)); then
  helm upgrade --install shortly "$ROOT/deploy/helm/shortly" "${helm_args[@]}" \
    --rollback-on-failure --wait --timeout "$TIMEOUT"
  kubectl -n "$NS" rollout status deploy/shortly --timeout=60s
  helm -n "$NS" history shortly --max 5
  exit 0
fi

# ------------------------------------------------------------------- canary (Argo Rollouts)
# Driven from here, so this script still answers "did the release work?" with its exit code:
#   1. helm applies the chart (hooks, i.e. migrations, still gate it) and returns
#   2. the Rollout runs its steps: 10% → analysis → 50% → analysis → 100%
#   3. Healthy on the new version: done. Aborted (analysis failed, canary never ready) or
#      out of time: helm rollback, so helm's history matches what runs, and exit 1.
# Knobs: CANARY_TIMEOUT (default 15m). The canary's synthetic traffic is a chart value
# (canary.analysis.syntheticTraffic.enabled), e.g. --set ...enabled=false with real users.
CANARY_TIMEOUT="${CANARY_TIMEOUT:-15m}"
ro() { kubectl -n "$NS" get rollout shortly -o jsonpath="$1" 2>/dev/null; }
seconds() { local d=$1; case "$d" in *m) echo $((${d%m} * 60)) ;; *s) echo "${d%s}" ;; *) echo "$d" ;; esac; }

# One-time move from the Deployment to the Rollout without a gap: helm would delete the
# Deployment as soon as the Rollout exists, before its pods are ready. "keep" makes helm
# leave it serving; it is deleted below once the Rollout is Healthy.
migrating=0
if kubectl -n "$NS" get deploy shortly >/dev/null 2>&1 && ! kubectl -n "$NS" get rollout shortly >/dev/null 2>&1; then
  echo "     migrating: the Deployment keeps serving until the Rollout is Healthy"
  kubectl -n "$NS" annotate deploy shortly helm.sh/resource-policy=keep --overwrite >/dev/null
  migrating=1
fi

# --wait=legacy: helm's default watcher doesn't understand a Rollout's status; the loop
# below does. It still waits for the hooks (migrations) and the Services.
# --force-conflicts: Argo Rollouts also writes to objects Helm applies (the stable/canary
# Services' selectors, the HTTPRoute's weights). Server-side apply calls that a conflict
# and refuses. Forcing gives Helm back only the fields it declares; the controller keeps
# the ones only it sets (the pod-template-hash selector), and resets the rest when it next
# acts. Safe because nothing upgrades in the middle of a canary: this script waits for it.
helm upgrade --install shortly "$ROOT/deploy/helm/shortly" "${helm_args[@]}" \
  --rollback-on-failure --force-conflicts --wait=legacy --timeout "$TIMEOUT"

# Let the controller see the new spec before reading its status.
gen=$(kubectl -n "$NS" get rollout shortly -o jsonpath='{.metadata.generation}')
for _ in $(seq 1 30); do [[ "$(ro '{.status.observedGeneration}')" == "$gen" ]] && break; sleep 1; done

# The canary's traffic is part of its analysis (chart: canary.analysis.syntheticTraffic),
# so a canary gets it however it started: here, or by Argo CD.
first_stable=$(ro '{.status.stableRS}')

# Follow the Rollout until it settles on the new version, or gives up on it.
deadline=$((SECONDS + $(seconds "$CANARY_TIMEOUT")))
last="" result=""
while :; do
  phase=$(ro '{.status.phase}') hash=$(ro '{.status.currentPodHash}') stable=$(ro '{.status.stableRS}')
  if [[ "$phase" == Healthy && -n "$hash" && "$hash" == "$stable" ]]; then result=ok; break; fi
  if [[ "$phase" == Degraded || "$(ro '{.status.abort}')" == true ]]; then result=aborted; break; fi
  if ((SECONDS > deadline)); then
    echo "     out of time after $CANARY_TIMEOUT: aborting the canary" >&2
    kubectl -n "$NS" patch rollout shortly --subresource=status --type=merge -p '{"status":{"abort":true}}' >/dev/null
    result=timeout; break
  fi
  if [[ -z "$first_stable" ]]; then # first install or migration: no steps, every pod is new
    line="first version, no canary: $phase $(ro '{.status.message}')"
  else
    weight=$(ro '{.status.canary.weights.canary.weight}')
    line="step $(ro '{.status.currentStepIndex}')/$(ro '{.spec.strategy.canary.steps}' | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))'), canary weight ${weight:-0}%, $phase $(ro '{.status.message}')"
  fi
  [[ "$line" != "$last" ]] && echo "     $line" && last=$line
  sleep 5
done

if [[ "$result" == ok ]]; then
  # A new Rollout starts at 1 replica until the HPA raises it to its floor (the chart leaves
  # `replicas` to the HPA). Don't call it done, or remove the old Deployment, before then.
  min=$(kubectl -n "$NS" get hpa shortly -o jsonpath='{.spec.minReplicas}' 2>/dev/null || true)
  for _ in $(seq 1 60); do
    r=$(ro '{.status.readyReplicas}')
    ((${r:-0} >= ${min:-1})) && break
    sleep 2
  done
  if ((migrating)); then
    kubectl -n "$NS" delete deploy shortly --wait=false >/dev/null
    echo "     migrated: the Rollout is Healthy; the old Deployment is deleted"
  fi
  echo "     Healthy: all traffic on the new version ($hash)"
  helm -n "$NS" history shortly --max 5
  exit 0
fi

# Aborted: the stable pods have kept (or got back) all traffic already. Roll helm back too,
# so the release history and the Rollout's spec say what is actually running.
echo "FAILED: canary $result: $(ro '{.status.message}')" >&2
run=$(kubectl -n "$NS" get analysisrun -l rollouts-pod-template-hash="$hash" \
  --sort-by=.metadata.creationTimestamp -o name 2>/dev/null | tail -n 1)
if [[ -n "$run" ]]; then
  kubectl -n "$NS" get "$run" -o jsonpath='{range .status.metricResults[*]}{"     "}{.name}{": "}{.phase}{" (last value "}{.measurements[-1:].value}{")"}{"\n"}{end}' >&2 || true
fi
helm -n "$NS" rollback shortly --force-conflicts --wait=legacy --timeout "$TIMEOUT" >&2
# A rollback inside rollbackWindow skips the steps: back on stable within seconds.
for _ in $(seq 1 60); do
  [[ "$(ro '{.status.phase}')" == Healthy ]] && break
  sleep 2
done
echo "     rolled back to helm revision $(helm -n "$NS" history shortly --max 1 -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)[-1]["revision"])'), Rollout $(ro '{.status.phase}')" >&2
exit 1
