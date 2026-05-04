import { TrackMatcher } from '../TrackMatcher';
import type { AuthToken, UniversalTrack } from '@streambridge/types';

const token: AuthToken = {
  platform: 'spotify',
  accessToken: 't',
  expiresAt: Date.now() + 3600_000,
  scopes: [],
  tokenType: 'Bearer',
};

function track(over: Partial<UniversalTrack>): UniversalTrack {
  return {
    id: 'src:1',
    universalId: 'u:1',
    title: 'Bohemian Rhapsody',
    artist: ['Queen'],
    album: 'A Night at the Opera',
    duration: 354_000,
    artwork: '',
    explicit: false,
    availableOn: ['spotify'],
    platformIds: {
      spotify: '1',
      'apple-music': null,
      'amazon-music': null,
      tidal: null,
      'youtube-music': null,
      deezer: null,
      pandora: null,
    },
    ...over,
  };
}

describe('TrackMatcher', () => {
  it('returns ISRC match with confidence 1', async () => {
    const m = new TrackMatcher();
    const source = track({ isrc: 'GBUM71029604' });
    const target = track({ id: 'apple:1', isrc: 'GBUM71029604' });
    const result = await m.match(source, {
      platform: 'apple-music',
      token,
      provider: {
        findByIsrc: jest.fn().mockResolvedValue(target),
        searchTracks: jest.fn(),
      },
    });
    expect(result.strategy).toBe('isrc');
    expect(result.confidence).toBe(1);
    expect(result.candidate).toBe(target);
    expect(result.needsReview).toBe(false);
  });

  it('falls back to fuzzy and scores high on a clean metadata match', async () => {
    const m = new TrackMatcher();
    const source = track({});
    const candidate = track({
      id: 'apple:42',
      title: 'Bohemian Rhapsody (Remastered 2011)',
      duration: 355_000,
    });
    const result = await m.match(source, {
      platform: 'apple-music',
      token,
      provider: {
        findByIsrc: jest.fn().mockResolvedValue(null),
        searchTracks: jest.fn().mockResolvedValue([candidate]),
      },
    });
    expect(result.strategy).toBe('fuzzy-metadata');
    expect(result.confidence).toBeGreaterThan(0.9);
    expect(result.candidate).toBe(candidate);
  });

  it('flags low-confidence matches for manual review', async () => {
    const m = new TrackMatcher();
    const source = track({ title: 'Imagine', artist: ['John Lennon'], duration: 183_000 });
    const candidate = track({ id: 'apple:99', title: 'Something Different', artist: ['Artist'], duration: 200_000 });
    const result = await m.match(source, {
      platform: 'apple-music',
      token,
      provider: {
        findByIsrc: jest.fn().mockResolvedValue(null),
        searchTracks: jest.fn().mockResolvedValue([candidate]),
      },
    });
    expect(result.confidence).toBeLessThan(0.9);
    expect(result.needsReview).toBe(true);
  });

  it('honors manual override before any API lookup', async () => {
    const m = new TrackMatcher();
    const source = track({ isrc: 'GBUM71029604' });
    const override = track({ id: 'apple:override' });
    const findByIsrc = jest.fn();
    const searchTracks = jest.fn();
    const result = await m.match(source, {
      platform: 'apple-music',
      token,
      provider: { findByIsrc, searchTracks },
      manualOverride: jest.fn().mockResolvedValue(override),
    });
    expect(result.strategy).toBe('manual');
    expect(result.candidate).toBe(override);
    expect(findByIsrc).not.toHaveBeenCalled();
    expect(searchTracks).not.toHaveBeenCalled();
  });
});
