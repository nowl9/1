import axios from 'axios';
import type { OAuthHandler } from '../types';
import { getStateStore } from '../stateStore';
import { pkcePair, randomState, tokenFromOAuth2 } from '../util';

const SCOPES = ['playlists.read', 'playlists.write', 'user.read'];

export interface TidalConfig { clientId: string; clientSecret: string; redirectUri: string }

export function tidalHandler(cfg: TidalConfig): OAuthHandler {
  return {
    platform: 'tidal',
    async start(userId) {
      const state = randomState();
      const { verifier, challenge } = pkcePair();
      await getStateStore().put(state, { userId, verifier, platform: 'tidal' });
      const url = new URL('https://login.tidal.com/authorize');
      url.searchParams.set('response_type', 'code');
      url.searchParams.set('client_id', cfg.clientId);
      url.searchParams.set('redirect_uri', cfg.redirectUri);
      url.searchParams.set('scope', SCOPES.join(' '));
      url.searchParams.set('state', state);
      url.searchParams.set('code_challenge_method', 'S256');
      url.searchParams.set('code_challenge', challenge);
      return { authorizationUrl: url.toString(), state, verifier };
    },
    async complete(code, state) {
      const stored = await getStateStore().take(state);
      if (!stored || stored.platform !== 'tidal') throw new Error('invalid state');
      const res = await axios.post(
        'https://auth.tidal.com/v1/oauth2/token',
        new URLSearchParams({
          grant_type: 'authorization_code',
          code,
          redirect_uri: cfg.redirectUri,
          client_id: cfg.clientId,
          client_secret: cfg.clientSecret,
          code_verifier: stored.verifier ?? '',
        }),
        { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } },
      );
      const token = tokenFromOAuth2({ platform: 'tidal', data: res.data, scopesFallback: SCOPES });
      const me = await axios.get('https://openapi.tidal.com/v2/users/me', {
        headers: { Authorization: `Bearer ${token.accessToken}`, Accept: 'application/vnd.api+json' },
      });
      return { token, platformUserId: String(me.data?.data?.id ?? '') };
    },
    async refresh(refreshToken) {
      const res = await axios.post(
        'https://auth.tidal.com/v1/oauth2/token',
        new URLSearchParams({
          grant_type: 'refresh_token',
          refresh_token: refreshToken,
          client_id: cfg.clientId,
          client_secret: cfg.clientSecret,
        }),
        { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } },
      );
      const t = tokenFromOAuth2({ platform: 'tidal', data: res.data, scopesFallback: SCOPES });
      if (!t.refreshToken) t.refreshToken = refreshToken;
      return t;
    },
  };
}
