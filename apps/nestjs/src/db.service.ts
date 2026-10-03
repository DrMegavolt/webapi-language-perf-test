import { Injectable, OnModuleDestroy } from '@nestjs/common';
import { Pool, types, type QueryResult } from 'pg';

// pg returns int8 (BIGSERIAL ids, COUNT(*)::bigint) as strings; parse them to
// JS numbers so every numeric field is a JSON number (ids/counts are small).
types.setTypeParser(20, (v) => parseInt(v, 10));

@Injectable()
export class DbService implements OnModuleDestroy {
  private readonly pool: Pool;

  constructor() {
    this.pool = new Pool({
      connectionString: process.env.DATABASE_URL,
      max: 8,
    });
  }

  query(text: string, values: unknown[] = []): Promise<QueryResult> {
    return this.pool.query(text, values as unknown[]);
  }

  async onModuleDestroy(): Promise<void> {
    await this.pool.end();
  }
}
