#!/usr/bin/env bash
# Local Kubernetes for shortly: one kind cluster, two namespaces (staging, prod), each with
# its own Postgres and secrets — the shape of real environments, on a laptop.
#
#   scripts/cluster.sh up              # idempotent
#   MONITORING=0 scripts/cluster.sh up # skip kube-prometheus-stack (saves ~1 GB of RAM)
#   POLICIES=0 scripts/cluster.sh up   # skip Kyverno and the admission policies
#   BACKUPS=0 scripts/cluster.sh up    # skip the backup bucket and the backup CronJobs
#   ARGOCD_APPS=0 scripts/cluster.sh up # don't let Argo CD manage the app (CI: the e2e
#                                       # deploys with deploy.sh instead)
#   PLATFORM_REVISION=<sha> scripts/cluster.sh up # the platform from that commit, not main
#   FRONT_DOOR=0 scripts/cluster.sh up  # no forwarders on the laptop's ports 8080 and 8090
#   scripts/cluster.sh wake            # after the laptop slept: restart the CronJob
#                                       # scheduler, back up any environment that's overdue
#   scripts/cluster.sh down
#
# What runs where:
#   - Argo CD: bootstrapped here with Helm, the one component not deployed from git.
#   - The platform (Envoy Gateway and the Gateway, Argo Rollouts, metrics-server,
#     kube-prometheus-stack, Kyverno and its policies, the backup bucket, the tool routes):
#     Argo CD, from deploy/argocd/platform/ (app-of-apps). A merged change to it reaches the
#     cluster within a minute; this script only applies the root and waits for it.
#   - Secrets, Postgres, network policies and backup CronJobs per environment: here, until
#     secrets come from a secret manager instead of being generated on the laptop.
#   - The app: Argo CD, from deploy/argocd/applications.yaml.
set -euo pipefail

CLUSTER=shortly
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENVS=(staging prod)
# Argo CD: deploys the platform and the app from git. Pinned by version and digest: the
# tag says what it is, the digest that it can't change. (The platform's chart pins are in
# deploy/argocd/platform/values.yaml.)
ARGOCD_CHART=oci://ghcr.io/argoproj/argo-helm/argo-cd
ARGOCD_VERSION=10.11.1 # Argo CD v3.5.6
ARGOCD_DIGEST=sha256:2ac5224a5c201789f96aa9e085673d80732591d1b3e815a8a825a4db6d1d7c88
# The front door: one tiny socat container per port, beside the kind node, forwarding the
# laptop's 127.0.0.1:<host> to the Gateway's fixed node port (deploy/kind/gateway.yaml).
# A port-forward dies with its terminal, the laptop's sleep or the pod it pinned; these
# restart with Docker and outlive every release. Containers rather than kind's
# extraPortMappings, which only take effect when the cluster is created.
FRONT_DOOR_IMAGE=docker.io/alpine/socat:1.8.1.3@sha256:22d77becaeeea8ef56b8c4bb2312323acafe85354be6a85714ff5050e3537842
FRONT_DOOR_PORTS=("8080:30080" "8090:30081") # host:nodePort, users (http) and dev tools (tools)
# Releases scripts/cluster.sh used to install with Helm, now Argo CD's (namespace/release):
# their Helm records are removed once Argo CD has adopted the resources.
HELM_RELEASES=(monitoring/monitoring kyverno/kyverno envoy-gateway-system/eg argo-rollouts/argo-rollouts)

on() { [[ "${!1:-1}" != 0 ]]; } # on MONITORING: the knob is unset or anything but 0
bool() { if on "$1"; then echo true; else echo false; fi; }

