#!/usr/bin/env bash
# Verify that an image was built by its own repo's release workflow from a release tag, before
# anything deploys it. Keyed by identity: ghcr.io/<owner>/<repo> must come from <owner>/<repo>,
# and the owner must be ours (VERIFY_OWNER, default statsguy). Same rule as the admission
# policy (deploy/policies/require-release-provenance.yaml). scripts/deploy.sh calls this for every registry image; it also runs
# on its own:
#
#   scripts/verify-image.sh ghcr.io/statsguy/shortly@sha256:...
#
# What is checked (via the signed SLSA provenance from actions/attest-build-provenance):
#   - the signature chains to Sigstore's public-good root and the GitHub Actions OIDC issuer
#   - the signer is <owner>/<repo>/.github/workflows/release.yml (not CI, not a fork, not
#     another repo's release)
#   - the build ran from a refs/tags/v* ref (a release, not a branch)
#   - the attestation's subject is exactly this digest
# Exit 0 only if all hold. Fails closed: no gh, no network, or no attestation = no deploy.
set -euo pipefail

REF="${1:?usage: $0 <registry/image@sha256:digest>}"
OWNER="${VERIFY_OWNER:-statsguy}"

fail() { echo "REFUSED: $*" >&2; exit 1; }

[[ "$REF" == *@sha256:* ]] || fail "$REF is not a digest reference; tags are mutable, so only a digest can be verified"
# The image's name says which repo must have built it.
[[ "${REF%@*}" =~ ^ghcr\.io/([^/]+)/([^/]+)$ ]] \
  || fail "$REF is not a ghcr.io/<owner>/<repo> image: its source repository can't be known"
[[ "${BASH_REMATCH[1]}" == "$OWNER" ]] || fail "$REF is not one of $OWNER's images"
REPO="${VERIFY_REPO:-${BASH_REMATCH[1]}/${BASH_REMATCH[2]}}"
SIGNER="${VERIFY_SIGNER:-$REPO/.github/workflows/release.yml}"
command -v gh >/dev/null || fail "gh (GitHub CLI) is required to verify provenance: https://cli.github.com"

# --bundle-from-oci reads the attestation from the registry (release.yml pushes it there),
# so this works without GitHub API access too.
if ! out=$(gh attestation verify "oci://$REF" --repo "$REPO" --signer-workflow "$SIGNER" \
  --predicate-type https://slsa.dev/provenance/v1 --bundle-from-oci --format json 2>&1); then
  fail "no valid provenance from $SIGNER for $REF
$out"
fi

python3 - "$out" "${REF#*@sha256:}" <<'PY' || exit 1
import json, sys

results = json.loads(sys.argv[1])
digest = sys.argv[2]
for r in results:
    v = r["verificationResult"]
    cert = v["signature"]["certificate"]
    ref = cert.get("sourceRepositoryRef", "")
    subjects = {s["digest"].get("sha256") for s in v["statement"]["subject"]}
    if ref.startswith("refs/tags/v") and digest in subjects:
        print(f"verified: built by {cert['buildSignerURI']}\n"
              f"          from {cert['sourceRepositoryURI']} {ref} @ {cert['sourceRepositoryDigest'][:12]}")
        sys.exit(0)
refs = sorted({r["verificationResult"]["signature"]["certificate"].get("sourceRepositoryRef") for r in results})
print(f"REFUSED: signed by the release workflow, but not from a release tag (refs: {refs})", file=sys.stderr)
sys.exit(1)
PY
