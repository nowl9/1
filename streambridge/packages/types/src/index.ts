/**
 * StreamBridge — shared TypeScript contracts.
 *
 * Every package consumes these types. Keep this file framework-free
 * (no React, no Node) so it can be imported from mobile, backend, and
 * server-only packages without bundler complaints.
 */

export const PLATFORMS = [
  'spotify',
  'apple-music',
  'amazon-music',
  'tidal',
  'youtube-music',
  'deezer',
  'pandora',
] as const;

export type Platform = (typeof PLATFORMS)[number];

export const PLATFORM_DISPLAY_NAMES: Record<Platform, string> = {
  spotify: 'Spotify',
  'apple-music': 'Apple Music',
  'amazon-music': 'Amazon Music',
  tidal: 'Tidal',
  'youtube-music': 'YouTube Music',
  deezer: 'Deezer',
  pandora: 'Pandora',
};

export const PLATFORM_BRAND_COLORS: Record<Platform, string> = {
  spotify: '#1DB954',
  'apple-music': '#FC3C44',
  'amazon-music': '#00A8E1',
  tidal: '#000000',
  'youtube-music': '#FF0000',
  deezer: '#A238FF',
  pandora: '#3668FF',
};

export type ConnectionStatus =
  | { state: 'connected'; expiresAt: number; scopes: string[] }
  | { state: 'expired'; expiresAt: number }
  | { state: 'disconnected' }
  | { state: 'error'; reason: string };

export interface AuthToken {
  platform: Platform;
  accessToken: string;
  refreshToken?: string;
  expiresAt: number;
  scopes: string[];
  tokenType: 'Bearer' | 'MAC';
}

export interface UniversalTrack {
  id: string;
  universalId: string;
  title: string;
  artist: string[];
  album: string;
  duration: number;
  isrc?: string;
  platformIds: Record<Platform, string | null>;
  artwork: string;
  previewUrl?: string;
  explicit: boolean;
  availableOn: Platform[];
}

export interface UniversalPlaylist {
  id: string;
  name: string;
  description?: string;
  tracks: UniversalTrack[];
  sourcePlatform: Platform;
  syncedTo: Platform[];
  lastSynced: Record<Platform, Date | null>;
  artwork?: string;
  isPublic: boolean;
  createdAt: Date;
  updatedAt: Date;
}

export type ConflictStrategy = 'source-wins' | 'newest-wins' | 'manual';

export interface SyncOptions {
  sourcePlatform: Platform;
  targetPlatforms: Platform[];
  playlistId: string;
  conflictStrategy: ConflictStrategy;
  skipUnavailable: boolean;
  createIfMissing: boolean;
}

export type SyncJobStatus =
  | 'queued'
  | 'matching'
  | 'writing'
  | 'completed'
  | 'failed'
  | 'partial';

export interface SyncJob {
  id: string;
  userId: string;
  playlistId: string;
  options: SyncOptions;
  status: SyncJobStatus;
  progress: number;
  matched: number;
  unavailable: number;
  manualReview: number;
  total: number;
  startedAt: Date;
  finishedAt?: Date;
  error?: string;
  perPlatform: Record<Platform, PlatformSyncProgress | undefined>;
}

export interface PlatformSyncProgress {
  status: SyncJobStatus;
  matched: number;
  unavailable: number;
  total: number;
  targetPlaylistId?: string;
}

export type MatchStrategy = 'isrc' | 'fuzzy-metadata' | 'audio-features' | 'manual';

export interface TrackMatch {
  sourceTrack: UniversalTrack;
  candidate: UniversalTrack | null;
  strategy: MatchStrategy;
  confidence: number;
  needsReview: boolean;
}

export interface PlatformError {
  platform: Platform;
  code: string;
  status?: number;
  message: string;
  retryable: boolean;
  retryAfterMs?: number;
}

export interface User {
  id: string;
  email: string;
  displayName: string;
  createdAt: Date;
}

export interface SearchTracksQuery {
  q: string;
  platform?: Platform;
  limit?: number;
}

export interface PaginatedResponse<T> {
  items: T[];
  nextCursor: string | null;
  total?: number;
}
