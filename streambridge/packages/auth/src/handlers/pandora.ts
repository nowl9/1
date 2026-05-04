import axios from 'axios';
import type { AuthToken } from '@streambridge/types';
import type { OAuthHandler } from '../types';
import { getStateStore } from '../stateStore';
import { randomState } from '../util';

export interface PandoraConfig { partnerUsername: string; partnerPassword: string; deviceModel: string }

/**
 * Pandora's Partner API uses a two-step partner→user login that produces an
 * X-AuthToken. There is no public OAuth code flow; mobile collects the user's
 * Pandora credentials and posts them to /auth/pandora/callback as the `code`
 * field in the form `username:password`.
 */
export function pandoraHandler(cfg: PandoraConfig): OAuthHandler {
  return {
    platform: 'pandora',
    async start(userId) {
      const state = randomState();
      await getStateStore().put(state, { userId, platform: 'pandora' });
      return { authorizationUrl: 'pandora://login', state };
    },
    async complete(code, state) {
      const stored = await getStateStore().take(state);
      if (!stored || stored.platform !== 'pandora') throw new Error('invalid state');
      const sep = code.indexOf(':');
      if (sep <= 0) throw new Error('expected username:password');
      const username = code.slice(0, sep);
      const password = code.slice(sep + 1);

      const partner = await axios.post('https://www.pandora.com/api/v1/auth/partnerLogin', {
        username: cfg.partnerUsername,
        password: cfg.partnerPassword,
        deviceModel: cfg.deviceModel,
      });
      const userLogin = await axios.post(
        'https://www.pandora.com/api/v1/auth/userLogin',
        { loginType: 'user', username, password, partnerAuthToken: partner.data.partnerAuthToken },
        { headers: { 'X-AuthToken': partner.data.partnerAuthToken } },
      );
      const token: AuthToken = {
        platform: 'pandora',
        accessToken: userLogin.data.userAuthToken,
        expiresAt: Date.now() + 24 * 60 * 60 * 1000,
        scopes: ['pandora-user'],
        tokenType: 'Bearer',
      };
      return { token, platformUserId: String(userLogin.data.userId ?? 'pandora-user') };
    },
    async refresh() {
      throw new Error('Pandora partner tokens are short-lived — re-authenticate the user');
    },
  };
}
