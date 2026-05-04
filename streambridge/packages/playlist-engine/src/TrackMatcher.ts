import Fuse from 'fuse.js';
import type {
  AuthToken,
  MatchStrategy,
  Platform,
  TrackMatch,
  UniversalTrack,
} from '@streambridge/types';
import { normalizeArtist, normalizeTitle, similarity, jaccard } from './textUtil';

export interface MatchProvider {
  /** Look up a track on the target platform by ISRC. */
  findByIsrc(token: AuthToken, isrc: string): Promise<UniversalTrack | null>;
  /** Search tracks on the target platform; engine will pick the best candidate. */
  searchTracks(token: AuthToken, query: string, limit?: number): Promise<UniversalTrack[]>;
  /**
   * Optional audio-feature fetch (Spotify only in the standard set). Used as a
   * tiebreaker when fuzzy metadata matching is ambiguous.
   */
  getAudioFeatures?(token: AuthToken, externalId: string): Promise<AudioFeatures | null>;
}

export interface AudioFeatures {
  tempo: number;       // BPM
  key: number;         // 0..11
  energy: number;      // 0..1
  danceability: number;
  valence: number;
}

export interface MatchContext {
  platform: Platform;
  token: AuthToken;
  provider: MatchProvider;
  /**
   * Optional resolver for a previously-recorded manual match. Returning a
   * non-null UniversalTrack short-circuits everything else.
   */
  manualOverride?: (sourceTrack: UniversalTrack) => Promise<UniversalTrack | null>;
}

interface ScoredCandidate {
  track: UniversalTrack;
  score: number;
}

const HIGH_CONFIDENCE = 0.9;
const REVIEW_THRESHOLD = 0.65;
const DURATION_TOLERANCE_MS = 5_000;

export class TrackMatcher {
  /**
   * Run the full matching pipeline for one source track against one target
   * platform. Returns a TrackMatch even when no candidate is found so callers
   * can decide whether to surface for manual review.
   */
  async match(source: UniversalTrack, ctx: MatchContext): Promise<TrackMatch> {
    if (ctx.manualOverride) {
      const override = await ctx.manualOverride(source);
      if (override) return this.result(source, override, 'manual', 1);
    }

    if (source.isrc) {
      const direct = await ctx.provider.findByIsrc(ctx.token, source.isrc);
      if (direct) return this.result(source, direct, 'isrc', 1);
    }

    const candidates = await this.fuzzySearch(source, ctx);
    const best = candidates[0];
    if (best && best.score >= HIGH_CONFIDENCE) {
      return this.result(source, best.track, 'fuzzy-metadata', best.score);
    }

    if (best && best.score >= REVIEW_THRESHOLD && ctx.provider.getAudioFeatures && source.platformIds.spotify) {
      const tieBroken = await this.audioFeatureTiebreak(source, candidates.slice(0, 5), ctx);
      if (tieBroken) return this.result(source, tieBroken.track, 'audio-features', tieBroken.score);
    }

    if (best) return this.result(source, best.track, 'fuzzy-metadata', best.score);
    return this.result(source, null, 'fuzzy-metadata', 0);
  }

  /** Convenience wrapper that runs match() across many tracks with bounded concurrency. */
  async matchMany(
    sources: UniversalTrack[],
    ctx: MatchContext,
    concurrency = 4,
  ): Promise<TrackMatch[]> {
    const out: TrackMatch[] = new Array(sources.length);
    let cursor = 0;
    const workers = Array.from({ length: Math.min(concurrency, sources.length) }, async () => {
      while (true) {
        const i = cursor++;
        if (i >= sources.length) return;
        out[i] = await this.match(sources[i]!, ctx);
      }
    });
    await Promise.all(workers);
    return out;
  }

