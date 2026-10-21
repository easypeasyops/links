#!/usr/bin/env bash
# Autoscaling drill: drive load at a namespace's Service from INSIDE the cluster and check
# that the HorizontalPodAutoscaler adds ready pods. Run by scripts/k8s-e2e.sh (and so by
# CI), or on its own against a deployed environment:
#
#   scripts/hpa-drill.sh                     # prod, 1000 req/s for 100 s
#   NS=prod RATE=1500 DURATION=240s scripts/hpa-drill.sh
#
# It gates on scaling only. k6's latency thresholds are expected to fail during the first
# seconds of the spike (that is the gap autoscaling has to close); they are printed, not
# asserted. Capacity itself is measured with scripts/loadtest.sh, not here.
set -euo pipefail

NS="${NS:-prod}"
# Must push CPU clearly past the target: the HPA ignores ratios within 10% of it (its
# tolerance), and per-request CPU varies by hardware. 600 req/s gave 75% vs a 70% target on
# a GitHub runner (ratio 1.07, so no scale-up): too close. 1000 lands well past 77%.
RATE="${RATE:-1000}"
# 100 s: in CI the first new pod is ready after 30-55 s, so this leaves about 2x margin.
DURATION="${DURATION:-100s}"
K6_IMAGE="${K6_IMAGE:-grafana/k6:2.3.0}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
JOB=k6-hpa-drill

pass() { echo "ok   $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }
cleanup() {
  kubectl -n "$NS" delete job "$JOB" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  kubectl -n "$NS" delete configmap "$JOB" --ignore-not-found >/dev/null 2>&1 || true
}
trap cleanup EXIT

hpa() { kubectl -n "$NS" get hpa shortly -o jsonpath="$1" 2>/dev/null; }
ready() { kubectl -n "$NS" get deploy shortly -o jsonpath='{.status.readyReplicas}'; }

kubectl -n "$NS" get hpa shortly >/dev/null 2>&1 || fail "no HPA in $NS (autoscaling.enabled=false?)"

# 1. The HPA can read CPU at all. "<unknown>" here means metrics-server is missing or the
#    pods have no CPU request; the HPA then silently never scales.
for _ in $(seq 1 60); do
  util=$(hpa '{.status.currentMetrics[0].resource.current.averageUtilization}')
  [[ -n "$util" ]] && break
  sleep 3
done
[[ -n "$util" ]] || fail "HPA has no CPU metrics after 180 s (is metrics-server running?)"
min=$(hpa '{.spec.minReplicas}')
max=$(hpa '{.spec.maxReplicas}')
pass "HPA reads CPU: ${util}% of request, replicas $(ready) (min $min, max $max)"

# 2. Load from a pod in the namespace, through the Service, under the restricted PSS.
kubectl -n "$NS" create configmap "$JOB" --from-file=mixed.js="$ROOT/loadtest/k6/mixed.js" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n "$NS" apply -f - >/dev/null <<YAML
apiVersion: batch/v1
kind: Job
metadata:
  name: $JOB
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 300
  template:
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext:
        runAsNonRoot: true
        runAsUser: 12345
        runAsGroup: 12345
        seccompProfile: { type: RuntimeDefault }
      containers:
        - name: k6
          image: $K6_IMAGE
          args: [run, --quiet, --no-color, /scripts/mixed.js]
          env:
            - { name: BASE_URL, value: "http://shortly" }
            - { name: RATE, value: "$RATE" }
            - { name: DURATION, value: "$DURATION" }
            - { name: SEED_LINKS, value: "100" }
          securityContext:
            allowPrivilegeEscalation: false
            capabilities: { drop: [ALL] }
          volumeMounts:
            - { name: scripts, mountPath: /scripts, readOnly: true }
      volumes:
        - name: scripts
          configMap: { name: $JOB }
YAML
echo "     load: $RATE req/s for $DURATION against $NS"

# 3. Watch until the load finishes.
start=$SECONDS peak=0 first_scale="" max_cpu=0
while :; do
  r=$(ready); r=${r:-0}
  ((r > peak)) && peak=$r
  [[ -z "$first_scale" && $r -gt $min ]] && first_scale=$((SECONDS - start))
  cpu=$(hpa '{.status.currentMetrics[0].resource.current.averageUtilization}'); cpu=${cpu:-0}
  ((cpu > max_cpu)) && max_cpu=$cpu
  printf '     t=+%3ss  ready=%s  desired=%s  cpu=%s%%\n' $((SECONDS - start)) "$r" \
    "$(hpa '{.status.desiredReplicas}')" "$cpu"
  done_count=$(kubectl -n "$NS" get job "$JOB" -o jsonpath='{.status.succeeded}{.status.failed}')
  [[ -n "$done_count" ]] && break
  ((SECONDS - start < 600)) || fail "load job did not finish in 600 s"
  sleep 10
done
echo "     k6 summary (informational):"
kubectl -n "$NS" logs "job/$JOB" 2>/dev/null | grep -E '✓|✗|http_req_duration|http_req_failed|http_reqs' | sed 's/^/       /' || true

target=$(hpa '{.spec.metrics[0].resource.target.averageUtilization}')
if ((peak <= min)); then
  # Tell "the load was too light to need a new pod" apart from "the HPA failed to act".
  if ((max_cpu * 10 <= target * 11)); then
    fail "load too light: CPU peaked at ${max_cpu}% vs target ${target}% (+10% HPA tolerance); raise RATE"
  fi
  fail "HPA never scaled past $min ready replicas although CPU reached ${max_cpu}% (target ${target}%)"
fi
pass "HPA scaled $min → $peak ready replicas; first new pod ready after ${first_scale}s"
