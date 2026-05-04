import type { AuthToken, Platform } from '@streambridge/types';

export interface OAuthStartResult {
  authorizationUrl: string;
  state: string;
  /** PKCE verifier or other secret to keep server-side until callback. */
  verifier?: string;
}

export interface OAuthCompleteResult {
  token: AuthToken;
  platformUserId: string;
}

export interface OAuthHandler {
  readonly platform: Platform;
  start(userId: string): Promise<OAuthStartResult>;
  complete(code: string, state: string, userId: string): Promise<OAuthCompleteResult>;
  refresh(refreshToken: string): Promise<AuthToken>;
}

export interface AuthConfig {
  spotify?: { clientId: string; clientSecret: string; redirectUri: string };
  amazon?: { clientId: string; clientSecret: string; redirectUri: string };
  tidal?: { clientId: string; clientSecret: string; redirectUri: string };
  google?: { clientId: string; clientSecret: string; redirectUri: string };
  deezer?: { appId: string; appSecret: string; redirectUri: string };
  apple?: { teamId: string; keyId: string; privateKey: string };
  pandora?: { partnerUsername: string; partnerPassword: string; deviceModel: string };
}

export interface StateStore {
  put(state: string, value: { userId: string; verifier?: string; platform: Platform }): Promise<void>;
  take(state: string): Promise<{ userId: string; verifier?: string; platform: Platform } | null>;
}