up() {
  if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
    kind create cluster --config "$ROOT/deploy/kind/cluster.yaml" --wait 120s
  fi
  kubectl config use-context "kind-$CLUSTER" >/dev/null
  install_argocd
  platform_secrets
  platform
  take_over_from_helm
  if on FRONT_DOOR; then front_door; fi

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
    if on BACKUPS; then
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

  # Last, once Postgres and the secrets exist: from here on, git decides what runs.
  if on ARGOCD_APPS; then
    kubectl apply -f "$ROOT/deploy/argocd/project.yaml" -f "$ROOT/deploy/argocd/applications.yaml" >/dev/null
    for ns in "${ENVS[@]}"; do wait_synced "shortly-$ns" 300; done
    kubectl -n argocd get applications
  fi
}

# Wait for an Application to be Synced AND Healthy. Healthy alone comes first when Argo CD
# adopts resources that already run, before its first sync has applied git (seen on the
# first takeover: OutOfSync/Healthy). On timeout, say what is out of sync, and fail.
wait_synced() {
  local app=$1 timeout=$2 state="" t0=$SECONDS last=$SECONDS
  while ((SECONDS - t0 < timeout)); do
    state=$(kubectl -n argocd get application "$app" \
      -o jsonpath='{.status.sync.status}/{.status.health.status}' 2>/dev/null || true)
    [[ "$state" == Synced/Healthy ]] && return 0
    if ((SECONDS - last >= 30)); then # progress, so a slow sync shows where it is
      last=$SECONDS
      echo "  ... $app is ${state:-not there yet} after $((SECONDS - t0))s; $(kubectl -n argocd get applications \
        -o jsonpath='{range .items[*]}{.metadata.name}={.status.sync.status}/{.status.health.status} {end}' 2>/dev/null)"
    fi
    sleep 3
  done
  echo "FAIL: Argo CD application $app is ${state:-missing} after ${timeout}s:" >&2
  kubectl -n argocd get application "$app" -o jsonpath='{range .status.resources[?(@.status!="Synced")]}  out of sync: {.kind}/{.name}{"\n"}{end}{range .status.resources[?(@.health.status!="Healthy")]}  {.health.status}: {.kind}/{.name} {.health.message}{"\n"}{end}{range .status.conditions[*]}  {.type}: {.message}{"\n"}{end}{.status.operationState.message}{"\n"}' >&2 || true
  return 1
}

# Secrets the platform needs before it syncs. Argo CD deploys only what is in git, and git
# holds no secrets, so they are generated here, once (a secret manager later).
platform_secrets() {
  if on MONITORING; then
    kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    if ! kubectl -n monitoring get secret grafana-admin >/dev/null 2>&1; then
      pw=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24 || true)
      kubectl -n monitoring create secret generic grafana-admin \
        --from-literal=admin-user=admin --from-literal=admin-password="$pw" >/dev/null
    fi
  fi
  if on BACKUPS; then
    # One S3 identity. SeaweedFS reads it from s3.json; the same keys are copied into each
    # app namespace for the backup Job (see up()).
    kubectl create namespace backups --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    if ! kubectl -n backups get secret seaweedfs-s3 >/dev/null 2>&1; then
      local key secret
      key=$(LC_ALL=C tr -dc 'A-Z0-9' </dev/urandom | head -c 20 || true)
      secret=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40 || true)
      kubectl -n backups create secret generic seaweedfs-s3 \
        --from-literal=access-key="$key" --from-literal=secret-key="$secret" \
        --from-literal=s3.json="{\"identities\":[{\"name\":\"backup\",\"credentials\":[{\"accessKey\":\"$key\",\"secretKey\":\"$secret\"}],\"actions\":[\"Admin\",\"Read\",\"Write\",\"List\",\"Tagging\"]}]}" >/dev/null
    fi
  fi
}

