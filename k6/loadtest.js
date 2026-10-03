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
        maxVUs: 2000,
      }
    : {
        executor: 'constant-arrival-rate',
        rate: RATE,
        timeUnit: '1s',
        duration: DURATION,
        preAllocatedVUs: 200,
        maxVUs: 1000,
      };

export const options = {
  scenarios: { mixed: scenario },
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

export default function () {
  const r = Math.random();
  if (r < 0.55) {
    const res = http.get(`${BASE}/feed?page=${rnd(50)}`, { tags: { name: 'GET /feed' } });
    check(res, { 'feed 200': (res) => res.status === 200 });
  } else if (r < 0.75) {
    const res = http.get(`${BASE}/posts/${rnd(500000)}`, { tags: { name: 'GET /posts/:id' } });
    check(res, { 'post 200': (res) => res.status === 200 });
  } else if (r < 0.90) {
    const payload = JSON.stringify({ user_id: rnd(50000), content: pick(CONTENTS) });
    const res = http.post(`${BASE}/posts`, payload, {
      headers: { 'Content-Type': 'application/json' },
      tags: { name: 'POST /posts' },
    });
    check(res, { 'post 201': (res) => res.status === 201 });
  } else {
    const payload = JSON.stringify({ user_id: rnd(50000) });
    const res = http.post(`${BASE}/posts/${rnd(500000)}/like`, payload, {
      headers: { 'Content-Type': 'application/json' },
      tags: { name: 'POST /posts/:id/like' },
    });
    check(res, { 'like 200/404': (res) => res.status === 200 || res.status === 404 });
  }
}

function round2(x) { return x == null ? null : Math.round(x * 100) / 100; }

export function handleSummary(data) {
  const m = data.metrics;
  const dur = m.http_req_duration || {};
  const per = (name) => {
    const d = m[`http_req_duration{name:${name}}`];
    return d
      ? { p50: round2(d['p(50)']), p95: round2(d['p(95)']), p999: round2(d['p(99.9)']) }
      : null;
  };
  const out = {
    mode: MODE,
    rps: round2(m.http_reqs ? m.http_reqs.rate : 0),
    iterations: m.iterations ? m.iterations.count : 0,
    p50_ms: round2(dur['p(50)']),
    p95_ms: round2(dur['p(95)']),
    p99_ms: round2(dur['p(99)']),
    p999_ms: round2(dur['p(99.9)']),
    max_ms: round2(dur.max || 0),
    failed_rate: m.http_req_failed ? round2((m.http_req_failed.rate || 0) * 10000) / 10000 : 0,
    checks_rate: m.checks ? round2((m.checks.rate || 0) * 10000) / 10000 : 0,
    dropped_iterations: m.dropped_iterations ? m.dropped_iterations.count : 0,
    per_endpoint: {
      'GET /feed': per('GET /feed'),
      'GET /posts/:id': per('GET /posts/:id'),
      'POST /posts': per('POST /posts'),
      'POST /posts/:id/like': per('POST /posts/:id/like'),
    },
  };
  return { stdout: `\nK6SUMMARY ${JSON.stringify(out)}\n` };
}
