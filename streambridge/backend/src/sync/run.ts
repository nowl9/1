import type { Platform, SyncOptions, UniversalPlaylist } from '@streambridge/types';
import { SyncEngine } from '@streambridge/playlist-engine';
import { TrackMatcher } from '@streambridge/playlist-engine';
import { prisma } from '../utils/prisma';
import { loadToken } from '../models/connection';
import { getService } from '../services/registry';
import { logger } from '../utils/logger';

export async function runSync(jobId: string, userId: string, options: SyncOptions): Promise<void> {
  await prisma.syncJob.update({ where: { id: jobId }, data: { status: 'MATCHING' } });
  const sourceToken = await loadToken(userId, options.sourcePlatform);
  const sourceSvc = getService(options.sourcePlatform);
  const playlist: UniversalPlaylist = await sourceSvc.getPlaylist(sourceToken, options.playlistId);

  const engine = new SyncEngine({
    matcher: new TrackMatcher(),
    services: Object.fromEntries(
      await Promise.all(
        options.targetPlatforms.map(async (p) => {
          const token = await loadToken(userId, p);
          return [p, { token, service: getService(p) }];
        }),
      ),
    ) as Record<Platform, { token: import('@streambridge/types').AuthToken; service: ReturnType<typeof getService> }>,
    onProgress: async (snap) => {
      await prisma.syncJob.update({
        where: { id: jobId },
        data: {
          status: 'WRITING',
          progress: snap.progress,
          matched: snap.matched,
          unavailable: snap.unavailable,
          manualReview: snap.manualReview,
          total: snap.total,
          perPlatform: snap.perPlatform as unknown as object,
        },
      });
    },
  });

  try {
    const result = await engine.sync(playlist, options);
    await prisma.syncJob.update({
      where: { id: jobId },
      data: {
        status: result.manualReview > 0 ? 'PARTIAL' : 'COMPLETED',
        progress: 1,
        matched: result.matched,
        unavailable: result.unavailable,
        manualReview: result.manualReview,
        total: result.total,
        perPlatform: result.perPlatform as unknown as object,
        finishedAt: new Date(),
      },
    });
  } catch (err) {
    logger.error({ err, jobId }, 'sync error');
    await prisma.syncJob.update({
      where: { id: jobId },
      data: { status: 'FAILED', error: (err as Error).message, finishedAt: new Date() },
    });
    throw err;
  }
}