# Apply the platform's root Application and wait until every child is Synced and Healthy.
# Its Helm parameters are what git can't know: the node's architecture, which commit to
# deploy, and which components this cluster wants.
platform() {
  local arch revision params children
  arch=$(kubectl get nodes -o jsonpath='{.items[0].status.nodeInfo.architecture}')
  revision=${PLATFORM_REVISION:-main}
  params=$(printf '[{"name":"arch","value":"%s"},{"name":"revision","value":"%s"},{"name":"components.monitoring","value":"%s"},{"name":"components.policies","value":"%s"},{"name":"components.backups","value":"%s"}]' \
    "$arch" "$revision" "$(bool MONITORING)" "$(bool POLICIES)" "$(bool BACKUPS)")
  kubectl apply -f "$ROOT/deploy/argocd/platform-project.yaml" >/dev/null
  # One apply with the parameters already set: applying the file as it is, then patching,
  # would first sync main on the wrong architecture.
  kubectl apply -f "$ROOT/deploy/argocd/platform-root.yaml" --dry-run=client -o json \
    | REVISION="$revision" PARAMS="$params" python3 -c '
import json, os, sys
app = json.load(sys.stdin)
app["spec"]["source"]["targetRevision"] = os.environ["REVISION"]
app["spec"]["source"]["helm"]["parameters"] = json.loads(os.environ["PARAMS"])
json.dump(app, sys.stdout)' | kubectl apply -f - >/dev/null
  echo "platform: syncing deploy/argocd/platform at $revision ($arch) through Argo CD"
  # The root is Healthy once every wave is: Argo CD reports each child's health
  # (argocd-cm, deploy/kind/argocd-values.yaml) and syncs wave 1 only after wave 0.
  wait_synced platform 600 || exit 1
  children=$(kubectl -n argocd get applications -l app.kubernetes.io/part-of=platform -o name)
  for app in $children; do wait_synced "${app#*/}" 300 || exit 1; done
  kubectl -n argocd get applications -l app.kubernetes.io/part-of=platform
  # Argo CD's health says the pods run; these say they do their job.
  kubectl -n gateway wait gateway/shortly --for=condition=Programmed --timeout=180s >/dev/null
  if on POLICIES; then
    # A policy only protects once Kyverno has compiled it and registered its webhook.
    for kind in imagevalidatingpolicy validatingpolicy; do
      kubectl wait "$kind" --all --for=jsonpath='{.status.conditionStatus.ready}'=true \
        --timeout=120s >/dev/null
    done
  fi
  echo "platform ready: $(echo "$children" | wc -l | tr -d ' ') Applications Synced and Healthy"
}

# A cluster built before the platform moved under Argo CD has Helm releases for the same
# resources. Once Argo CD manages them, the Helm records only mislead (helm list would show
# releases nobody upgrades): remove them. The resources stay; Argo CD owns them now.
take_over_from_helm() {
  local entry ns release
  for entry in "${HELM_RELEASES[@]}"; do
    ns=${entry%/*} release=${entry#*/}
    if kubectl -n "$ns" get secret -l "owner=helm,name=$release" -o name 2>/dev/null | grep -q .; then
      kubectl -n "$ns" delete secret -l "owner=helm,name=$release" >/dev/null
      echo "helm release $ns/$release handed over to Argo CD (its Helm records removed)"
    fi
  done
}

install_argocd() {
  kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl label namespace argocd --overwrite pod-security.kubernetes.io/enforce=restricted >/dev/null
  if ! helm upgrade --install argocd "$ARGOCD_CHART@$ARGOCD_DIGEST" --version "$ARGOCD_VERSION" \
    --namespace argocd --values "$ROOT/deploy/kind/argocd-values.yaml" \
    --wait --timeout 5m >/dev/null; then
    kubectl -n argocd get pods -o wide >&2 || true
    kubectl -n argocd get events --sort-by=.lastTimestamp 2>/dev/null | grep -iE 'fail|error|back-off' | tail -10 >&2 || true
    return 1
  fi
  echo "argocd ready: Argo CD (chart $ARGOCD_VERSION) in namespace argocd"
}

