#!/usr/bin/env bash
# Promote a release to an environment, the GitOps way: write its repository and digest into
# deploy/envs/<env>.yaml. Merging that change to main is the deploy (Argo CD syncs it: the
# platform's ApplicationSet reads this repo's main); reverting it is the rollback.
#
#   scripts/promote.sh staging ghcr.io/easypeasyops/links/shortly@sha256:...   # or just sha256:...
#   scripts/promote.sh prod sha256:... --pr     # also branch, commit, push and open the PR
#
# Refuses, before touching the file:
#   - a digest without release provenance (the platform's scripts/verify-image.sh, as deploy.sh)
#   - for prod, a digest that staging hasn't run: it must be staging's digest on origin/main
#     now or earlier (tests/test_gitops.py checks the same in CI, so a hand edit can't skip it)
set -euo pipefail

ENV="${1:?usage: $0 <staging|prod> <digest|repo@digest> [--pr]}"
REF="${2:?usage: $0 <staging|prod> <digest|repo@digest> [--pr]}"
PR="${3:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO=ghcr.io/easypeasyops/links/shortly # releases from links (platform plan 3.5)
OLD_REPO=ghcr.io/statsguy/shortly       # up to v0.7.1: a rollback may name it
FILE="deploy/envs/$ENV.yaml"

case "$ENV" in staging | prod) ;; *) echo "unknown env: $ENV" >&2; exit 2 ;; esac
DIGEST="${REF#*@}"
[[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "not a digest: $REF" >&2; exit 2; }
[[ "$REF" != *@* ]] || REPO="${REF%@*}"
[[ "$REPO" == ghcr.io/easypeasyops/links/shortly || "$REPO" == "$OLD_REPO" ]] \
  || { echo "REFUSED: not a shortly image: $REF" >&2; exit 1; }

cd "$ROOT"
current=$(sed -nE 's/^  digest: "(sha256:[0-9a-f]{64})".*/\1/p' "$FILE")
[[ "$current" != "$DIGEST" ]] || { echo "$ENV already runs $DIGEST"; exit 0; }

# The platform checkout beside this one, as scripts/lib/platform.sh finds it. VERIFY: tests only.
verified=$("${VERIFY:-${PLATFORM_DIR:-$ROOT/../../../platform}/scripts/verify-image.sh}" "$REPO@$DIGEST")
echo "$verified"
version=$(sed -nE 's|.*refs/tags/([a-z0-9-]+/)?(v[0-9][^ ]*).*|\2|p' <<<"$verified" | head -n 1)
[[ -n "$version" ]] || { echo "could not read the release tag from verify-image.sh" >&2; exit 1; }

if [[ "$ENV" == prod ]]; then
  git fetch --quiet origin main
  # Into a variable, not `git log | grep -q`: with pipefail, grep -q quitting at the first
  # match kills git log with SIGPIPE, and the pipeline fails exactly when staging DID run
  # the digest (seen promoting v0.7.1 once the history outgrew the pipe buffer).
  staging_history=$(git log -p --follow origin/main -- deploy/envs/staging.yaml) # --follow: across the move to apps/shortly/
  if ! grep -q "^[+ ]  digest: \"$DIGEST\"" <<<"$staging_history"; then
    echo "REFUSED: staging has not run $DIGEST ($version) on main. Promote it to staging first." >&2
    exit 1
  fi
fi

sed -i.bak -E -e "s|^  repository: ghcr\.io/[^ ]+|  repository: $REPO|" \
  -e "s|^  digest: \"sha256:[0-9a-f]{64}\".*|  digest: \"$DIGEST\" # $version|" "$FILE" && rm -f "$FILE.bak"
grep -q "^  digest: \"$DIGEST\"" "$FILE" || { echo "no image.digest line updated in $FILE" >&2; exit 1; }
grep -q "^  repository: $REPO\b" "$FILE" || { echo "no image.repository line updated in $FILE" >&2; exit 1; }
echo "$FILE: $version ($REPO@$DIGEST)"

if [[ "$PR" == --pr ]]; then
  branch="promote-$ENV-$version"
  git switch --quiet -c "$branch" origin/main 2>/dev/null || git switch --quiet -c "$branch"
  git add "$FILE"
  git commit --quiet -m "deploy: $version to $ENV"
  git push --quiet -u origin "$branch"
  gh pr create --base main --head "$branch" --title "deploy: $version to $ENV" \
    --body "Promotes $version (\`$DIGEST\`) to $ENV. Provenance verified by scripts/promote.sh; CI checks it again. Argo CD deploys it within a minute of the merge."
else
  echo "next: commit $FILE in a PR (or rerun with --pr). Merging it deploys to $ENV."
fi
