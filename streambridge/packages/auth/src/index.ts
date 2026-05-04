import type { AuthToken, Platform } from '@streambridge/types';
import type { AuthConfig, OAuthHandler } from './types';
import { spotifyHandler } from './handlers/spotify';
import { amazonHandler } from './handlers/amazon';
import { tidalHandler } from './handlers/tidal';
import { googleHandler } from './handlers/google';
import { deezerHandler } from './handlers/deezer';
import { appleHandler } from './handlers/apple';
import { pandoraHandler } from './handlers/pandora';

export * from './types';
export { MemoryStateStore, getStateStore, setStateStore } from './stateStore';

const handlers: Partial<Record<Platform, OAuthHandler>> = {};

export function configureAuth(cfg: AuthConfig): void {
  if (cfg.spotify) handlers.spotify = spotifyHandler(cfg.spotify);
  if (cfg.amazon) handlers['amazon-music'] = amazonHandler(cfg.amazon);
  if (cfg.tidal) handlers.tidal = tidalHandler(cfg.tidal);
  if (cfg.google) handlers['youtube-music'] = googleHandler(cfg.google);
  if (cfg.deezer) handlers.deezer = deezerHandler(cfg.deezer);
  if (cfg.apple) handlers['apple-music'] = appleHandler(cfg.apple);
  if (cfg.pandora) handlers.pandora = pandoraHandler(cfg.pandora);
}

function get(platform: Platform): OAuthHandler {
  const h = handlers[platform];
  if (!h) throw new Error(`OAuth not configured for ${platform}`);
  return h;
}

export const startOAuth = (platform: Platform, userId: string) => get(platform).start(userId);
export const completeOAuth = (platform: Platform, code: string, state: string, userId: string) =>
  get(platform).complete(code, state, userId);
export const refreshOAuth = (platform: Platform, refreshToken: string): Promise<AuthToken> =>
  get(platform).refresh(refreshToken);
