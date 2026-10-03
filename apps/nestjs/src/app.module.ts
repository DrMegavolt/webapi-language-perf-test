import { Module } from '@nestjs/common';
import { AppController } from './app.controller';
import { DbService } from './db.service';
import { MetricsService } from './metrics.service';

@Module({
  controllers: [AppController],
  providers: [DbService, MetricsService],
})
export class AppModule {}
