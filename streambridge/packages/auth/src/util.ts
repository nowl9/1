import { createHash, randomBytes } from 'node:crypto';
import type { AuthToken, Platform } from '@streambridge/types';

export function randomState(): string {
  return randomBytes(24).toString('base64url');
}

export function pkcePair(): { verifier: string; challenge: string } {
  const verifier = randomBytes(32).toString('base64url');
  const challenge = createHash('sha256').update(verifier).digest('base64url');
  return { verifier, challenge };
}

export function tokenFromOAuth2(args: {
  platform: Platform;
  data: { access_token: string; refresh_token?: string; expires_in: number; scope?: string; token_type?: string };
  scopesFallback?: string[];
}): AuthToken {
  const scopes = args.data.scope ? args.data.scope.split(/[\s,]+/).filter(Boolean) : args.scopesFallback ?? [];
  const token: AuthToken = {
    platform: args.platform,
    accessToken: args.data.access_token,
    expiresAt: Date.now() + args.data.expires_in * 1000,
    scopes,
    tokenType: (args.data.token_type as 'Bearer') ?? 'Bearer',
  };
  if (args.data.refresh_token !== undefined) token.refreshToken = args.data.refresh_token;
  return token;
}
