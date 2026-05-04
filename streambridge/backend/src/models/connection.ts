import type { Platform as PrismaPlatform } from '@prisma/client';
import type { AuthToken, Platform } from '@streambridge/types';
import { prisma } from '../utils/prisma';
import { encryptToken, decryptToken } from '../utils/crypto';
import { AuthError } from '../utils/errors';

const PLATFORM_TO_PRISMA: Record<Platform, PrismaPlatform> = {
  spotify: 'SPOTIFY',
  'apple-music': 'APPLE_MUSIC',
  'amazon-music': 'AMAZON_MUSIC',
  tidal: 'TIDAL',
  'youtube-music': 'YOUTUBE_MUSIC',
  deezer: 'DEEZER',
  pandora: 'PANDORA',
};

const PRISMA_TO_PLATFORM: Record<PrismaPlatform, Platform> = Object.fromEntries(
  Object.entries(PLATFORM_TO_PRISMA).map(([k, v]) => [v, k]),
) as Record<PrismaPlatform, Platform>;

export const toPrismaPlatform = (p: Platform): PrismaPlatform => PLATFORM_TO_PRISMA[p];
export const toPlatform = (p: PrismaPlatform): Platform => PRISMA_TO_PLATFORM[p];

export async function saveConnection(args: {
  userId: string;
  platform: Platform;
  platformUserId: string;
  token: AuthToken;
}): Promise<void> {
  const data = {
    userId: args.userId,
    platform: toPrismaPlatform(args.platform),
    platformUserId: args.platformUserId,
    accessTokenEnc: encryptToken(args.token.accessToken),
    refreshTokenEnc: args.token.refreshToken ? encryptToken(args.token.refreshToken) : null,
    expiresAt: new Date(args.token.expiresAt),
    scopes: args.token.scopes,
    tokenType: args.token.tokenType,
    revokedAt: null,
  };
  await prisma.connectedService.upsert({
    where: { userId_platform: { userId: args.userId, platform: data.platform } },
    create: data,
    update: data,
  });
}

export async function loadToken(userId: string, platform: Platform): Promise<AuthToken> {
  const row = await prisma.connectedService.findUnique({
    where: { userId_platform: { userId, platform: toPrismaPlatform(platform) } },
  });
  if (!row || row.revokedAt) throw new AuthError(`${platform} not connected`, 'platform_not_connected');
  return {
    platform,
    accessToken: decryptToken(row.accessTokenEnc),
    refreshToken: row.refreshTokenEnc ? decryptToken(row.refreshTokenEnc) : undefined,
    expiresAt: row.expiresAt.getTime(),
    scopes: row.scopes,
    tokenType: row.tokenType as AuthToken['tokenType'],
  };
}

export async function revokeConnection(userId: string, platform: Platform): Promise<void> {
  await prisma.connectedService.update({
    where: { userId_platform: { userId, platform: toPrismaPlatform(platform) } },
    data: { revokedAt: new Date(), accessTokenEnc: '', refreshTokenEnc: null },
  });
}

export async function listConnections(userId: string): Promise<Array<{ platform: Platform; expiresAt: Date; scopes: string[] }>> {
  const rows = await prisma.connectedService.findMany({
    where: { userId, revokedAt: null },
    select: { platform: true, expiresAt: true, scopes: true },
  });
  return rows.map((r) => ({ platform: toPlatform(r.platform), expiresAt: r.expiresAt, scopes: r.scopes }));
}
