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
# Extra args are passed to helm (e.g. --set config.logLevel=DEBUG).
# A failed rollout is rolled back automatically (--rollback-on-failure).
set -euo pipefail

ENV="${1:?usage: $0 <staging|prod> <image-ref> [helm args...]}"
REF="${2:?usage: $0 <staging|prod> <image-ref> [helm args...]}"
shift 2
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TIMEOUT="${DEPLOY_TIMEOUT:-3m}"

case "$ENV" in staging | prod) ;; *) echo "unknown env: $ENV" >&2; exit 2 ;; esac

context=$(kubectl config current-context 2>/dev/null || true)

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
  image_args=(--set "image.repository=${REF%:*}" --set "image.tag=${REF##*:}")
  # A local tag only exists on this machine; copy it into the kind node.
  if docker image inspect "$REF" >/dev/null 2>&1; then
    kind load docker-image "$REF" --name "${context#kind-}"
  fi
fi

echo "deploying $REF → $ENV"
helm upgrade --install shortly "$ROOT/deploy/helm/shortly" \
  --namespace "$ENV" \
  --values "$ROOT/deploy/envs/$ENV.yaml" \
  "${image_args[@]}" \
  --rollback-on-failure \
  --wait \
  --timeout "$TIMEOUT" \
  "$@"
kubectl -n "$ENV" rollout status deploy/shortly --timeout=60s
helm -n "$ENV" history shortly --max 5
