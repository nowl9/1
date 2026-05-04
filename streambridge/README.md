# StreamBridge

A universal music playlist manager that syncs playlists across **Spotify, Apple Music, Amazon Music, Tidal, YouTube Music, Deezer, and Pandora**.

```
streambridge/
├── apps/
│   └── mobile/              React Native (Expo SDK 51) iOS + Android app
├── packages/
│   ├── api-client/          Typed REST client used by mobile
│   ├── auth/                OAuth handlers per streaming service
│   ├── playlist-engine/     TrackMatcher + SyncEngine
│   ├── ui/                  Shared React Native component library
│   └── types/               Shared TypeScript contracts
├── backend/                 Express + Prisma + BullMQ API
├── package.json             Turborepo root
└── turbo.json
```

## Prerequisites

- Node.js >= 20
- pnpm >= 9 (`npm i -g pnpm`)
- PostgreSQL 14+ and Redis 6+ (a local `docker compose` is the simplest path)
- For the mobile app: Xcode (iOS) and/or Android Studio + JDK 17, plus the Expo CLI (`pnpm dlx expo`)
- For builds: an [Expo](https://expo.dev) account if you plan to use EAS Build

## Quickstart

```bash
git clone <repo>
cd streambridge
pnpm install
cp .env.example .env

# Database
pnpm --filter @streambridge/backend prisma:migrate

# In one terminal — API + sync worker
SYNC_WORKER=1 pnpm --filter @streambridge/backend dev

# In another — Expo
pnpm --filter @streambridge/mobile dev
```

## Registering API keys

Each platform requires its own developer credentials. The full list of required environment variables lives in `.env.example`.

| Platform       | Where to register                                                                 | What you'll need                                |
| -------------- | --------------------------------------------------------------------------------- | ----------------------------------------------- |
| Spotify        | https://developer.spotify.com/dashboard                                           | Client ID, Client Secret, Redirect URI          |
| Apple Music    | https://developer.apple.com/account/resources/authkeys/list                       | Team ID, Key ID, MusicKit private key (`.p8`)   |
| Amazon Music   | https://developer.amazon.com/loginwithamazon/console/site/lwa/overview.html       | Client ID, Client Secret, Allowed Return URLs   |
| Tidal          | https://developer.tidal.com                                                       | Client ID, Client Secret, Redirect URI          |
| YouTube Music  | https://console.cloud.google.com/apis/credentials (enable YouTube Data API v3)    | OAuth Client ID, Secret, Redirect URI           |
| Deezer         | https://developers.deezer.com/myapps                                              | App ID, Secret Key, Redirect URI                |
| Pandora        | https://partners.pandora.com (request partner access)                             | Partner username/password, device model         |

Every redirect URI registered with a provider must match one of:

```
{APP_BASE_URL}/auth/{platform-id}/callback
```

where `{platform-id}` is one of `spotify`, `apple-music`, `amazon-music`, `tidal`, `youtube-music`, `deezer`, `pandora`.

## Architecture overview

### Backend
- `backend/src/services/*.service.ts` — one adapter per streaming platform, all implementing the common `PlatformService` interface (list/get playlists, search, add/remove tracks, ISRC lookup, create).
- `backend/src/sync/queue.ts` — BullMQ queue + worker for sync jobs; persists state to Postgres via Prisma.
- `backend/src/middleware/auth.ts` — JWT session authentication for the mobile app.
- `backend/src/utils/crypto.ts` — AES-256-GCM encryption for OAuth tokens at rest. Logs are redacted via Pino so tokens never leak.

### Playlist engine (`packages/playlist-engine`)
- **`TrackMatcher`** — multi-strategy matching:
  1. ISRC direct lookup (confidence 1.0)
  2. Fuse.js + a hand-rolled composite score (title + artist + duration + album + explicit-bit)
  3. Spotify Audio Features tiebreaker for ambiguous candidates (BPM, key, energy, danceability, valence)
  4. Manual override resolved at call time
- **`SyncEngine`** — orchestrates one source playlist → N target platforms with three conflict strategies (`source-wins`, `newest-wins`, `manual`), bounded concurrency, and per-platform progress callbacks.

### Auth (`packages/auth`)
A single `OAuthHandler` interface implemented per platform (Spotify + Tidal use Authorization Code with PKCE; Apple Music wraps a MusicKit token; Pandora wraps the partner-login flow). Plug in a Redis-backed `StateStore` for multi-instance deployments.

### Mobile (`apps/mobile`)
- React Native (Expo SDK 51), TypeScript, NativeWind, Reanimated 3.
- Zustand for app state, React Query for server state, `expo-secure-store` for the JWT, `react-native-mmkv` for offline playlist cache.
- Background sync via `expo-background-fetch` + `expo-notifications` for completion alerts.
- Screens: Welcome, ConnectServices, Permissions, Home, Playlists, PlaylistDetail, Sync, Discover, Settings, plus three modals (TrackMatchReview, PlatformPicker, CreatePlaylist).

## API surface

```
POST   /user/register
POST   /user/login
GET    /user/connections
GET    /user/sync-history

POST   /auth/:platform/connect           → returns { authorizationUrl, state }
POST   /auth/:platform/callback          → body: { code, state }
DELETE /auth/:platform/disconnect

GET    /playlists                        → fan-out across all connected services
GET    /playlists/:id                    → id format: "{platform}:{externalId}"
POST   /playlists/sync                   → enqueue a sync job
GET    /playlists/:id/sync-status/:jobId → poll until completed/failed/partial

GET    /tracks/search?q=&platform=
POST   /tracks/match                     → record a manual override
```

All routes (other than `/user/login` and `/user/register`) require a `Bearer <jwt>` header.

## Testing

```bash
pnpm --filter @streambridge/playlist-engine test    # TrackMatcher unit tests
pnpm --filter @streambridge/backend test            # API tests (MSW for platform mocks)
```

## Production checklist

- [ ] Rotate `JWT_SECRET` and `TOKEN_ENCRYPTION_KEY` (32 raw bytes / 64 hex chars)
- [ ] Run `prisma:deploy` against the production database
- [ ] Run an additional process with `SYNC_WORKER=1` for the BullMQ worker
- [ ] Swap `MemoryStateStore` for a Redis-backed implementation in `@streambridge/auth`
- [ ] Configure certificate pinning per the Expo docs (`expo-network` + native config)
- [ ] Add Posthog API key to `EXPO_PUBLIC_POSTHOG_KEY` for product analytics
- [ ] Submit OAuth verification for Google (YouTube) and Spotify if going public

## License

MIT
