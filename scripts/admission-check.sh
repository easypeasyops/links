#!/usr/bin/env bash
# Admission policy check (scripts/k8s-e2e.sh step 8): the live API server, through Kyverno,
# refuses shortly images without release provenance and reports images outside the allowlist.
# Every request is a server-side dry run: it goes through admission webhooks, creates nothing.
#
#   scripts/admission-check.sh            # against the kind cluster from scripts/cluster.sh
#
# Needs network from the cluster to ghcr.io and Sigstore (Rekor, TUF), like a real cluster.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NS="${NS:-staging}"
REPO=ghcr.io/statsguy/shortly
# The release the runbook documents (also checked by tests/test_deploy_gate.py).
RELEASE=$(sed -nE 's/^\| Current release \| v[0-9.]+, `(sha256:[0-9a-f]{64})`.*/\1/p' "$ROOT/docs/RUNBOOK.md")
[[ -n "$RELEASE" ]] || { echo "FAIL: no 'Current release' digest in RUNBOOK" >&2; exit 1; }
# v0.2.0's linux/amd64 manifest: a real image, but the attestation names the index only.
UNATTESTED=sha256:7025c409513225aeb5e473d2e08951cfd8cbbe9d1f574a0ac042ad8e3aa0d4ec
MISSING="sha256:$(printf '0%.0s' {1..64})"

pass() { echo "ok   $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

# A Pod that the "restricted" Pod Security Standard admits, so only Kyverno can refuse it.
pod() {
  cat <<YAML
apiVersion: v1
kind: Pod
metadata: { name: admission-check, namespace: $NS }
spec:
  automountServiceAccountToken: false
  securityContext: { runAsNonRoot: true, runAsUser: 10001, seccompProfile: { type: RuntimeDefault } }
  containers:
    - name: app
      image: $1
      securityContext: { allowPrivilegeEscalation: false, capabilities: { drop: [ALL] } }
YAML
}

deployment() {
  cat <<YAML
apiVersion: apps/v1
kind: Deployment
metadata: { name: admission-check, namespace: $NS }
spec:
  selector: { matchLabels: { app: admission-check } }
  template:
    metadata: { labels: { app: admission-check } }
    spec:
      automountServiceAccountToken: false
      securityContext: { runAsNonRoot: true, runAsUser: 10001, seccompProfile: { type: RuntimeDefault } }
      containers:
        - name: app
          image: $1
          securityContext: { allowPrivilegeEscalation: false, capabilities: { drop: [ALL] } }
YAML
}

# try <pod|deployment> <image> -> prints the API server's answer, returns its exit code
try() { "$1" "$2" | kubectl apply --dry-run=server -f - 2>&1; }

admitted() {
  local out
  out=$(try "$1" "$2") || fail "$1 with $2 was refused, expected admitted:
$out"
  pass "admitted: $1 $2 ($3)"
}

refused() {
  local out
  if out=$(try "$1" "$2"); then fail "$1 with $2 was ADMITTED, expected refused ($3)"; fi
  # Refused by Kyverno, not by something else (Pod Security, a typo in the manifest).
  grep -q "require-release-provenance" <<<"$out" \
    || fail "$1 with $2 was refused, but not by the provenance policy:
$out"
  pass "refused:  $1 $2 ($3)"
}

kubectl get imagevalidatingpolicy require-release-provenance >/dev/null 2>&1 \
  || fail "provenance policy not installed (POLICIES=0? see scripts/cluster.sh)"

admitted pod "$REPO@$RELEASE" "the documented release"
refused pod "$REPO@$UNATTESTED" "a real image with no attestation of its own"
refused pod "$REPO:0.2.0" "a tag: mutable, so never verified"
refused pod "$REPO@$MISSING" "a digest that doesn't exist"
refused deployment "$REPO@$UNATTESTED" "Pod templates are checked too (autogen)"
admitted pod "shortly:e2e" "a local build: not a registry image, outside the policy"

# The allowlist is Audit in kind: admitted, but reported. The app pods from the earlier
# steps run shortly:e2e, which isn't on it, so their PolicyReport must say "fail".
for _ in $(seq 1 36); do
  failed=$(kubectl -n "$NS" get policyreport -o json 2>/dev/null | python3 -c '
import json, sys
items = json.load(sys.stdin).get("items", [])
print(sum(1 for r in items for res in r.get("results", [])
          if res.get("policy") == "shortly-allowed-images" and res.get("result") == "fail"))')
  ((failed > 0)) && break
  sleep 5
done
((failed > 0)) || fail "no allowlist violations reported in $NS after 180 s (reports controller?)"
pass "allowlist (Audit): $failed violation(s) reported in $NS for the local build"
