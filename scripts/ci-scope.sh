#!/usr/bin/env bash
# Does this CI run need the full checks? Writes `full=true|false` to $GITHUB_OUTPUT.
#
# A pull request that changes only Markdown, other than docs/RUNBOOK.md, skips the work in
# every CI job: no code, chart, image or test input changed. The jobs still run and report
# success, because they are required checks: a workflow-level `paths` filter would leave
# them "expected" forever and block the merge.
#
# docs/RUNBOOK.md is not docs-only: the tests, the admission checks and the e2e read the
# current release digest from it. Pushes to main always run everything.
#
# Needs the PR merge commit and its first parent (actions/checkout fetch-depth: 2 or more).
set -euo pipefail

out="${GITHUB_OUTPUT:-/dev/stdout}"
if [[ "${GITHUB_EVENT_NAME:-}" != pull_request ]]; then
  echo "full=true" >>"$out"
  exit 0
fi
changed=$(git diff --name-only HEAD^1 HEAD)
echo "$changed"
if grep -qvE '\.md$' <<<"$changed" || grep -qx docs/RUNBOOK.md <<<"$changed"; then
  echo "full=true" >>"$out"
else
  echo "full=false" >>"$out"
  echo "::notice::Docs-only change: checks skipped (scripts/ci-scope.sh)"
fi
