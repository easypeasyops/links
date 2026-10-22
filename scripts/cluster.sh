#!/usr/bin/env bash
# Local Kubernetes for shortly: one kind cluster, two namespaces (staging, prod), each with
# its own Postgres and secrets — the shape of real environments, on a laptop.
#
#   scripts/cluster.sh up              # idempotent
#   MONITORING=0 scripts/cluster.sh up # skip kube-prometheus-stack (saves ~1 GB of RAM)
#   POLICIES=0 scripts/cluster.sh up   # skip Kyverno and the admission policies
#   BACKUPS=0 scripts/cluster.sh up    # skip the backup bucket and the backup CronJobs
#   scripts/cluster.sh down
set -euo pipefail

CLUSTER=shortly
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENVS=(staging prod)
# metrics-server feeds CPU usage to the HorizontalPodAutoscaler. Pinned version + checksum:
# a manifest fetched at runtime is code you run with cluster-admin.
METRICS_SERVER_VERSION=v0.9.0
METRICS_SERVER_SHA256=1cec29a5267809306a2c6ec74a3e449abbb705b4a8beed0c8a1963910f72c79b
# kube-prometheus-stack: Prometheus Operator, Prometheus, Alertmanager, kube-state-metrics,
# Grafana. Pinned by version AND digest: the tag says what it is, the digest that it can't change.
MONITORING_CHART=oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack
MONITORING_VERSION=91.9.0
MONITORING_DIGEST=sha256:04047e500c803dc8d2bc284ecb43e9229bb32b5c36bd13fc89592c8a2f3cdc72
# Kyverno enforces deploy/policies/ at admission (release provenance, image allowlist).
# Chart 3.9.1 = Kyverno v1.19.1, the engine tests/test_admission_policies.py runs.
KYVERNO_CHART=oci://ghcr.io/kyverno/charts/kyverno
KYVERNO_VERSION=3.9.1
KYVERNO_DIGEST=sha256:720542b97bdbc2633535c2297332690d21cfbb944053eb1ec1f0dbe8e3895444

up() {
  if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    kind create cluster --config "$ROOT/deploy/kind/cluster.yaml" --wait 120s
  fi
  kubectl config use-context "kind-$CLUSTER" >/dev/null
  # The three add-ons don't depend on each other, so they install side by side (about 25 s
  # faster than one after another). Kyverno's policies cover staging and prod only, so they
  # can't get in the way of the monitoring pods. All three finish before the app namespaces:
  # the shortly chart's ServiceMonitor and PrometheusRule need the stack's CRDs, and from
  # here on the API server refuses unverified shortly images.
  local pids=() failed=0
  install_metrics_server & pids+=($!)
  if [[ "${MONITORING:-1}" != 0 ]]; then install_monitoring & pids+=($!); fi
  if [[ "${POLICIES:-1}" != 0 ]]; then install_policies & pids+=($!); fi
  if [[ "${BACKUPS:-1}" != 0 ]]; then install_backups & pids+=($!); fi
  for pid in "${pids[@]}"; do wait "$pid" || failed=1; done
  ((failed == 0)) || { echo "an add-on failed to install (its error is above)" >&2; exit 1; }

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
    # Default-deny ingress, and Postgres open to shortly's pods only. The chart adds the
    # app's own policy (deploy/helm/shortly/templates/networkpolicy.yaml).
    kubectl -n "$ns" apply -f "$ROOT/deploy/kind/network-policies.yaml" >/dev/null
    if [[ "${BACKUPS:-1}" != 0 ]]; then
      # Each environment gets the bucket credentials and an hourly backup CronJob.
      if ! kubectl -n "$ns" get secret backup-s3 >/dev/null 2>&1; then
        kubectl -n "$ns" create secret generic backup-s3 \
          --from-literal=access-key="$(kubectl -n backups get secret seaweedfs-s3 -o jsonpath='{.data.access-key}' | base64 -d)" \
          --from-literal=secret-key="$(kubectl -n backups get secret seaweedfs-s3 -o jsonpath='{.data.secret-key}' | base64 -d)" >/dev/null
      fi
      kubectl -n "$ns" apply -f "$ROOT/deploy/kind/postgres-backup.yaml" >/dev/null
    fi
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
  echo "metrics-server ready: $METRICS_SERVER_VERSION"
}

