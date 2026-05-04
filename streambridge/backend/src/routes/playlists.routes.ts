import { Router } from 'express';
import { z } from 'zod';
import { PLATFORMS } from '@streambridge/types';
import { asyncHandler } from '../middleware/asyncHandler';
import { requireAuth } from '../middleware/auth';
import { listConnections, loadToken } from '../models/connection';
import { getService } from '../services/registry';
import { enqueueSync, getSyncJob } from '../sync/queue';
import { NotFoundError } from '../utils/errors';

export const playlistsRouter = Router();

playlistsRouter.use(requireAuth);

playlistsRouter.get(
  '/',
  asyncHandler(async (req, res) => {
    const userId = req.user!.sub;
    const filter = z.object({ platform: z.enum(PLATFORMS).optional(), cursor: z.string().optional() }).parse(req.query);
    const connections = await listConnections(userId);
    const targets = filter.platform
      ? connections.filter((c) => c.platform === filter.platform)
      : connections;
    // Concurrent fanout — one platform's failure shouldn't sink the whole call.
    const results = await Promise.allSettled(
      targets.map(async (c) => {
        const token = await loadToken(userId, c.platform);
        const page = await getService(c.platform).listPlaylists(token, filter.cursor);
        return { platform: c.platform, ...page };
      }),
    );
    const out = results.map((r) =>
      r.status === 'fulfilled'
        ? { ok: true as const, ...r.value }
        : { ok: false as const, error: (r.reason as Error).message },
    );
    res.json({ results: out });
  }),
);

playlistsRouter.get(
  '/:id',
  asyncHandler(async (req, res) => {
    // id is "{platform}:{externalId}"
    const id = req.params.id ?? '';
    const colon = id.indexOf(':');
    if (colon <= 0) throw new NotFoundError('Playlist');
    const platform = id.slice(0, colon) as (typeof PLATFORMS)[number];
    if (!PLATFORMS.includes(platform)) throw new NotFoundError('Playlist');
    const token = await loadToken(req.user!.sub, platform);
    const playlist = await getService(platform).getPlaylist(token, id);
    res.json({ playlist });
  }),
);

playlistsRouter.post(
  '/sync',
  asyncHandler(async (req, res) => {
    const body = z
      .object({
        sourcePlatform: z.enum(PLATFORMS),
        targetPlatforms: z.array(z.enum(PLATFORMS)).min(1),
        playlistId: z.string(),
        conflictStrategy: z.enum(['source-wins', 'newest-wins', 'manual']).default('source-wins'),
        skipUnavailable: z.boolean().default(true),
        createIfMissing: z.boolean().default(true),
      })
      .parse(req.body);
    const job = await enqueueSync(req.user!.sub, body);
    res.status(202).json({ jobId: job.id });
  }),
);

playlistsRouter.get(
  '/:id/sync-status/:jobId',
  asyncHandler(async (req, res) => {
    const job = await getSyncJob(req.user!.sub, req.params.jobId!);
    if (!job) throw new NotFoundError('Sync job');
    res.json({ job });
  }),
);
