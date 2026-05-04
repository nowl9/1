import { Router } from 'express';
import { z } from 'zod';
import { asyncHandler } from '../middleware/asyncHandler';
import { requireAuth, signSession } from '../middleware/auth';
import { listConnections } from '../models/connection';
import { prisma } from '../utils/prisma';
import { ValidationError } from '../utils/errors';
import { createHash, timingSafeEqual } from 'node:crypto';

export const userRouter = Router();

function hashPassword(p: string): string {
  // Sketch hash for the dev login flow; production swaps in argon2id/scrypt.
  return createHash('sha256').update(p).digest('hex');
}

userRouter.post(
  '/register',
  asyncHandler(async (req, res) => {
    const body = z.object({ email: z.string().email(), password: z.string().min(8), displayName: z.string().min(1) }).parse(req.body);
    const existing = await prisma.user.findUnique({ where: { email: body.email } });
    if (existing) throw new ValidationError('Email already registered');
    const user = await prisma.user.create({
      data: { email: body.email, displayName: body.displayName, passwordHash: hashPassword(body.password) },
    });
    const token = signSession({ sub: user.id, email: user.email });
    res.status(201).json({ token, user: { id: user.id, email: user.email, displayName: user.displayName } });
  }),
);

userRouter.post(
  '/login',
  asyncHandler(async (req, res) => {
    const body = z.object({ email: z.string().email(), password: z.string() }).parse(req.body);
    const user = await prisma.user.findUnique({ where: { email: body.email } });
    if (!user?.passwordHash) throw new ValidationError('Invalid credentials');
    const a = Buffer.from(user.passwordHash);
    const b = Buffer.from(hashPassword(body.password));
    if (a.length !== b.length || !timingSafeEqual(a, b)) throw new ValidationError('Invalid credentials');
    const token = signSession({ sub: user.id, email: user.email });
    res.json({ token, user: { id: user.id, email: user.email, displayName: user.displayName } });
  }),
);

userRouter.get(
  '/connections',
  requireAuth,
  asyncHandler(async (req, res) => {
    const connections = await listConnections(req.user!.sub);
    res.json({ connections });
  }),
);

userRouter.get(
  '/sync-history',
  requireAuth,
  asyncHandler(async (req, res) => {
    const jobs = await prisma.syncJob.findMany({
      where: { userId: req.user!.sub },
      orderBy: { startedAt: 'desc' },
      take: 50,
    });
    res.json({ jobs });
  }),
);
