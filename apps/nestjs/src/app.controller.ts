import { Body, Controller, Get, HttpCode, HttpException, Param, Post, Query, Req, Res } from '@nestjs/common';
import type { FastifyReply, FastifyRequest } from 'fastify';
import { DbService } from './db.service';
import { MetricsService } from './metrics.service';
import { requestSpan, withDbSpan } from './tracing';

// ---------------------------------------------------------------------------
// Canonical SQL — byte-identical to sql/queries.sql (Q1, Q2, Q3, Q4a-c).
// ---------------------------------------------------------------------------
const Q1 = `WITH feed AS (
  SELECT p.id, p.user_id, p.content, p.created_at
  FROM posts p
  ORDER BY p.created_at DESC, p.id DESC
  LIMIT 20 OFFSET ($1 - 1) * 20
)
SELECT f.id, f.user_id, u.username, f.content, f.created_at,
       COUNT(l.id)::bigint AS like_count
FROM feed f
JOIN users u ON u.id = f.user_id
LEFT JOIN likes l ON l.post_id = f.id
GROUP BY f.id, f.user_id, u.username, f.content, f.created_at
ORDER BY f.created_at DESC, f.id DESC`;

const Q2 = `SELECT p.id, p.user_id, u.username, p.content, p.created_at,
       COUNT(l.id)::bigint AS like_count
FROM posts p
JOIN users u ON u.id = p.user_id
LEFT JOIN likes l ON l.post_id = p.id
WHERE p.id = $1
GROUP BY p.id, p.user_id, u.username, p.content, p.created_at`;

const Q3 = `INSERT INTO posts (user_id, content, created_at)
VALUES ($1, $2, now())
RETURNING id, user_id, content, created_at`;

const Q4A = 'SELECT 1 FROM posts WHERE id = $1';

const Q4B = `INSERT INTO likes (post_id, user_id, created_at)
VALUES ($1, $2, now())
ON CONFLICT (post_id, user_id) DO NOTHING`;

const Q4C = 'SELECT COUNT(*)::bigint AS like_count FROM likes WHERE post_id = $1';

// ---------------------------------------------------------------------------

function parsePostId(raw: unknown): number | undefined {
  if (typeof raw === 'string' && /^\d+$/.test(raw)) {
    const n = Number(raw);
    if (Number.isSafeInteger(n) && n > 0) return n;
  }
  return undefined;
}

function badRequest(error: string): HttpException {
  return new HttpException({ error }, 400);
}

function postNotFound(): HttpException {
  return new HttpException({ error: 'post not found' }, 404);
}

function mapPostRow(row: any): Record<string, unknown> {
  return {
    id: row.id,
    user_id: row.user_id,
    username: row.username,
    content: row.content,
    like_count: row.like_count,
    created_at: row.created_at,
  };
}

@Controller()
export class AppController {
  constructor(
    private readonly db: DbService,
    private readonly metrics: MetricsService,
  ) {}

  // Q1 (GET /feed?page=N): home feed, newest 20 posts with author + like count
  @Get('feed')
  async feed(
    @Query('page') page: unknown,
    @Req() request: FastifyRequest,
  ): Promise<Record<string, unknown>> {
    let p = Number(page === undefined || page === null || page === '' ? 1 : page);
    if (!Number.isFinite(p) || p < 1) p = 1;
    p = Math.floor(p);
    const { rows } = await withDbSpan(requestSpan(request), 'DB Q1 feed', () =>
      this.db.query(Q1, [p]),
    );
    return { page: p, posts: rows.map(mapPostRow) };
  }

  // Q2 (GET /posts/:id): single post with author + like count
  @Get('posts/:id')
  async getPost(
    @Param('id') id: string,
    @Req() request: FastifyRequest,
  ): Promise<Record<string, unknown>> {
    const postId = parsePostId(id);
    if (postId === undefined) throw badRequest('invalid post id');
    const { rows } = await withDbSpan(
      requestSpan(request),
      'DB Q2 single post',
      () => this.db.query(Q2, [postId]),
    );
    if (rows.length === 0) throw postNotFound();
    return mapPostRow(rows[0]);
  }

  // Q3 (POST /posts): create post — exactly one statement, no pre-check.
  @Post('posts')
  async createPost(
    @Body() body: any,
    @Req() request: FastifyRequest,
  ): Promise<Record<string, unknown>> {
    const payload = body ?? {};
    try {
      const { rows } = await withDbSpan(
        requestSpan(request),
        'DB Q3 create post',
        () => this.db.query(Q3, [payload.user_id, payload.content]),
      );
      const row = rows[0];
      return {
        id: row.id,
        user_id: row.user_id,
        content: row.content,
        created_at: row.created_at,
        like_count: 0, // computed in the app, never queried
      };
    } catch (err: any) {
      // A missing/unknown user_id surfaces as FK (23503) or NOT NULL (23502).
      if (err?.code === '23503' || (err?.code === '23502' && err?.column === 'user_id')) {
        throw badRequest('invalid user_id');
      }
      throw err;
    }
  }

  // Q4 (POST /posts/:id/like): exactly three statements in this order.
  @Post('posts/:id/like')
  @HttpCode(200)
  async like(
    @Param('id') id: string,
    @Body() body: any,
    @Req() request: FastifyRequest,
  ): Promise<Record<string, unknown>> {
    const postId = parsePostId(id);
    if (postId === undefined) throw badRequest('invalid post id');
    const span = requestSpan(request);

    // 4a: missing post -> 404 {"error":"post not found"}
    const post = await withDbSpan(span, 'DB Q4a post exists', () =>
      this.db.query(Q4A, [postId]),
    );
    if (post.rows.length === 0) throw postNotFound();

    // 4b: idempotent insert
    await withDbSpan(span, 'DB Q4b insert like', () =>
      this.db.query(Q4B, [postId, (body ?? {}).user_id]),
    );

    // 4c: fresh count for the response
    const count = await withDbSpan(span, 'DB Q4c like count', () =>
      this.db.query(Q4C, [postId]),
    );

    return { post_id: postId, like_count: count.rows[0].like_count };
  }

  @Get('healthz')
  async health(): Promise<{ status: string }> {
    await this.db.query('SELECT 1');
    return { status: 'ok' };
  }

  @Get('metrics')
  async metricsHandler(@Res() reply: FastifyReply): Promise<void> {
    reply.type(this.metrics.contentType).send(await this.metrics.render());
  }
}
