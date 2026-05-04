import axios from 'axios';
import type { OAuthHandler } from '../types';
import { getStateStore } from '../stateStore';
import { pkcePair, randomState, tokenFromOAuth2 } from '../util';

const SCOPES = [
  'playlist-read-private',
  'playlist-read-collaborative',
  'playlist-modify-public',
  'playlist-modify-private',
  'user-read-private',
];

export interface SpotifyConfig { clientId: string; clientSecret: string; redirectUri: string }

export function spotifyHandler(cfg: SpotifyConfig): OAuthHandler {
  return {
    platform: 'spotify',
    async start(userId) {
      const state = randomState();
      const { verifier, challenge } = pkcePair();
      await getStateStore().put(state, { userId, verifier, platform: 'spotify' });
      const url = new URL('https://accounts.spotify.com/authorize');
      url.searchParams.set('response_type', 'code');
      url.searchParams.set('client_id', cfg.clientId);
      url.searchParams.set('redirect_uri', cfg.redirectUri);
      url.searchParams.set('state', state);
      url.searchParams.set('scope', SCOPES.join(' '));
      url.searchParams.set('code_challenge_method', 'S256');
      url.searchParams.set('code_challenge', challenge);
      return { authorizationUrl: url.toString(), state, verifier };
    },
    async complete(code, state) {
      const stored = await getStateStore().take(state);
      if (!stored || stored.platform !== 'spotify') throw new Error('invalid state');
      const res = await axios.post(
        'https://accounts.spotify.com/api/token',
        new URLSearchParams({
          grant_type: 'authorization_code',
          code,
          redirect_uri: cfg.redirectUri,
          client_id: cfg.clientId,
          code_verifier: stored.verifier ?? '',
        }),
        { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } },
      );
      const token = tokenFromOAuth2({ platform: 'spotify', data: res.data, scopesFallback: SCOPES });
      const me = await axios.get('https://api.spotify.com/v1/me', {
        headers: { Authorization: `Bearer ${token.accessToken}` },
      });
      return { token, platformUserId: me.data.id };
    },
    async refresh(refreshToken) {
      const res = await axios.post(
        'https://accounts.spotify.com/api/token',
        new URLSearchParams({
          grant_type: 'refresh_token',
          refresh_token: refreshToken,
          client_id: cfg.clientId,
        }),
        {
          headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
            Authorization: 'Basic ' + Buffer.from(`${cfg.clientId}:${cfg.clientSecret}`).toString('base64'),
          },
        },
      );
      // Spotify may omit refresh_token in refresh responses; preserve the old one.
      const t = tokenFromOAuth2({ platform: 'spotify', data: res.data, scopesFallback: SCOPES });
      if (!t.refreshToken) t.refreshToken = refreshToken;
      return t;
    },
  };
}
