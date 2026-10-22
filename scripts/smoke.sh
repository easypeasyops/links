#!/usr/bin/env bash
# Black-box smoke test of the built image: real container, real Postgres, real HTTP.
# Same script locally and in CI, so "it passed in CI" means the same thing everywhere.
#
#   SHORTLY_IMAGE=shortly:dev scripts/smoke.sh
set -euo pipefail

export SHORTLY_IMAGE="${SHORTLY_IMAGE:-shortly:dev}"
BASE="http://127.0.0.1:8000"
compose() { docker compose --profile app "$@"; }

cleanup() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "--- smoke test failed; container logs ---"
    compose logs --no-color migrate app || true
  fi
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok   $*"; }

echo "Starting stack with ${SHORTLY_IMAGE}"
compose up -d --no-build --wait --wait-timeout 60

# 1. Hardening: the process is not root.
uid=$(compose exec -T app id -u)
[[ "$uid" != "0" ]] || fail "app runs as root"
pass "runs as non-root (uid $uid)"

# 2. Readiness reflects a working database (migrations ran, pool connects).
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/readyz")
[[ "$code" == "200" ]] || fail "/readyz returned $code"
pass "/readyz 200"

# 3. Create → redirect round trip.
body=$(curl -sf -X POST "$BASE/links" -H 'content-type: application/json' \
  -d '{"url": "https://example.com/smoke"}')
link_code=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["code"])' <<<"$body")
location=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "$BASE/$link_code")
[[ "$location" == "307 https://example.com/smoke" ]] || fail "redirect was: $location"
pass "POST /links → GET /$link_code → 307"

# 4. Unknown code.
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/doesnotexist")
[[ "$code" == "404" ]] || fail "unknown code returned $code"
pass "unknown code 404"

# 5. Observability: request IDs are returned, and Prometheus can scrape us.
rid=$(curl -s -D - -o /dev/null -H 'x-request-id: smoke-1' "$BASE/healthz" | tr -d '\r' \
  | awk -F': ' 'tolower($1)=="x-request-id"{print $2}')
[[ "$rid" == "smoke-1" ]] || fail "request id not echoed (got '$rid')"
# /metrics is on its own port; the app port must not serve it.
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/metrics")
[[ "$code" == "404" ]] || fail "/metrics on the app port returned $code, want 404"
metrics=$(curl -sf "http://127.0.0.1:9100/metrics")
grep -q 'http_requests_total{method="GET",route="/{code}",status="307"}' <<<"$metrics" \
  || fail "/metrics missing the redirect series"
pass "x-request-id echoed; /metrics (port 9100 only) has route-templated series"

# 6. Logs are structured: every line the app wrote is a JSON object.
if ! compose logs --no-color --no-log-prefix app | python3 -c '
import json, sys
lines = [l for l in sys.stdin if l.strip()]
bad = [l for l in lines if not l.lstrip().startswith("{")]
assert lines and not bad, bad[:3]
[json.loads(l) for l in lines]
'; then
  fail "app logs are not all JSON"
fi
pass "logs are JSON"

# 7. Graceful shutdown: SIGTERM must produce a clean exit well inside the grace period.
#    Exit 137 here would mean Docker had to SIGKILL it — signals aren't reaching uvicorn.
start=$(date +%s)
compose stop -t 25 app >/dev/null
elapsed=$(( $(date +%s) - start ))
exit_code=$(docker inspect "$(compose ps -a -q app)" --format '{{.State.ExitCode}}')
[[ "$exit_code" == "0" ]] || fail "exit code $exit_code after SIGTERM"
(( elapsed < 20 )) || fail "shutdown took ${elapsed}s"
pass "SIGTERM → clean exit in ${elapsed}s"

echo "smoke test passed"
