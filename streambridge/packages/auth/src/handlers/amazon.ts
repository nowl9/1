import axios from 'axios';
import type { OAuthHandler } from '../types';
import { getStateStore } from '../stateStore';
import { randomState, tokenFromOAuth2 } from '../util';

const SCOPES = ['profile', 'music:playlist:read', 'music:playlist:write'];

export interface AmazonConfig { clientId: string; clientSecret: string; redirectUri: string }

export function amazonHandler(cfg: AmazonConfig): OAuthHandler {
  return {
    platform: 'amazon-music',
    async start(userId) {
      const state = randomState();
      await getStateStore().put(state, { userId, platform: 'amazon-music' });
      const url = new URL('https://www.amazon.com/ap/oa');
      url.searchParams.set('client_id', cfg.clientId);
      url.searchParams.set('scope', SCOPES.join(' '));
      url.searchParams.set('response_type', 'code');
      url.searchParams.set('redirect_uri', cfg.redirectUri);
      url.searchParams.set('state', state);
      return { authorizationUrl: url.toString(), state };
    },
    async complete(code, state) {
      const stored = await getStateStore().take(state);
      if (!stored || stored.platform !== 'amazon-music') throw new Error('invalid state');
      const res = await axios.post(
        'https://api.amazon.com/auth/o2/token',
        new URLSearchParams({
          grant_type: 'authorization_code',
          code,
          client_id: cfg.clientId,
          client_secret: cfg.clientSecret,
          redirect_uri: cfg.redirectUri,
        }),
        { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } },
      );
      const token = tokenFromOAuth2({ platform: 'amazon-music', data: res.data, scopesFallback: SCOPES });
      const profile = await axios.get('https://api.amazon.com/user/profile', {
        headers: { Authorization: `Bearer ${token.accessToken}` },
      });
      return { token, platformUserId: profile.data.user_id };
    },
    async refresh(refreshToken) {
      const res = await axios.post(
        'https://api.amazon.com/auth/o2/token',
        new URLSearchParams({
          grant_type: 'refresh_token',
          refresh_token: refreshToken,
          client_id: cfg.clientId,
          client_secret: cfg.clientSecret,
        }),
        { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } },
      );
      return tokenFromOAuth2({ platform: 'amazon-music', data: res.data, scopesFallback: SCOPES });
    },
  };
}