  private async fuzzySearch(source: UniversalTrack, ctx: MatchContext): Promise<ScoredCandidate[]> {
    const titleNorm = normalizeTitle(source.title);
    const primaryArtist = source.artist[0] ?? '';
    const query = `${titleNorm} ${primaryArtist}`.trim();
    const raw = await ctx.provider.searchTracks(ctx.token, query, 15);
    if (!raw.length) return [];

    // First pass: weighted Fuse.js to prune obvious non-matches quickly.
    const fuse = new Fuse(raw, {
      includeScore: true,
      threshold: 0.6,
      keys: [
        { name: 'title', weight: 0.5 },
        { name: 'artist', weight: 0.4 },
        { name: 'album', weight: 0.1 },
      ],
    });
    const fuseHits = fuse.search(query);

    // Second pass: hand-rolled composite score so duration and explicit-bit
    // factor in the way humans actually compare two recordings.
    const scored: ScoredCandidate[] = (fuseHits.length ? fuseHits.map((h) => h.item) : raw).map(
      (cand) => ({ track: cand, score: this.composite(source, cand) }),
    );
    scored.sort((a, b) => b.score - a.score);
    return scored;
  }

  private composite(source: UniversalTrack, cand: UniversalTrack): number {
    const titleSim = similarity(normalizeTitle(source.title), normalizeTitle(cand.title));
    const sourceArtists = source.artist.map(normalizeArtist);
    const candArtists = cand.artist.map(normalizeArtist);
    const artistSim = Math.max(
      jaccard(
        sourceArtists.flatMap((a) => a.split(' ')),
        candArtists.flatMap((a) => a.split(' ')),
      ),
      candArtists.length && sourceArtists.length
        ? similarity(sourceArtists[0]!, candArtists[0]!)
        : 0,
    );
    const albumSim = source.album && cand.album
      ? similarity(normalizeTitle(source.album), normalizeTitle(cand.album))
      : 0.5;
    const durationDelta = Math.abs(source.duration - cand.duration);
    const durationScore = durationDelta <= DURATION_TOLERANCE_MS
      ? 1
      : Math.max(0, 1 - (durationDelta - DURATION_TOLERANCE_MS) / 60_000);
    const explicitMatch = source.explicit === cand.explicit ? 1 : 0.85;

    // Weights chosen empirically: title and artist dominate, duration acts as a
    // sanity check, album breaks ties, explicit nudges away from radio edits.
    return (
      titleSim * 0.45 +
      artistSim * 0.35 +
      durationScore * 0.1 +
      albumSim * 0.05 +
      explicitMatch * 0.05
    );
  }

  private async audioFeatureTiebreak(
    source: UniversalTrack,
    candidates: ScoredCandidate[],
    ctx: MatchContext,
  ): Promise<ScoredCandidate | null> {
    if (!ctx.provider.getAudioFeatures || !source.platformIds.spotify) return null;
    const sourceFeat = await ctx.provider.getAudioFeatures(ctx.token, source.platformIds.spotify);
    if (!sourceFeat) return null;
    let best: ScoredCandidate | null = null;
    let bestScore = -Infinity;
    for (const c of candidates) {
      const candId = c.track.platformIds.spotify;
      if (!candId) continue;
      const feat = await ctx.provider.getAudioFeatures(ctx.token, candId);
      if (!feat) continue;
      const featScore = audioFeatureSimilarity(sourceFeat, feat);
      const combined = c.score * 0.7 + featScore * 0.3;
      if (combined > bestScore) {
        bestScore = combined;
        best = { track: c.track, score: combined };
      }
    }
    return best;
  }

  private result(
    sourceTrack: UniversalTrack,
    candidate: UniversalTrack | null,
    strategy: MatchStrategy,
    confidence: number,
  ): TrackMatch {
    return {
      sourceTrack,
      candidate,
      strategy,
      confidence: Math.max(0, Math.min(1, confidence)),
      needsReview: !candidate || confidence < HIGH_CONFIDENCE,
    };
  }
}

function audioFeatureSimilarity(a: AudioFeatures, b: AudioFeatures): number {
  const tempoDiff = Math.abs(a.tempo - b.tempo);
  const tempoScore = tempoDiff <= 2 ? 1 : Math.max(0, 1 - (tempoDiff - 2) / 30);
  const keyScore = a.key === b.key ? 1 : 0.7;
  const energyScore = 1 - Math.abs(a.energy - b.energy);
  const danceScore = 1 - Math.abs(a.danceability - b.danceability);
  const valenceScore = 1 - Math.abs(a.valence - b.valence);
  return tempoScore * 0.4 + keyScore * 0.2 + energyScore * 0.15 + danceScore * 0.15 + valenceScore * 0.1;
}
