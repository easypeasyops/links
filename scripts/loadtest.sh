#!/usr/bin/env bash
# Step load test: run the k6 mix at increasing fixed rates and report, per step, achieved
# throughput, latency percentiles, errors and the app's CPU. The highest step that meets
# every threshold is the capacity — at this CPU allocation.
#
#   docker compose --profile app up -d --build --wait      # the stack under test
#   scripts/loadtest.sh                                     # defaults: 1 CPU, steps 100..1200
#   APP_CPUS=2 RATES="200 400 800 1600" scripts/loadtest.sh
#
# Results are written to loadtest/results/<timestamp>.csv.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RATES="${RATES:-100 200 300 400 600 800 1000 1200}"
DURATION="${DURATION:-30s}"
APP_CPUS="${APP_CPUS:-1}"        # pin the app so results are "per N cores", not "per laptop"
K6_IMAGE="${K6_IMAGE:-grafana/k6:2.3.0}"
NETWORK="${NETWORK:-$(basename "$ROOT")_default}"
TARGET="${TARGET:-http://app:8000}"

app=$(docker compose --profile app ps -q app)
[[ -n "$app" ]] || { echo "app container not running: docker compose --profile app up -d --wait" >&2; exit 1; }
docker update --cpus "$APP_CPUS" "$app" >/dev/null

# Total CPU time the app container has used, in microseconds (cgroup v2, else v1).
cpu_usec() {
  docker exec "$app" sh -c '
    if [ -r /sys/fs/cgroup/cpu.stat ]; then awk "/^usage_usec/{print \$2}" /sys/fs/cgroup/cpu.stat
    elif [ -r /sys/fs/cgroup/cpuacct/cpuacct.usage ]; then echo $(( $(cat /sys/fs/cgroup/cpuacct/cpuacct.usage) / 1000 ))
    else cat /sys/fs/cgroup/cpuacct.usage | awk "{print int(\$1/1000)}"; fi'
}

mkdir -p "$ROOT/loadtest/results"
out="$ROOT/loadtest/results/$(date -u +%Y%m%dT%H%M%SZ)-cpus${APP_CPUS}.csv"
echo "target_rps,achieved_rps,redirect_p50_ms,redirect_p95_ms,redirect_p99_ms,create_p99_ms,error_rate,dropped,cores_used,cpu_ms_per_req,pass,goodput_rps,ok_redirect_p50_ms,ok_redirect_p99_ms" >"$out"

# goodput = successful requests/s; ok-p50/ok-p99 = redirect latency of successful requests only
printf '%-7s %-9s %-8s %-7s %-7s %-8s %-8s %-9s %-7s %-6s %-9s %s\n' target achieved goodput p50 p95 p99 ok-p99 create99 errors cores cpu-ms/req result
for rate in $RATES; do
  work=$(mktemp -d) && chmod 777 "$work"   # k6 runs as a non-root user in its container
  # Seed once per step in setup(); measure CPU only over the steady-state window that follows.
  docker run --rm -i --network "$NETWORK" -v "$ROOT/loadtest/k6:/scripts:ro" -v "$work:/out" \
    -e BASE_URL="$TARGET" -e RATE="$rate" -e DURATION="$DURATION" \
    "$K6_IMAGE" run --quiet --no-color --summary-export /out/summary.json /scripts/mixed.js \
    >/dev/null 2>&1 &   # a threshold failure exits non-zero; that's a result, not an error
  k6pid=$!
  # setup() seeds links first; start the CPU clock once seeding is done (rate jumps to RATE).
  sleep "${SEED_WAIT:-6}"
  cpu0=$(cpu_usec); t0=$(date +%s.%N)
  wait "$k6pid" || true
  cpu1=$(cpu_usec); t1=$(date +%s.%N)
  python3 - "$work/summary.json" "$rate" "$out" "$cpu0" "$cpu1" "$t0" "$t1" "${DURATION%s}" <<'PY'
import json, sys
path, rate, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
cpu0, cpu1, t0, t1, dur = map(float, sys.argv[4:9])
m = json.load(open(path))["metrics"]
red = m["http_req_duration{scenario:redirects}"]
cre = m["http_req_duration{scenario:creates}"]
count = m["http_reqs{scenario:redirects}"]["count"] + m["http_reqs{scenario:creates}"]["count"]
achieved = count / dur
okm = lambda k: m.get(k, {"count": 0, "p(50)": 0, "p(99)": 0})  # absent if nothing succeeded
good = (okm("http_reqs{scenario:redirects,expected_response:true}")["count"]
        + okm("http_reqs{scenario:creates,expected_response:true}")["count"]) / dur
ok_red = okm("http_req_duration{scenario:redirects,expected_response:true}")
err = m["http_req_failed"]["value"]
dropped = m.get("dropped_iterations", {}).get("count", 0)
window = t1 - t0
cores = (cpu1 - cpu0) / 1e6 / window                    # average cores busy in the window
ms_per_req = cores * 1000 / achieved if achieved else 0  # CPU cost of one request
ok = err < 0.01 and red["p(99)"] < 50 and cre["p(99)"] < 100 and dropped < 1
row = [rate, round(achieved), round(red["p(50)"], 1), round(red["p(95)"], 1), round(red["p(99)"], 1),
       round(cre["p(99)"], 1), round(err, 4), dropped, round(cores, 2), round(ms_per_req, 2),
       "PASS" if ok else "FAIL", round(good), round(ok_red["p(50)"], 1), round(ok_red["p(99)"], 1)]
open(out, "a").write(",".join(map(str, row)) + "\n")
print(f"{rate:<7} {round(achieved):<9} {round(good):<8} {row[2]:<7} {row[3]:<7} {row[4]:<8} {row[13]:<8} {row[5]:<9} {err:<7.2%} {row[8]:<6} {row[9]:<9} {row[10]}")
PY
  rm -rf "$work"
done
echo "results: $out"
