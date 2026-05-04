import { Router } from 'express';
import { z } from 'zod';
import { PLATFORMS } from '@streambridge/types';
import { asyncHandler } from '../middleware/asyncHandler';
import { requireAuth } from '../middleware/auth';
import { listConnections, loadToken } from '../models/connection';
import { getService } from '../services/registry';
import { prisma } from '../utils/prisma';
import { toPrismaPlatform } from '../models/connection';

export const tracksRouter = Router();
tracksRouter.use(requireAuth);

tracksRouter.get(
  '/search',
  asyncHandler(async (req, res) => {
    const q = z.object({
      q: z.string().min(1),
      platform: z.enum(PLATFORMS).optional(),
      limit: z.coerce.number().int().min(1).max(50).default(20),
    }).parse(req.query);
    const userId = req.user!.sub;
    const connections = await listConnections(userId);
    const targets = q.platform ? connections.filter((c) => c.platform === q.platform) : connections;
    const results = await Promise.allSettled(
      targets.map(async (c) => {
        const token = await loadToken(userId, c.platform);
        const tracks = await getService(c.platform).searchTracks(token, q.q, q.limit);
        return { platform: c.platform, tracks };
      }),
    );
    res.json({
      results: results.map((r) =>
        r.status === 'fulfilled' ? { ok: true, ...r.value } : { ok: false, error: (r.reason as Error).message },
      ),
    });
  }),
);

tracksRouter.post(
  '/match',
  asyncHandler(async (req, res) => {
    // Persist a manual override so future syncs reuse it for this (user, source, platform).
    const body = z.object({
      sourceTrackId: z.string(),
      candidateTrackId: z.string(),
      platform: z.enum(PLATFORMS),
    }).parse(req.body);
    const userId = req.user!.sub;
    const match = await prisma.trackMatch.upsert({
      where: { userId_sourceId_platform: { userId, sourceId: body.sourceTrackId, platform: toPrismaPlatform(body.platform) } },
      create: {
        userId,
        sourceId: body.sourceTrackId,
        candidateId: body.candidateTrackId,
        platform: toPrismaPlatform(body.platform),
        strategy: 'MANUAL',
        confidence: 1,
        manual: true,
      },
      update: { candidateId: body.candidateTrackId, strategy: 'MANUAL', confidence: 1, manual: true },
    });
    res.json({ match });
  }),
);
