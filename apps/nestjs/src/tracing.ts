// langperf — OpenTelemetry tracing (contract shared across all stacks).
//
// One fresh SERVER root span per API request ("HTTP <METHOD> <route>"), one
// CLIENT child span per SQL statement ("DB Q1 feed" etc.). No context
// extraction is performed, so incoming trace headers are never propagated —
// roots are always fresh. Spans use explicit parent contexts, so no global
// context manager is registered.
//
// Export: OTLP/HTTP to <OTEL_EXPORTER_OTLP_ENDPOINT>/v1/traces, batched every
// 500ms (prompt flush), 100% sampled.
import { Span, SpanKind, SpanStatusCode, context, trace } from '@opentelemetry/api';
import {
  BasicTracerProvider,
  BatchSpanProcessor,
  AlwaysOnSampler,
} from '@opentelemetry/sdk-trace-base';
import { resourceFromAttributes } from '@opentelemetry/resources';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';

const OTEL_EXPORTER_ENDPOINT = (
  process.env.OTEL_EXPORTER_OTLP_ENDPOINT ||
  'http://otel-gateway-collector.observability.svc.cluster.local:4318'
).replace(/\/+$/, '');

const tracerProvider = new BasicTracerProvider({
  resource: resourceFromAttributes({ 'service.name': 'langperf-nestjs' }),
  sampler: new AlwaysOnSampler(), // 100% sampling
  spanProcessors: [
    new BatchSpanProcessor(
      new OTLPTraceExporter({ url: `${OTEL_EXPORTER_ENDPOINT}/v1/traces` }),
      { scheduledDelayMillis: 500 },
    ),
  ],
});
export const tracer = tracerProvider.getTracer('langperf.nestjs');

// route label values are the *pattern*, exactly (SPEC.md). /metrics and
// /healthz (and unmatched paths) are never traced.
export const TRACED_ROUTES = new Set([
  '/feed',
  '/posts',
  '/posts/:id',
  '/posts/:id/like',
]);

// Fastify's routeOptions.url is authoritative for matched routes; fall back to
// normalizing the raw path in case a route pattern ever reaches us verbatim.
export function normalizeRoute(rawUrl: unknown): string | undefined {
  if (typeof rawUrl !== 'string') return undefined;
  if (TRACED_ROUTES.has(rawUrl)) return rawUrl;
  if (/^\/posts\/[^/]+$/.test(rawUrl)) return '/posts/:id';
  if (/^\/posts\/[^/]+\/like$/.test(rawUrl)) return '/posts/:id/like';
  return undefined;
}

// Root spans are attached to the Fastify request object via this WeakMap
// (one span per request object; Fastify creates one per request).
const requestSpans = new WeakMap<object, Span>();

export function setRequestSpan(request: object, span: Span | undefined): void {
  if (span) requestSpans.set(request, span);
  else requestSpans.delete(request);
}

export function requestSpan(request: object): Span | undefined {
  return requestSpans.get(request);
}

// SERVER root span for one request, measuring wall time around the whole
// request handling. Ended by the onResponse hook via endRequestSpan().
export function startRequestSpan(
  request: object,
  method: string,
  route: string,
): void {
  const span = tracer.startSpan(`HTTP ${method} ${route}`, {
    kind: SpanKind.SERVER,
    attributes: { 'http.method': method, 'http.route': route },
  });
  setRequestSpan(request, span);
}

export function endRequestSpan(
  request: object,
  method: string,
  route: string,
): void {
  const span = requestSpans.get(request);
  if (!span) return;
  setRequestSpan(request, undefined);
  // The route pattern is authoritative; re-check at span end (mirrors the
  // sibling stacks) before closing the root span.
  span.updateName(`HTTP ${method} ${route}`);
  span.setAttribute('http.route', route);
  span.end();
}

// CLIENT span around one DB round-trip (wall time start -> end), parented
// explicitly on the request's SERVER span. Returns the query's promise.
export function withDbSpan<T>(
  parentSpan: Span | undefined,
  name: string,
  run: () => Promise<T>,
): Promise<T> {
  const parentContext = parentSpan
    ? trace.setSpan(context.active(), parentSpan)
    : undefined;
  const span = tracer.startSpan(
    name,
    { kind: SpanKind.CLIENT, attributes: { 'db.system': 'postgresql' } },
    parentContext,
  );
  let result: Promise<T>;
  try {
    result = run();
  } catch (err) {
    span.recordException(err as Error);
    span.setStatus({ code: SpanStatusCode.ERROR });
    span.end();
    throw err;
  }
  return Promise.resolve(result).then(
    (value) => {
      span.end();
      return value;
    },
    (err) => {
      span.recordException(err as Error);
      span.setStatus({ code: SpanStatusCode.ERROR });
      span.end();
      throw err;
    },
  );
}
