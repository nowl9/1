import express from 'express';
import cors from 'cors';
import helmet from 'helmet';
import rateLimit from 'express-rate-limit';
import pinoHttp from 'pino-http';
import { config } from './utils/config';
import { logger } from './utils/logger';
import { errorHandler } from './middleware/error';
import { authRouter } from './routes/auth.routes';
import { playlistsRouter } from './routes/playlists.routes';
import { tracksRouter } from './routes/tracks.routes';
import { userRouter } from './routes/user.routes';
import { startWorker } from './sync/queue';
import { configureAuth } from '@streambridge/auth';

function configureAuthFromEnv(): void {
  configureAuth({
    spotify: config.SPOTIFY_CLIENT_ID && config.SPOTIFY_CLIENT_SECRET && config.SPOTIFY_REDIRECT_URI
      ? { clientId: config.SPOTIFY_CLIENT_ID, clientSecret: config.SPOTIFY_CLIENT_SECRET, redirectUri: config.SPOTIFY_REDIRECT_URI }
      : undefined,
    amazon: config.AMAZON_CLIENT_ID && config.AMAZON_CLIENT_SECRET && config.AMAZON_REDIRECT_URI
      ? { clientId: config.AMAZON_CLIENT_ID, clientSecret: config.AMAZON_CLIENT_SECRET, redirectUri: config.AMAZON_REDIRECT_URI }
      : undefined,
    tidal: config.TIDAL_CLIENT_ID && config.TIDAL_CLIENT_SECRET && config.TIDAL_REDIRECT_URI
      ? { clientId: config.TIDAL_CLIENT_ID, clientSecret: config.TIDAL_CLIENT_SECRET, redirectUri: config.TIDAL_REDIRECT_URI }
      : undefined,
    google: config.GOOGLE_CLIENT_ID && config.GOOGLE_CLIENT_SECRET && config.GOOGLE_REDIRECT_URI
      ? { clientId: config.GOOGLE_CLIENT_ID, clientSecret: config.GOOGLE_CLIENT_SECRET, redirectUri: config.GOOGLE_REDIRECT_URI }
      : undefined,
    deezer: config.DEEZER_APP_ID && config.DEEZER_APP_SECRET && config.DEEZER_REDIRECT_URI
      ? { appId: config.DEEZER_APP_ID, appSecret: config.DEEZER_APP_SECRET, redirectUri: config.DEEZER_REDIRECT_URI }
      : undefined,
    apple: config.APPLE_TEAM_ID && config.APPLE_KEY_ID && config.APPLE_PRIVATE_KEY
      ? { teamId: config.APPLE_TEAM_ID, keyId: config.APPLE_KEY_ID, privateKey: config.APPLE_PRIVATE_KEY }
      : undefined,
    pandora: config.PANDORA_PARTNER_USERNAME && config.PANDORA_PARTNER_PASSWORD
      ? { partnerUsername: config.PANDORA_PARTNER_USERNAME, partnerPassword: config.PANDORA_PARTNER_PASSWORD, deviceModel: config.PANDORA_DEVICE_MODEL }
      : undefined,
  });
}

export function createApp(): express.Express {
  configureAuthFromEnv();
  const app = express();
  app.use(helmet());
  app.use(cors());
  app.use(express.json({ limit: '1mb' }));
  app.use(pinoHttp({ logger }));
  app.use(
    rateLimit({
      windowMs: 60_000,
      limit: 240,
      standardHeaders: 'draft-7',
      legacyHeaders: false,
    }),
  );

  app.get('/health', (_req, res) => res.json({ ok: true, env: config.NODE_ENV }));
  app.use('/auth', authRouter);
  app.use('/playlists', playlistsRouter);
  app.use('/tracks', tracksRouter);
  app.use('/user', userRouter);

  app.use(errorHandler);
  return app;
}

if (require.main === module) {
  const app = createApp();
  app.listen(config.PORT, () => logger.info({ port: config.PORT }, 'streambridge api listening'));
  if (process.env.SYNC_WORKER === '1') startWorker();
}
