import axios from 'axios';
import type { OAuthHandler } from '../types';
import { getStateStore } from '../stateStore';
import { randomState, tokenFromOAuth2 } from '../util';

const SCOPES = ['https://www.googleapis.com/auth/youtube', 'openid', 'email', 'profile'];

export interface GoogleConfig { clientId: string; clientSecret: string; redirectUri: string }

export function googleHandler(cfg: GoogleConfig): OAuthHandler {
  return {
    platform: 'youtube-music',
    async start(userId) {
      const state = randomState();
      await getStateStore().put(state, { userId, platform: 'youtube-music' });
      const url = new URL('https://accounts.google.com/o/oauth2/v2/auth');
      url.searchParams.set('response_type', 'code');
      url.searchParams.set('client_id', cfg.clientId);
      url.searchParams.set('redirect_uri', cfg.redirectUri);
      url.searchParams.set('scope', SCOPES.join(' '));
      url.searchParams.set('access_type', 'offline');
      url.searchParams.set('include_granted_scopes', 'true');
      url.searchParams.set('prompt', 'consent');
      url.searchParams.set('state', state);
      return { authorizationUrl: url.toString(), state };
    },
    async complete(code, state) {
      const stored = await getStateStore().take(state);
      if (!stored || stored.platform !== 'youtube-music') throw new Error('invalid state');
      const res = await axios.post(
        'https://oauth2.googleapis.com/token',
        new URLSearchParams({
          grant_type: 'authorization_code',
          code,
          client_id: cfg.clientId,
          client_secret: cfg.clientSecret,
          redirect_uri: cfg.redirectUri,
        }),
        { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } },
      );
      const token = tokenFromOAuth2({ platform: 'youtube-music', data: res.data, scopesFallback: SCOPES });
      const me = await axios.get('https://www.googleapis.com/oauth2/v3/userinfo', {
        headers: { Authorization: `Bearer ${token.accessToken}` },
      });
      return { token, platformUserId: me.data.sub };
    },
    async refresh(refreshToken) {
      const res = await axios.post(
        'https://oauth2.googleapis.com/token',
        new URLSearchParams({
          grant_type: 'refresh_token',
          refresh_token: refreshToken,
          client_id: cfg.clientId,
          client_secret: cfg.clientSecret,
        }),
        { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } },
      );
      const t = tokenFromOAuth2({ platform: 'youtube-music', data: res.data, scopesFallback: SCOPES });
      if (!t.refreshToken) t.refreshToken = refreshToken;
      return t;
    },
  };
}
