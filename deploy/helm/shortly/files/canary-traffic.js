// Synthetic traffic for canary analysis (templates/analysistemplate.yaml): a steady mix of
// redirects (90%) and creates (10%) through the Gateway, as users would arrive, so the
// canary's share has requests to be judged on. It never fails on its own: errors are what
// the analysis measures, not this script's business. Seeding retries until it has codes.
//
//   k6 run -e BASE_URL=... -e HOST=sho.rt.localhost -e RATE=20 -e DURATION=180s canary-traffic.js
import http from "k6/http";
import { sleep } from "k6";

const BASE = __ENV.BASE_URL;
const RATE = Number(__ENV.RATE || 20);
const DURATION = __ENV.DURATION || "180s";
const HOST = { Host: __ENV.HOST };
const createRate = Math.max(1, Math.round(RATE / 10));

export const options = {
  discardResponseBodies: true,
  scenarios: {
    redirects: {
      executor: "constant-arrival-rate", exec: "redirect", rate: RATE - createRate,
      timeUnit: "1s", duration: DURATION, preAllocatedVUs: 10, maxVUs: 50,
    },
    creates: {
      executor: "constant-arrival-rate", exec: "create", rate: createRate,
      timeUnit: "1s", duration: DURATION, preAllocatedVUs: 2, maxVUs: 10,
    },
  },
};

// A random client per create (RFC 2544 benchmark range), so the per-client rate limit
// sees many clients, as in production. The app trusts X-Forwarded-For from the pod network.
const client = () => `198.18.${Math.floor(Math.random() * 256)}.${1 + Math.floor(Math.random() * 254)}`;
const createParams = () => ({
  headers: { ...HOST, "content-type": "application/json", "x-forwarded-for": client() },
  responseType: "text",
});

export function setup() {
  const codes = [];
  for (let i = 0; codes.length < 20 && i < 200; i++) {
    const res = http.post(`${BASE}/links`, JSON.stringify({ url: `https://example.com/canary/${i}` }), createParams());
    if (res.status === 201) codes.push(res.json("code"));
    else sleep(0.2);
  }
  return { codes };
}

export function redirect(data) {
  if (data.codes.length === 0) return;
  const code = data.codes[Math.floor(Math.random() * data.codes.length)];
  http.get(`${BASE}/${code}`, { redirects: 0, headers: HOST, tags: { name: "GET /{code}" } });
}

export function create() {
  http.post(`${BASE}/links`, JSON.stringify({ url: `https://example.com/canary/${__VU}/${__ITER}` }), createParams());
}
