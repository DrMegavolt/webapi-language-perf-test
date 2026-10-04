// langperf — NestJS 11 + FastifyAdapter implementation.
// Canonical SQL lives in sql/queries.sql at the repo root; the statements in
// app.controller.ts must stay byte-identical to Q1, Q2, Q3 and Q4a-c
// (parameter binding only).
import 'reflect-metadata';
import { NestFactory } from '@nestjs/core';
import {
  FastifyAdapter,
  NestFastifyApplication,
} from '@nestjs/platform-fastify';
import { AppModule } from './app.module';
import { MetricsService } from './metrics.service';
import { endRequestSpan, normalizeRoute, startRequestSpan } from './tracing';

async function bootstrap(): Promise<void> {
  const adapter = new FastifyAdapter();
  const app = await NestFactory.create<NestFastifyApplication>(
    AppModule,
    adapter,
    { logger: false },
  );

  // Wall-clock timing around the full request handling (includes body parse
  // + DB time), observed exactly once per request via Fastify hooks.
  //
  // Tracing uses the same hooks: a fresh SERVER root span per API request is
  // opened in onRequest (Fastify routes before onRequest, so routeOptions.url
  // is the authoritative pattern) and closed in onResponse after the response
  // has been sent. /metrics, /healthz and unmatched paths get no spans.
  const fastify = app.getHttpAdapter().getInstance();
  const metrics = app.get(MetricsService);
  fastify.addHook('onRequest', (request, reply, done) => {
    metrics.onRequest(request);
    const route = normalizeRoute(request.routeOptions?.url);
    if (route) startRequestSpan(request, request.method, route);
    done();
  });
  fastify.addHook('onResponse', (request, reply, done) => {
    metrics.onResponse(request, reply);
    const route = normalizeRoute(request.routeOptions?.url);
    if (route) endRequestSpan(request, request.method, route);
    done();
  });

  app.enableShutdownHooks();

  const port = Number(process.env.PORT) || 8080;
  await app.listen(port, '0.0.0.0');
}

void bootstrap();
