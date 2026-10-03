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

async function bootstrap(): Promise<void> {
  const adapter = new FastifyAdapter();
  const app = await NestFactory.create<NestFastifyApplication>(
    AppModule,
    adapter,
    { logger: false },
  );

  // Wall-clock timing around the full request handling (includes body parse
  // + DB time), observed exactly once per request via Fastify hooks.
  const fastify = app.getHttpAdapter().getInstance();
  const metrics = app.get(MetricsService);
  fastify.addHook('onRequest', (request, reply, done) => {
    metrics.onRequest(request);
    done();
  });
  fastify.addHook('onResponse', (request, reply, done) => {
    metrics.onResponse(request, reply);
    done();
  });

  app.enableShutdownHooks();

  const port = Number(process.env.PORT) || 8080;
  await app.listen(port, '0.0.0.0');
}

void bootstrap();
