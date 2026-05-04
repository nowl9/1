import 'dotenv/config';
import { z } from 'zod';

const schema = z.object({
  NODE_ENV: z.enum(['development', 'test', 'production']).default('development'),
  PORT: z.coerce.number().default(4000),
  DATABASE_URL: z.string().url(),
  REDIS_URL: z.string().url().default('redis://localhost:6379'),
  JWT_SECRET: z.string().min(32),
  TOKEN_ENCRYPTION_KEY: z.string().min(32), // 32-byte key, hex or base64
  APP_BASE_URL: z.string().url().default('http://localhost:4000'),

  SPOTIFY_CLIENT_ID: z.string().optional(),
  SPOTIFY_CLIENT_SECRET: z.string().optional(),
  SPOTIFY_REDIRECT_URI: z.string().optional(),

  APPLE_TEAM_ID: z.string().optional(),
  APPLE_KEY_ID: z.string().optional(),
  APPLE_PRIVATE_KEY: z.string().optional(),

  AMAZON_CLIENT_ID: z.string().optional(),
  AMAZON_CLIENT_SECRET: z.string().optional(),
  AMAZON_REDIRECT_URI: z.string().optional(),

  TIDAL_CLIENT_ID: z.string().optional(),
  TIDAL_CLIENT_SECRET: z.string().optional(),
  TIDAL_REDIRECT_URI: z.string().optional(),

  GOOGLE_CLIENT_ID: z.string().optional(),
  GOOGLE_CLIENT_SECRET: z.string().optional(),
  GOOGLE_REDIRECT_URI: z.string().optional(),

  DEEZER_APP_ID: z.string().optional(),
  DEEZER_APP_SECRET: z.string().optional(),
  DEEZER_REDIRECT_URI: z.string().optional(),

  PANDORA_PARTNER_USERNAME: z.string().optional(),
  PANDORA_PARTNER_PASSWORD: z.string().optional(),
  PANDORA_DEVICE_MODEL: z.string().default('android-generic'),
});

export const config = schema.parse(process.env);
export type AppConfig = z.infer<typeof schema>;