# Start (or replace) the front-door forwarders. Not fatal: the cluster works without them,
# through kubectl port-forward (RUNBOOK, Local cluster operations).
front_door() {
  local node="$CLUSTER-control-plane" mapping host node_port name
  for mapping in "${FRONT_DOOR_PORTS[@]}"; do
    host=${mapping%%:*} node_port=${mapping##*:} name="$CLUSTER-front-door-$host"
    docker rm -f "$name" >/dev/null 2>&1 || true
    # 127.0.0.1 only: the tools listener has no login of its own (Prometheus, Alertmanager).
    if ! docker run -d --name "$name" --restart unless-stopped --network kind \
      -p "127.0.0.1:$host:$host" "$FRONT_DOOR_IMAGE" \
      "tcp-listen:$host,fork,reuseaddr" "tcp-connect:$node:$node_port" >/dev/null; then
      docker rm -f "$name" >/dev/null 2>&1 || true
      echo "WARNING: front door: port $host is taken on this machine (an old kubectl port-forward?):" >&2
      lsof -nP -iTCP:"$host" -sTCP:LISTEN >&2 2>/dev/null || true
      echo "  stop it and rerun scripts/cluster.sh up" >&2
    fi
  done
  echo "front door: http://sho.rt.localhost:8080 (prod), http://staging.sho.rt.localhost:8080,"
  echo "  http://{argocd,grafana,prometheus,alertmanager}.localhost:8090"
}

# A sleeping Mac pauses Docker's VM, and the CronJob controller's timers only count time
# while it runs: at 10% awake, "the next run in an hour" comes 9 hours later (seen on
# 2026-10-24: backups at 22:41, 07:41, 16:41 and none between). Restarting the controller
# makes it read every CronJob against the wall clock at once; startingDeadlineSeconds (50
# minutes) lets it start a run it just missed. Backups older than WAKE_BACKUP_MAX_AGE
# seconds (default an hour, the schedule) are taken right away instead of waiting.
wake() {
  local node="$CLUSTER-control-plane" before ns last age
  kubectl config use-context "kind-$CLUSTER" >/dev/null
  before=$(kubectl -n kube-system get pod "kube-controller-manager-$node" \
    -o jsonpath='{.status.containerStatuses[0].restartCount}')
  # A static pod: stopping its container makes the kubelet start a fresh one.
  docker exec "$node" sh -c 'crictl stop $(crictl ps -q --name kube-controller-manager)' >/dev/null
  for _ in $(seq 1 60); do
    [[ "$(kubectl -n kube-system get pod "kube-controller-manager-$node" \
      -o jsonpath='{.status.containerStatuses[0].restartCount}/{.status.containerStatuses[0].ready}' 2>/dev/null)" \
      == "$((before + 1))/true" ]] && break
    sleep 2
  done
  echo "kube-controller-manager restarted (restarts: $((before + 1))): CronJobs rescheduled from the wall clock"
  for ns in "${ENVS[@]}"; do
    kubectl -n "$ns" get cronjob postgres-backup >/dev/null 2>&1 || continue
    last=$(kubectl -n "$ns" get cronjob postgres-backup -o jsonpath='{.status.lastSuccessfulTime}')
    age=$(python3 -c "import datetime as d,sys; t=sys.argv[1]; print(int((d.datetime.now(d.timezone.utc)-d.datetime.fromisoformat(t.replace('Z','+00:00'))).total_seconds()) if t else 10**9)" "$last")
    if ((age > ${WAKE_BACKUP_MAX_AGE:-3600})); then
      kubectl -n "$ns" create job "backup-wake-$(date +%s)" --from=cronjob/postgres-backup >/dev/null
      echo "$ns: last backup ${last:-never}: started one now (kubectl -n $ns get jobs)"
    else
      echo "$ns: last backup $last: recent enough"
    fi
  done
}

down() {
  local mapping
  for mapping in "${FRONT_DOOR_PORTS[@]}"; do
    docker rm -f "$CLUSTER-front-door-${mapping%%:*}" >/dev/null 2>&1 || true
  done
  kind delete cluster --name "$CLUSTER"
}

case "${1:-}" in
  up) up ;;
  wake) wake ;;
  down) down ;;
  *) echo "usage: $0 up|wake|down" >&2; exit 2 ;;
esac
