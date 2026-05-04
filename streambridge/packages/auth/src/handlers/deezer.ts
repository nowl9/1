import axios from 'axios';
import type { OAuthHandler } from '../types';
import { getStateStore } from '../stateStore';
import { randomState } from '../util';
import type { AuthToken } from '@streambridge/types';

const PERMS = ['basic_access', 'email', 'manage_library', 'offline_access'];

export interface DeezerConfig { appId: string; appSecret: string; redirectUri: string }

export function deezerHandler(cfg: DeezerConfig): OAuthHandler {
  return {
    platform: 'deezer',
    async start(userId) {
      const state = randomState();
      await getStateStore().put(state, { userId, platform: 'deezer' });
      const url = new URL('https://connect.deezer.com/oauth/auth.php');
      url.searchParams.set('app_id', cfg.appId);
      url.searchParams.set('redirect_uri', cfg.redirectUri);
      url.searchParams.set('perms', PERMS.join(','));
      url.searchParams.set('state', state);
      return { authorizationUrl: url.toString(), state };
    },
    async complete(code, state) {
      const stored = await getStateStore().take(state);
      if (!stored || stored.platform !== 'deezer') throw new Error('invalid state');
      // Deezer returns plain text "access_token=...&expires=...".
      const res = await axios.get('https://connect.deezer.com/oauth/access_token.php', {
        params: { app_id: cfg.appId, secret: cfg.appSecret, code, output: 'json' },
        responseType: 'json',
      });
      const data = res.data as { access_token: string; expires: number };
      const token: AuthToken = {
        platform: 'deezer',
        accessToken: data.access_token,
        expiresAt: Date.now() + (data.expires || 0) * 1000,
        scopes: PERMS,
        tokenType: 'Bearer',
      };
      const me = await axios.get('https://api.deezer.com/user/me', {
        params: { access_token: token.accessToken },
      });
      return { token, platformUserId: String(me.data.id) };
    },
    async refresh() {
      // Deezer Connect tokens don't refresh — re-auth required.
      throw new Error('Deezer tokens cannot be refreshed; re-authenticate the user');
    },
  };
}
