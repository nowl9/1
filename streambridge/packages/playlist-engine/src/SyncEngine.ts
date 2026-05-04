import type {
  AuthToken,
  Platform,
  PlatformSyncProgress,
  SyncJobStatus,
  SyncOptions,
  TrackMatch,
  UniversalPlaylist,
  UniversalTrack,
} from '@streambridge/types';
import { TrackMatcher, type MatchProvider } from './TrackMatcher';

export interface PlatformBinding {
  token: AuthToken;
  service: PlatformWriter & MatchProvider;
}

export interface PlatformWriter {
  readonly platform: Platform;
  getPlaylist(token: AuthToken, externalId: string): Promise<UniversalPlaylist>;
  createPlaylist(
    token: AuthToken,
    args: { name: string; description?: string; isPublic?: boolean },
  ): Promise<{ externalId: string }>;
  addTracks(token: AuthToken, externalPlaylistId: string, externalTrackIds: string[]): Promise<void>;
  removeTracks(token: AuthToken, externalPlaylistId: string, externalTrackIds: string[]): Promise<void>;
}

export interface SyncResult {
  matched: number;
  unavailable: number;
  manualReview: number;
  total: number;
  perPlatform: Record<Platform, PlatformSyncProgress | undefined>;
}

export interface SyncProgressSnapshot extends SyncResult {
  progress: number; // 0..1
}

export interface SyncEngineOptions {
  matcher: TrackMatcher;
  services: Partial<Record<Platform, PlatformBinding>>;
  /** Optional resolver for previously-recorded manual matches per platform. */
  resolveManualOverride?: (
    platform: Platform,
    track: UniversalTrack,
  ) => Promise<UniversalTrack | null>;
  /**
   * Map our internal playlist id to the externalId already known on a target
   * platform. Returning null means "no playlist exists yet — create one".
   */
  resolveTargetPlaylist?: (
    platform: Platform,
    sourcePlaylist: UniversalPlaylist,
  ) => Promise<string | null>;
  onProgress?: (snap: SyncProgressSnapshot) => void | Promise<void>;
}

export class SyncEngine {
  constructor(private readonly opts: SyncEngineOptions) {}

  async sync(playlist: UniversalPlaylist, options: SyncOptions): Promise<SyncResult> {
    const total = playlist.tracks.length * options.targetPlatforms.length;
    const perPlatform: Record<Platform, PlatformSyncProgress | undefined> = {
      spotify: undefined,
      'apple-music': undefined,
      'amazon-music': undefined,
      tidal: undefined,
      'youtube-music': undefined,
      deezer: undefined,
      pandora: undefined,
    };

    let matched = 0;
    let unavailable = 0;
    let manualReview = 0;
    let processed = 0;

    const emit = async (extraStatus?: SyncJobStatus) => {
      const snap: SyncProgressSnapshot = {
        matched,
        unavailable,
        manualReview,
        total,
        perPlatform: { ...perPlatform },
        progress: total === 0 ? 1 : processed / total,
      };
      if (extraStatus) {
        // surface in-flight status for callers that care
      }
      await this.opts.onProgress?.(snap);
    };

    for (const platform of options.targetPlatforms) {
      const binding = this.opts.services[platform];
      if (!binding) {
        perPlatform[platform] = { status: 'failed', matched: 0, unavailable: playlist.tracks.length, total: playlist.tracks.length };
        processed += playlist.tracks.length;
        await emit();
        continue;
      }

      perPlatform[platform] = { status: 'matching', matched: 0, unavailable: 0, total: playlist.tracks.length };
      await emit();

      const matches = await this.opts.matcher.matchMany(playlist.tracks, {
        platform,
        token: binding.token,
        provider: binding.service,
        manualOverride: this.opts.resolveManualOverride
          ? (track) => this.opts.resolveManualOverride!(platform, track)
          : undefined,
      });

      const writableIds: string[] = [];
      const platformAcc: PlatformSyncProgress = perPlatform[platform]!;
      for (const m of matches) {
        if (m.candidate) {
          const ext = m.candidate.platformIds[platform] ?? extractPlatformIdFromComposite(m.candidate.id, platform);
          if (ext) {
            writableIds.push(ext);
            matched++;
            platformAcc.matched++;
          } else {
            unavailable++;
            platformAcc.unavailable++;
          }
          if (m.needsReview && options.conflictStrategy === 'manual') manualReview++;
        } else if (options.skipUnavailable) {
          unavailable++;
          platformAcc.unavailable++;
        } else {
          manualReview++;
        }
        processed++;
        if (processed % 10 === 0) await emit();
      }

      const existing = await this.opts.resolveTargetPlaylist?.(platform, playlist);
      let targetId = existing;
      if (!targetId) {
        if (!options.createIfMissing) {
          platformAcc.status = 'failed';
          await emit();
          continue;
        }
        const created = await binding.service.createPlaylist(binding.token, {
          name: playlist.name,
          description: playlist.description,
          isPublic: playlist.isPublic,
        });
        targetId = created.externalId;
      }
      platformAcc.targetPlaylistId = targetId;
      platformAcc.status = 'writing';
      await emit();

      try {
        await this.applyConflictStrategy({
          binding,
          targetId,
          desiredTrackIds: writableIds,
          options,
          existingPlaylistFetcher: existing
            ? () => binding.service.getPlaylist(binding.token, existing)
            : undefined,
        });
        platformAcc.status = manualReview > 0 ? 'partial' : 'completed';
      } catch (err) {
        platformAcc.status = 'failed';
        throw err;
      } finally {
        await emit();
      }
    }

    return { matched, unavailable, manualReview, total, perPlatform };
  }

  private async applyConflictStrategy(args: {
    binding: PlatformBinding;
    targetId: string;
    desiredTrackIds: string[];
    options: SyncOptions;
    existingPlaylistFetcher?: () => Promise<UniversalPlaylist>;
  }): Promise<void> {
    const { binding, targetId, desiredTrackIds, options, existingPlaylistFetcher } = args;
    if (options.conflictStrategy === 'source-wins' || !existingPlaylistFetcher) {
      // Replace semantics: clear and re-add. We chunk via the service adapter.
      const existing = existingPlaylistFetcher ? await existingPlaylistFetcher() : null;
      if (existing && existing.tracks.length) {
        const ids = existing.tracks
          .map((t) => t.platformIds[binding.service.platform] ?? extractPlatformIdFromComposite(t.id, binding.service.platform))
          .filter((s): s is string => !!s);
        if (ids.length) await binding.service.removeTracks(binding.token, targetId, ids);
      }
      if (desiredTrackIds.length) await binding.service.addTracks(binding.token, targetId, desiredTrackIds);
      return;
    }

    // newest-wins / manual: only add what's missing on the target.
    const existing = await existingPlaylistFetcher();
    const existingIds = new Set(
      existing.tracks
        .map((t) => t.platformIds[binding.service.platform] ?? extractPlatformIdFromComposite(t.id, binding.service.platform))
        .filter((s): s is string => !!s),
    );
    const toAdd = desiredTrackIds.filter((id) => !existingIds.has(id));
    if (toAdd.length) await binding.service.addTracks(binding.token, targetId, toAdd);
  }
}

function extractPlatformIdFromComposite(compositeId: string, platform: Platform): string | null {
  const prefix = `${platform}:`;
  return compositeId.startsWith(prefix) ? compositeId.slice(prefix.length) : null;
}
