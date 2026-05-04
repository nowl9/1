import { Queue, Worker, QueueEvents } from 'bullmq';
import IORedis from 'ioredis';
import type { SyncJob, SyncJobStatus, SyncOptions } from '@streambridge/types';
import { config } from '../utils/config';
import { prisma } from '../utils/prisma';
import { logger } from '../utils/logger';
import { runSync } from './run';
import { toPrismaPlatform } from '../models/connection';

const QUEUE_NAME = 'streambridge-sync';

const connection = new IORedis(config.REDIS_URL, { maxRetriesPerRequest: null });

export const syncQueue = new Queue<{ jobId: string; userId: string; options: SyncOptions }>(QUEUE_NAME, {
  connection,
  defaultJobOptions: { attempts: 3, backoff: { type: 'exponential', delay: 5_000 } },
});

export const syncEvents = new QueueEvents(QUEUE_NAME, { connection });

export async function enqueueSync(userId: string, options: SyncOptions): Promise<{ id: string }> {
  // Resolve playlistId to a row in our DB, creating a stub if needed.
  const playlist = await prisma.playlist.upsert({
    where: { id: options.playlistId },
    update: {},
    create: {
      id: options.playlistId,
      userId,
      name: 'Imported playlist',
      sourcePlatform: toPrismaPlatform(options.sourcePlatform),
    },
  });
  const job = await prisma.syncJob.create({
    data: {
      userId,
      playlistId: playlist.id,
      options: options as unknown as object,
      status: 'QUEUED',
    },
  });
  await syncQueue.add('sync', { jobId: job.id, userId, options });
  return { id: job.id };
}

export async function getSyncJob(userId: string, jobId: string): Promise<SyncJob | null> {
  const row = await prisma.syncJob.findUnique({ where: { id: jobId } });
  if (!row || row.userId !== userId) return null;
  return {
    id: row.id,
    userId: row.userId,
    playlistId: row.playlistId,
    options: row.options as unknown as SyncOptions,
    status: row.status.toLowerCase() as SyncJobStatus,
    progress: row.progress,
    matched: row.matched,
    unavailable: row.unavailable,
    manualReview: row.manualReview,
    total: row.total,
    startedAt: row.startedAt,
    finishedAt: row.finishedAt ?? undefined,
    error: row.error ?? undefined,
    perPlatform: row.perPlatform as SyncJob['perPlatform'],
  };
}

// Worker is started by the server entrypoint when SYNC_WORKER=1.
export function startWorker(): Worker {
  const worker = new Worker(
    QUEUE_NAME,
    async (job) => {
      const data = job.data;
      logger.info({ jobId: data.jobId }, 'sync.start');
      await runSync(data.jobId, data.userId, data.options);
      logger.info({ jobId: data.jobId }, 'sync.done');
    },
    { connection, concurrency: 4 },
  );
  worker.on('failed', (job, err) => logger.error({ jobId: job?.id, err }, 'sync.failed'));
  return worker;
}
