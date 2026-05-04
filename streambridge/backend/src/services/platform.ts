import type {
  AuthToken,
  PaginatedResponse,
  Platform,
  UniversalPlaylist,
  UniversalTrack,
} from '@streambridge/types';

/**
 * Common surface every streaming platform must implement. Routes and the
 * SyncEngine talk only to this interface, so the rest of the system stays
 * platform-agnostic.
 */
export interface PlatformService {
  readonly platform: Platform;

  listPlaylists(token: AuthToken, cursor?: string): Promise<PaginatedResponse<UniversalPlaylist>>;
  getPlaylist(token: AuthToken, externalId: string): Promise<UniversalPlaylist>;
  searchTracks(token: AuthToken, query: string, limit?: number): Promise<UniversalTrack[]>;

  /** Look up a single track by an external ID native to this platform. */
  getTrack(token: AuthToken, externalId: string): Promise<UniversalTrack | null>;

  /** Look up by ISRC; preferred high-confidence cross-platform match path. */
  findByIsrc(token: AuthToken, isrc: string): Promise<UniversalTrack | null>;

  createPlaylist(
    token: AuthToken,
    args: { name: string; description?: string; isPublic?: boolean },
  ): Promise<{ externalId: string }>;

  addTracks(token: AuthToken, externalPlaylistId: string, externalTrackIds: string[]): Promise<void>;
  removeTracks(token: AuthToken, externalPlaylistId: string, externalTrackIds: string[]): Promise<void>;
  reorderTracks?(token: AuthToken, externalPlaylistId: string, fromIndex: number, toIndex: number): Promise<void>;
}
