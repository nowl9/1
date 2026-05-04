import type { AuthToken } from '@streambridge/types';
import type { OAuthHandler } from '../types';
import { getStateStore } from '../stateStore';
import { randomState } from '../util';

export interface AppleConfig { teamId: string; keyId: string; privateKey: string }

/**
 * Apple Music doesn't have an OAuth2 code flow — clients use MusicKit on the
 * device to obtain a Music User Token, then post it here. `complete()` accepts
 * the Music User Token in place of an authorization code.
 */
export function appleHandler(_cfg: AppleConfig): OAuthHandler {
  return {
    platform: 'apple-music',
    async start(userId) {
      const state = randomState();
      await getStateStore().put(state, { userId, platform: 'apple-music' });
      // The mobile app will call MusicKit, then POST the user token to
      // /auth/apple-music/callback with `code` set to the Music User Token.
      return { authorizationUrl: 'musickit://authorize', state };
    },
    async complete(code, state) {
      const stored = await getStateStore().take(state);
      if (!stored || stored.platform !== 'apple-music') throw new Error('invalid state');
      const token: AuthToken = {
        platform: 'apple-music',
        accessToken: code, // Music User Token from MusicKit
        // MusicKit user tokens are valid for ~6 months; refresh by re-prompting.
        expiresAt: Date.now() + 180 * 24 * 60 * 60 * 1000,
        scopes: ['musickit'],
        tokenType: 'Bearer',
      };
      return { token, platformUserId: 'musickit-user' };
    },
    async refresh() {
      throw new Error('Apple Music user tokens cannot be refreshed — re-prompt via MusicKit');
    },
  };
}
