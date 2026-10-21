#!/usr/bin/env bash
# Local Kubernetes for shortly: one kind cluster, two namespaces (staging, prod), each with
# its own Postgres and secrets — the shape of real environments, on a laptop.
#
#   scripts/cluster.sh up      # idempotent
#   scripts/cluster.sh down
set -euo pipefail

CLUSTER=shortly
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENVS=(staging prod)
# metrics-server feeds CPU usage to the HorizontalPodAutoscaler. Pinned version + checksum:
# a manifest fetched at runtime is code you run with cluster-admin.
METRICS_SERVER_VERSION=v0.9.0
METRICS_SERVER_SHA256=1cec29a5267809306a2c6ec74a3e449abbb705b4a8beed0c8a1963910f72c79b

up() {
  if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    kind create cluster --config "$ROOT/deploy/kind/cluster.yaml" --wait 120s
  fi
  kubectl config use-context "kind-$CLUSTER" >/dev/null
  install_metrics_server

  for ns in "${ENVS[@]}"; do
    kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    # Enforce the "restricted" Pod Security Standard: the API server itself rejects any pod
    # that runs as root, keeps capabilities, or allows privilege escalation.
    kubectl label namespace "$ns" --overwrite \
      pod-security.kubernetes.io/enforce=restricted \
      pod-security.kubernetes.io/warn=restricted >/dev/null

    # Secrets are generated here, once, and never written to git. In a real cluster they'd
    # come from a secret manager (External Secrets, Vault, Sealed Secrets).
    if ! kubectl -n "$ns" get secret postgres-auth >/dev/null 2>&1; then
      pw=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32 || true)
      kubectl -n "$ns" create secret generic postgres-auth --from-literal=password="$pw" >/dev/null
      kubectl -n "$ns" create secret generic shortly-db \
        --from-literal=url="postgresql://shortly:${pw}@postgres:5432/shortly" >/dev/null
    fi
    kubectl -n "$ns" apply -f "$ROOT/deploy/kind/postgres.yaml" >/dev/null
  done

  for ns in "${ENVS[@]}"; do
    kubectl -n "$ns" rollout status statefulset/postgres --timeout=180s
  done
  echo "cluster ready: namespaces ${ENVS[*]}"
}

install_metrics_server() {
  local manifest
  manifest=$(mktemp)
  curl -sSfL -o "$manifest" \
    "https://github.com/kubernetes-sigs/metrics-server/releases/download/$METRICS_SERVER_VERSION/components.yaml"
  echo "$METRICS_SERVER_SHA256  $manifest" | sha256sum -c --quiet - \
    || { echo "metrics-server manifest checksum mismatch" >&2; exit 1; }
  kubectl apply -f "$manifest" >/dev/null
  rm -f "$manifest"
  # kind kubelets serve self-signed certs. Local clusters only: never do this in a real one.
  if ! kubectl -n kube-system get deploy metrics-server -o jsonpath='{.spec.template.spec.containers[0].args}' \
    | grep -q kubelet-insecure-tls; then
    kubectl -n kube-system patch deployment metrics-server --type=json \
      -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' >/dev/null
  fi
  kubectl -n kube-system rollout status deploy/metrics-server --timeout=120s >/dev/null
}

down() {
  kind delete cluster --name "$CLUSTER"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  *) echo "usage: $0 up|down" >&2; exit 2 ;;
esac