install_monitoring() {
  kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  # "baseline", not "restricted": some of the stack's pods don't meet "restricted" yet.
  # baseline still forbids privileged pods and host namespaces (hence no node-exporter).
  kubectl label namespace monitoring --overwrite pod-security.kubernetes.io/enforce=baseline >/dev/null
  if ! kubectl -n monitoring get secret grafana-admin >/dev/null 2>&1; then
    pw=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24 || true)
    kubectl -n monitoring create secret generic grafana-admin \
      --from-literal=admin-user=admin --from-literal=admin-password="$pw" >/dev/null
  fi
  helm upgrade --install monitoring "$MONITORING_CHART@$MONITORING_DIGEST" \
    --version "$MONITORING_VERSION" --namespace monitoring \
    --values "$ROOT/deploy/kind/monitoring-values.yaml" --wait --timeout 10m >/dev/null
  echo "monitoring ready: kube-prometheus-stack $MONITORING_VERSION in namespace monitoring"
}

install_policies() {
  kubectl create namespace kyverno --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl label namespace kyverno --overwrite pod-security.kubernetes.io/enforce=restricted >/dev/null
  helm upgrade --install kyverno "$KYVERNO_CHART@$KYVERNO_DIGEST" \
    --version "$KYVERNO_VERSION" --namespace kyverno \
    --values "$ROOT/deploy/kind/kyverno-values.yaml" --wait --timeout 10m >/dev/null
  # helm --wait can return before Kyverno's own webhook (which validates the policies we're
  # about to apply) is serving: wait for the rollout, then retry the apply until it answers.
  kubectl -n kyverno rollout status deploy/kyverno-admission-controller --timeout=180s >/dev/null
  for i in $(seq 1 30); do
    kubectl apply -f "$ROOT/deploy/policies/" >/dev/null 2>/tmp/kyverno-apply.err && break
    ((i < 30)) || { cat /tmp/kyverno-apply.err >&2; exit 1; }
    sleep 4
  done
  # A policy only protects once Kyverno has compiled it and registered its webhook.
  for kind in imagevalidatingpolicy validatingpolicy; do
    kubectl wait "$kind" --all --for=jsonpath='{.status.conditionStatus.ready}'=true \
      --timeout=120s >/dev/null
  done
  echo "policies ready: Kyverno $KYVERNO_VERSION, $(ls "$ROOT"/deploy/policies/*.yaml | wc -l | tr -d ' ') policies"
}

install_backups() {
  kubectl create namespace backups --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl label namespace backups --overwrite pod-security.kubernetes.io/enforce=restricted >/dev/null
  # One S3 identity, generated once, never in git. SeaweedFS reads it from s3.json; the
  # same keys are copied into each app namespace for the backup Job (see up()).
  if ! kubectl -n backups get secret seaweedfs-s3 >/dev/null 2>&1; then
    local key secret
    key=$(LC_ALL=C tr -dc 'A-Z0-9' </dev/urandom | head -c 20 || true)
    secret=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40 || true)
    kubectl -n backups create secret generic seaweedfs-s3 \
      --from-literal=access-key="$key" --from-literal=secret-key="$secret" \
      --from-literal=s3.json="{\"identities\":[{\"name\":\"backup\",\"credentials\":[{\"accessKey\":\"$key\",\"secretKey\":\"$secret\"}],\"actions\":[\"Admin\",\"Read\",\"Write\",\"List\",\"Tagging\"]}]}" >/dev/null
  fi
  kubectl -n backups apply -f "$ROOT/deploy/kind/backups.yaml" >/dev/null
  kubectl -n backups rollout status statefulset/seaweedfs --timeout=180s >/dev/null
  echo "backups ready: SeaweedFS S3 in namespace backups"
}

down() {
  kind delete cluster --name "$CLUSTER"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  *) echo "usage: $0 up|down" >&2; exit 2 ;;
esac
