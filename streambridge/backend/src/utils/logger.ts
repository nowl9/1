import pino from 'pino';
import { config } from './config';

// Redact every credential-shaped field. Tokens never end up in logs.
const REDACT_PATHS = [
  'req.headers.authorization',
  'req.headers.cookie',
  '*.accessToken',
  '*.refreshToken',
  '*.access_token',
  '*.refresh_token',
  '*.client_secret',
  '*.password',
  '*.passwordHash',
  '*.accessTokenEnc',
  '*.refreshTokenEnc',
];

export const logger = pino({
  level: config.NODE_ENV === 'production' ? 'info' : 'debug',
  redact: { paths: REDACT_PATHS, censor: '[REDACTED]' },
  base: { app: 'streambridge-api' },
});
