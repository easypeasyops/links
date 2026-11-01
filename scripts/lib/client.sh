# shellcheck shell=bash
# An in-cluster client: one long-lived pod per namespace, "client", for requests that must
# come from inside the cluster (through a Service, the Gateway, or to test a network
# policy). It carries no app label, so every NetworkPolicy treats it as a stranger.
# Busybox wget and nc, from the Postgres image the admission policy already admits; the
# restricted Pod Security Standard's settings. (The old Postgres pod, postgres-0, used to
# double as this until platform plan 4.3 removed it.)
CLIENT_IMAGE=postgres:17-alpine

client_ready() { # <namespace>: create the pod if it isn't there, and wait until it's Ready
  local ns=$1
  if ! kubectl -n "$ns" get pod client >/dev/null 2>&1; then
    kubectl -n "$ns" run client --image="$CLIENT_IMAGE" --restart=Never --labels=app=e2e-client \
      --overrides='{"spec":{"automountServiceAccountToken":false,"terminationGracePeriodSeconds":0,
        "securityContext":{"runAsNonRoot":true,"runAsUser":70,"seccompProfile":{"type":"RuntimeDefault"}},
        "containers":[{"name":"client","image":"'"$CLIENT_IMAGE"'","command":["sh","-c","while :; do sleep 3600; done"],
          "securityContext":{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]}}}]}}' >/dev/null
  fi
  kubectl -n "$ns" wait pod/client --for=condition=Ready --timeout=120s >/dev/null
}
