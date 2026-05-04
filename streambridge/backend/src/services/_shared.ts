import type { Platform, UniversalPlaylist, UniversalTrack } from '@streambridge/types';

export function emptyPlatformIds(set: Partial<Record<Platform, string | null>>): UniversalTrack['platformIds'] {
  return {
    spotify: null,
    'apple-music': null,
    'amazon-music': null,
    tidal: null,
    'youtube-music': null,
    deezer: null,
    pandora: null,
    ...set,
  };
}

export function emptyLastSynced(): UniversalPlaylist['lastSynced'] {
  return {
    spotify: null,
    'apple-music': null,
    'amazon-music': null,
    tidal: null,
    'youtube-music': null,
    deezer: null,
    pandora: null,
  };
}

export function stripPrefix(s: string, prefix: string): string {
  return s.startsWith(prefix) ? s.slice(prefix.length) : s;
}

export function withPrefix(prefix: Platform, id: string): string {
  return `${prefix}:${id}`;
}
