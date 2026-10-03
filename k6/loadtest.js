// langperf load test — identical mix for every stack.
// Mix: 55% GET /feed, 20% GET /posts/:id, 15% POST /posts, 10% POST /posts/:id/like
// Modes (env MODE): baseline = constant 200 rps (RATE) | ramp = 200->1800 rps stress ramp
import http from 'k6/http';
import { check } from 'k6';

const BASE = (__ENV.BASE_URL || 'http://localhost:8080').replace(/\/+$/, '');
const RATE = Number(__ENV.RATE || 200);
const DURATION = __ENV.DURATION || '300s';
const MODE = __ENV.MODE || 'baseline';

const CONTENTS = [
  'shipping my side project today',
  'coffee first, then code',
  'anyone else benchmarking stuff?',
  'hello world from k6',
  'just refactored my whole life',
  'deployed on a friday, wish me luck',
  'this one weird sql trick changed everything',
  'p99 is a lifestyle not a metric',
  'the answer was a missing index',
  'load test went brrr',
  'green threads, red eyes',
  'another day another rollout',
];

const scenario =
  MODE === 'ramp'
    ? {
        executor: 'ramping-arrival-rate',
        startRate: 200,
        timeUnit: '1s',
        stages: [
          { target: 400, duration: '30s' },
          { target: 600, duration: '30s' },
          { target: 800, duration: '30s' },
          { target: 1000, duration: '30s' },
          { target: 1200, duration: '30s' },
          { target: 1400, duration: '30s' },
          { target: 1600, duration: '30s' },
          { target: 1800, duration: '30s' },
        ],
        preAllocatedVUs: 400,
        maxVUs: 20000,
      }
    : {
        executor: 'constant-arrival-rate',
        rate: RATE,
        timeUnit: '1s',
        duration: DURATION,
        preAllocatedVUs: Math.min(6000, Math.max(500, Math.round(RATE / 3))),
        maxVUs: 20000,
      };

export const options = {
  scenarios: { mixed: scenario },
  summaryTrendStats: ['avg', 'p(50)', 'p(95)', 'p(99)', 'p(99.9)', 'max'],
  // thresholds are intentionally loose; they exist to force per-endpoint sub-metrics
  thresholds: {
    http_req_failed: ['rate<0.90'],
    'http_req_duration{name:GET /feed}': ['p(99.9)<120000'],
    'http_req_duration{name:GET /posts/:id}': ['p(99.9)<120000'],
    'http_req_duration{name:POST /posts}': ['p(99.9)<120000'],
    'http_req_duration{name:POST /posts/:id/like}': ['p(99.9)<120000'],
  },
};

function rnd(max) { return 1 + Math.floor(Math.random() * max); }
function pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }

// Overqueue guard: a request slower than TIMEOUT_BUDGET is an error, not a slow success.
// Fails fast, frees the VU, and keeps the server from poisoning the run with 10s campers.
const TIMEOUT_BUDGET = __ENV.TIMEOUT_BUDGET || '2s';

export default function () {
  const r = Math.random();
  if (r < 0.55) {
    const res = http.get(`${BASE}/feed?page=${rnd(50)}`, { tags: { name: 'GET /feed' }, timeout: TIMEOUT_BUDGET });
    check(res, { 'feed 200': (res) => res.status === 200 });
  } else if (r < 0.75) {
    const res = http.get(`${BASE}/posts/${rnd(500000)}`, { tags: { name: 'GET /posts/:id' }, timeout: TIMEOUT_BUDGET });
    check(res, { 'post 200': (res) => res.status === 200 });
  } else if (r < 0.90) {
    const payload = JSON.stringify({ user_id: rnd(50000), content: pick(CONTENTS) });
    const res = http.post(`${BASE}/posts`, payload, {
      headers: { 'Content-Type': 'application/json' },
      tags: { name: 'POST /posts' },
      timeout: TIMEOUT_BUDGET,
    });
    check(res, { 'post 201': (res) => res.status === 201 });
  } else {
    const payload = JSON.stringify({ user_id: rnd(50000) });
    const res = http.post(`${BASE}/posts/${rnd(500000)}/like`, payload, {
      headers: { 'Content-Type': 'application/json' },
      tags: { name: 'POST /posts/:id/like' },
      timeout: TIMEOUT_BUDGET,
    });
    check(res, { 'like 200/404': (res) => res.status === 200 || res.status === 404 });
  }
}

function round2(x) { return x == null ? null : Math.round(x * 100) / 100; }

export function handleSummary(data) {
  // k6 v2: every metric is {type, contains, values:{...}}
  const m = data.metrics;
  const V = (n) => (m[n] && m[n].values) || {};
  const dur = V('http_req_duration');
  const sub = (name) => V(`http_req_duration{name:${name}}`);
  const per = (name) => {
    const d = sub(name);
    return d['p(50)'] != null
      ? { p50: round2(d['p(50)']), p95: round2(d['p(95)']), p999: round2(d['p(99.9)']) }
      : null;
  };
  const out = {
    mode: MODE,
    offered_rate: RATE,
    rps: round2(V('http_reqs')['rate']),
    iterations: V('iterations')['count'],
    p50_ms: round2(dur['p(50)']),
    p95_ms: round2(dur['p(95)']),
    p99_ms: round2(dur['p(99)']),
    p999_ms: round2(dur['p(99.9)']),
    max_ms: round2(dur['max']),
    failed_rate: V('http_req_failed')['rate'],
    checks_rate: V('checks')['rate'],
    dropped_iterations: V('dropped_iterations')['count'] || 0,
    per_endpoint: {
      'GET /feed': per('GET /feed'),
      'GET /posts/:id': per('GET /posts/:id'),
      'POST /posts': per('POST /posts'),
      'POST /posts/:id/like': per('POST /posts/:id/like'),
    },
  };
  return { stdout: `\nK6SUMMARY ${JSON.stringify(out)}\n` };
}
