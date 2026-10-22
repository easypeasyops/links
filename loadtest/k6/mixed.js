// Realistic traffic mix at a FIXED arrival rate (open model): 90% redirects, 10% creates.
//
// Open model on purpose: real users don't wait for the previous request to finish before
// sending the next, so a closed model (N looping VUs) hides overload by slowing itself
// down. constant-arrival-rate keeps sending at RATE; when the service can't keep up,
// latency and errors show it.
//
//   k6 run -e BASE_URL=http://localhost:8000 -e RATE=400 -e DURATION=30s loadtest/k6/mixed.js
import http from "k6/http";
import { check } from "k6";

const BASE = __ENV.BASE_URL || "http://localhost:8000";
const RATE = Number(__ENV.RATE || 100); // total requests per second
const DURATION = __ENV.DURATION || "30s";
const SEED = Number(__ENV.SEED_LINKS || 500);
const CREATE_SHARE = Number(__ENV.CREATE_SHARE || 0.1);

const createRate = Math.max(1, Math.round(RATE * CREATE_SHARE));
const redirectRate = Math.max(1, RATE - createRate);
// Enough VUs to sustain the rate even if each request takes ~1 s; k6 warns if it runs out.
const vus = (rate) => ({ preAllocatedVUs: Math.ceil(rate / 4) + 5, maxVUs: rate + 50 });

export const options = {
  discardResponseBodies: true,
  scenarios: {
    redirects: {
      executor: "constant-arrival-rate",
      exec: "redirect",
      rate: redirectRate,
      timeUnit: "1s",
      duration: DURATION,
      ...vus(redirectRate),
    },
    creates: {
      executor: "constant-arrival-rate",
      exec: "create",
      rate: createRate,
      timeUnit: "1s",
      duration: DURATION,
      ...vus(createRate),
    },
  },
  // The candidate SLO. A step "passes" only if all of these hold.
  thresholds: {
    http_req_failed: ["rate<0.01"],
    "http_req_duration{scenario:redirects}": ["p(99)<50"],
    "http_req_duration{scenario:creates}": ["p(99)<100"],
    dropped_iterations: ["count<1"], // k6 itself couldn't keep up = invalid result
    // Not gates: these exist so the summary reports per-scenario throughput, which
    // excludes the seeding requests made in setup().
    "http_reqs{scenario:redirects}": ["count>=0"],
    "http_reqs{scenario:creates}": ["count>=0"],
    // Successful requests only: past capacity a pod sheds (fast 503s), so "all requests"
    // latency flatters it. Goodput and the latency of what succeeded are the honest numbers.
    "http_reqs{scenario:redirects,expected_response:true}": ["count>=0"],
    "http_reqs{scenario:creates,expected_response:true}": ["count>=0"],
    "http_req_duration{scenario:redirects,expected_response:true}": ["p(99)>=0"],
  },
  summaryTrendStats: ["avg", "p(50)", "p(95)", "p(99)", "max"],
};

// Many clients, not one: every create comes from a random address in 198.18.0.0/15 (the
// range reserved for benchmarks, RFC 2544), sent as X-Forwarded-For the way an ingress
// would. The app trusts it only where configured (compose; kind prod via the pod network),
// so the per-client rate limit is exercised as in production instead of refusing the
// load test, a single real client, after its burst.
const fakeClient = () =>
  `198.${18 + Math.floor(Math.random() * 2)}.${Math.floor(Math.random() * 256)}.${1 + Math.floor(Math.random() * 254)}`;
const createHeaders = () => ({ "content-type": "application/json", "x-forwarded-for": fakeClient() });

// 307 and 201 are successes; everything else counts toward http_req_failed.
http.setResponseCallback(http.expectedStatuses(201, 307));

export function setup() {
  const codes = [];
  for (let i = 0; i < SEED; i++) {
    const params = { headers: createHeaders(), responseType: "text" };
    const res = http.post(`${BASE}/links`, JSON.stringify({ url: `https://example.com/seed/${i}` }), params);
    if (res.status !== 201) throw new Error(`seeding failed: HTTP ${res.status}`);
    codes.push(res.json("code"));
  }
  return { codes };
}

export function redirect(data) {
  const code = data.codes[Math.floor(Math.random() * data.codes.length)];
  const res = http.get(`${BASE}/${code}`, { redirects: 0, tags: { name: "GET /{code}" } });
  check(res, { "307": (r) => r.status === 307 });
}

export function create() {
  const res = http.post(
    `${BASE}/links`,
    JSON.stringify({ url: `https://example.com/load/${__VU}/${__ITER}` }),
    { headers: createHeaders(), tags: { name: "POST /links" } },
  );
  check(res, { "201": (r) => r.status === 201 });
}
