#!/usr/bin/env bash
# Admission policy check (scripts/k8s-e2e.sh step 8): the live API server, through Kyverno,
# refuses shortly images without release provenance and reports images outside the allowlist.
# Every request is a server-side dry run: it goes through admission webhooks, creates nothing.
#
#   scripts/admission-check.sh            # against the kind cluster from the platform's scripts/cluster.sh
#
# Needs network from the cluster to ghcr.io and Sigstore (Rekor, TUF), like a real cluster.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/lib/platform.sh
source "$(dirname "$0")/lib/platform.sh"
NS="${NS:-$STAGING_NS}"
REPO=ghcr.io/easypeasyops/links/shortly
# The release the runbook documents (also checked by tests/test_deploy_gate.py).
RELEASE=$(sed -nE 's/^\| Current release \| v[0-9.]+, `([^`@ ]+@sha256:[0-9a-f]{64})`.*/\1/p' "$ROOT/docs/RUNBOOK.md")
[[ -n "$RELEASE" ]] || { echo "FAIL: no 'Current release' image in RUNBOOK" >&2; exit 1; }
# v0.7.2's linux/amd64 manifest: a real image, but the attestation names the index only.
UNATTESTED=sha256:d4a610fb8ad3fa48a8df0089935b5d465e54001cf82daad4a47abaf06a4e95f9
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

# refused <pod|deployment> <image> <why> [what the refusal must say: default, the provenance policy]
refused() {
  local out by="${4:-require-release-provenance}"
  if out=$(try "$1" "$2"); then fail "$1 with $2 was ADMITTED, expected refused ($3)"; fi
  # Refused by Kyverno, not by something else (Pod Security, a typo in the manifest).
  grep -qF "$by" <<<"$out" || fail "$1 with $2 was refused, but not with \"$by\":
$out"
  pass "refused:  $1 $2 ($3)"
}

kubectl get imagevalidatingpolicy require-release-provenance >/dev/null 2>&1 \
  || fail "provenance policy not installed (POLICIES=0? see the platform's scripts/cluster.sh)"

admitted pod "$RELEASE" "the documented release"
refused pod "$REPO@$UNATTESTED" "a real image with no attestation of its own"
refused pod "$REPO:0.2.0" "a tag: mutable, so never verified"
refused pod "$REPO@$MISSING" "a digest that doesn't exist"
refused deployment "$REPO@$UNATTESTED" "Pod templates are checked too (autogen)"
admitted pod "shortly:e2e" "a local build: outside the provenance policy; the allowlist's named exception"

# The allowlist (app-allowed-images): Audit in kind up to platform PT-111, Deny from then
# (shortly:e2e passes it through the named exception for the app's own local build). Read
# from the policy itself, so this holds on either side of a lock move; any other answer,
# or no policy, fails.
FOREIGN=busybox:1.36 # an image no app namespace may run
actions=$(kubectl get validatingpolicy app-allowed-images -o jsonpath='{.spec.validationActions}' 2>/dev/null) \
  || fail "allowlist policy app-allowed-images not installed (POLICIES=0? see the platform's scripts/cluster.sh)"
case "$actions" in
  *Deny*)
    refused pod "$FOREIGN" "outside the allowlist" "image not allowed in the namespaces of app"
    ;;
  *Audit*)
    admitted pod "$FOREIGN" "outside the allowlist, which is Audit here: reported, not refused"
    # Audit only reports: the policy must still be evaluating shortly's pods.
    for _ in $(seq 1 36); do
      results=$(kubectl -n "$NS" get policyreport -o json 2>/dev/null | python3 -c '
import json, sys
items = json.load(sys.stdin).get("items", [])
print(sum(1 for r in items for res in r.get("results", []) if res.get("policy") == "app-allowed-images"))')
      ((results > 0)) && break
      sleep 5
    done
    ((results > 0)) || fail "no allowlist results reported in $NS after 180 s (reports controller?)"
    pass "allowlist (Audit): $results result(s) reported in $NS"
    ;;
  *) fail "app-allowed-images has validationActions $actions: expected Deny or Audit" ;;
esac
