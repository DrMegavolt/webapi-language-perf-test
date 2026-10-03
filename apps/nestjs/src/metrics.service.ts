import { Injectable } from '@nestjs/common';
import * as fs from 'node:fs';
import { Counter, Gauge, Histogram, Registry } from 'prom-client';
import type { FastifyReply, FastifyRequest } from 'fastify';

// route label values are the *pattern*, exactly (SPEC.md). /metrics and
// /healthz are never recorded.
const RECORDED_ROUTES = new Set([
  '/feed',
  '/posts',
  '/posts/:id',
  '/posts/:id/like',
]);

function readRssBytes(): number {
  try {
    const status = fs.readFileSync('/proc/self/status', 'utf8');
    const m = /VmRSS:\s+(\d+)\s+kB/.exec(status);
    if (m) return Number(m[1]) * 1024;
  } catch {
    // No /proc (e.g. running on macOS) — fall back to process.memoryUsage().
  }
  return process.memoryUsage().rss;
}

interface RequestLabels {
  method: string;
  route: string;
  status: string;
}

@Injectable()
export class MetricsService {
  readonly contentType: string;

  private readonly register = new Registry();
  private readonly httpDuration: Histogram<'method' | 'route' | 'status'>;
  private readonly httpRequestsTotal: Counter<'method' | 'route' | 'status'>;
  private readonly appMemoryRssBytes: Gauge<string>;
  private readonly startTimes = new WeakMap<object, bigint>();

  constructor() {
    this.httpDuration = new Histogram({
      name: 'http_request_duration_seconds',
      help: 'Duration of HTTP requests in seconds.',
      labelNames: ['method', 'route', 'status'],
      buckets: [
        0.0005, 0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5,
        5, 10,
      ],
      registers: [this.register],
    });
    this.httpRequestsTotal = new Counter({
      name: 'http_requests_total',
      help: 'Total number of HTTP requests.',
      labelNames: ['method', 'route', 'status'],
      registers: [this.register],
    });
    this.appMemoryRssBytes = new Gauge({
      name: 'app_memory_rss_bytes',
      help: 'Resident memory of the process in bytes (VmRSS from /proc/self/status).',
      registers: [this.register],
    });
    this.contentType = this.register.contentType;
  }

  onRequest(request: FastifyRequest): void {
    this.startTimes.set(request, process.hrtime.bigint());
  }

  onResponse(request: FastifyRequest, reply: FastifyReply): void {
    const route = request.routeOptions?.url;
    if (!route || !RECORDED_ROUTES.has(route)) return; // only the 4 API endpoints
    const start = this.startTimes.get(request);
    if (start === undefined) return; // observe each request exactly once
    this.startTimes.delete(request);
    const elapsedSeconds = Number(process.hrtime.bigint() - start) / 1e9;
    const labels: RequestLabels = {
      method: request.method,
      route,
      status: String(reply.statusCode),
    };
    this.httpDuration.observe(labels, elapsedSeconds);
    this.httpRequestsTotal.inc(labels);
    this.appMemoryRssBytes.set(readRssBytes());
  }

  async render(): Promise<string> {
    return this.register.metrics();
  }
}
