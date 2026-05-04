import { Router } from 'express';
import { z } from 'zod';
import { PLATFORMS } from '@streambridge/types';
import { asyncHandler } from '../middleware/asyncHandler';
import { requireAuth } from '../middleware/auth';
import { revokeConnection, saveConnection } from '../models/connection';
import { startOAuth, completeOAuth } from '@streambridge/auth';

export const authRouter = Router();

const PlatformParam = z.object({ platform: z.enum(PLATFORMS) });

authRouter.post(
  '/:platform/connect',
  requireAuth,
  asyncHandler(async (req, res) => {
    const { platform } = PlatformParam.parse(req.params);
    const userId = req.user!.sub;
    const { authorizationUrl, state } = await startOAuth(platform, userId);
    res.json({ authorizationUrl, state });
  }),
);

authRouter.post(
  '/:platform/callback',
  requireAuth,
  asyncHandler(async (req, res) => {
    const { platform } = PlatformParam.parse(req.params);
    const body = z.object({ code: z.string(), state: z.string() }).parse(req.body);
    const userId = req.user!.sub;
    const { token, platformUserId } = await completeOAuth(platform, body.code, body.state, userId);
    await saveConnection({ userId, platform, platformUserId, token });
    res.json({ ok: true, expiresAt: token.expiresAt });
  }),
);

authRouter.delete(
  '/:platform/disconnect',
  requireAuth,
  asyncHandler(async (req, res) => {
    const { platform } = PlatformParam.parse(req.params);
    await revokeConnection(req.user!.sub, platform);
    res.json({ ok: true });
  }),
);
