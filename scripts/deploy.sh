#!/usr/bin/env bash
# Deploy one image to one environment. The SAME image reference is promoted from staging
# to prod — build once, deploy many; never rebuild for production.
#
#   scripts/deploy.sh staging ghcr.io/statsguy/shortly@sha256:...   # released image (preferred)
#   scripts/deploy.sh prod    ghcr.io/statsguy/shortly@sha256:...
#   scripts/deploy.sh staging shortly:dev                            # local image, loaded into kind
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

if [[ "$REF" == *@sha256:* ]]; then
  image_args=(--set "image.repository=${REF%@*}" --set "image.digest=${REF#*@}")
else
  image_args=(--set "image.repository=${REF%:*}" --set "image.tag=${REF##*:}")
  # A local tag only exists on this machine; copy it into the kind node.
  if kubectl config current-context 2>/dev/null | grep -q '^kind-' && docker image inspect "$REF" >/dev/null 2>&1; then
    kind load docker-image "$REF" --name "$(kubectl config current-context | sed 's/^kind-//')"
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
