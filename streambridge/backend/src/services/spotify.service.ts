import { createPlatformHttp } from '../utils/http';
import type { PlatformService } from './platform';
import type {
  AuthToken,
  PaginatedResponse,
  UniversalPlaylist,
  UniversalTrack,
} from '@streambridge/types';

interface SpotifyImage { url: string; height?: number; width?: number }
interface SpotifyArtist { id: string; name: string }
interface SpotifyAlbum { id: string; name: string; images: SpotifyImage[] }
interface SpotifyTrack {
  id: string;
  name: string;
  artists: SpotifyArtist[];
  album: SpotifyAlbum;
  duration_ms: number;
  explicit: boolean;
  preview_url: string | null;
  external_ids?: { isrc?: string };
  available_markets?: string[];
}
interface SpotifyPlaylistItem { track: SpotifyTrack | null }
interface SpotifyPlaylist {
  id: string;
  name: string;
  description: string;
  public: boolean;
  images: SpotifyImage[];
  tracks: { items?: SpotifyPlaylistItem[]; total: number; href: string };
}

const http = createPlatformHttp({ platform: 'spotify', baseURL: 'https://api.spotify.com/v1' });

const auth = (t: AuthToken) => ({ Authorization: `Bearer ${t.accessToken}` });

function toUniversalTrack(t: SpotifyTrack): UniversalTrack {
  const universalId = t.external_ids?.isrc ? `isrc:${t.external_ids.isrc}` : `spotify:${t.id}`;
  return {
    id: `spotify:${t.id}`,
    universalId,
    title: t.name,
    artist: t.artists.map((a) => a.name),
    album: t.album.name,
    duration: t.duration_ms,
    isrc: t.external_ids?.isrc,
    platformIds: {
      spotify: t.id,
      'apple-music': null,
      'amazon-music': null,
      tidal: null,
      'youtube-music': null,
      deezer: null,
      pandora: null,
    },
    artwork: t.album.images[0]?.url ?? '',
    previewUrl: t.preview_url ?? undefined,
    explicit: t.explicit,
    availableOn: ['spotify'],
  };
}

export const spotifyService: PlatformService = {
  platform: 'spotify',

  async listPlaylists(token, cursor) {
    const offset = cursor ? Number(cursor) : 0;
    const res = await http.get<{ items: SpotifyPlaylist[]; next: string | null; total: number }>(
      '/me/playlists',
      { headers: auth(token), params: { limit: 50, offset } },
    );
    const items: UniversalPlaylist[] = res.data.items.map((p) => ({
      id: `spotify:${p.id}`,
      name: p.name,
      description: p.description || undefined,
      tracks: [],
      sourcePlatform: 'spotify',
      syncedTo: [],
      lastSynced: emptyLastSynced(),
      artwork: p.images[0]?.url,
      isPublic: p.public,
      createdAt: new Date(),
      updatedAt: new Date(),
    }));
    return { items, nextCursor: res.data.next ? String(offset + 50) : null, total: res.data.total };
  },

  async getPlaylist(token, externalId) {
    const id = stripPrefix(externalId, 'spotify:');
    const res = await http.get<SpotifyPlaylist>(`/playlists/${id}`, { headers: auth(token) });
    const tracks: UniversalTrack[] = [];
    let next = `/playlists/${id}/tracks?limit=100&offset=0`;
    while (next) {
      const page = await http.get<{ items: SpotifyPlaylistItem[]; next: string | null }>(next, {
        headers: auth(token),
      });
      for (const item of page.data.items) if (item.track) tracks.push(toUniversalTrack(item.track));
      next = page.data.next ? page.data.next.replace('https://api.spotify.com/v1', '') : '';
    }
    return {
      id: `spotify:${res.data.id}`,
      name: res.data.name,
      description: res.data.description || undefined,
      tracks,
      sourcePlatform: 'spotify',
      syncedTo: [],
      lastSynced: emptyLastSynced(),
      artwork: res.data.images[0]?.url,
      isPublic: res.data.public,
      createdAt: new Date(),
      updatedAt: new Date(),
    };
  },

  async searchTracks(token, query, limit = 20) {
    const res = await http.get<{ tracks: { items: SpotifyTrack[] } }>('/search', {
      headers: auth(token),
      params: { q: query, type: 'track', limit },
    });
    return res.data.tracks.items.map(toUniversalTrack);
  },

  async getTrack(token, externalId) {
    const id = stripPrefix(externalId, 'spotify:');
    const res = await http.get<SpotifyTrack>(`/tracks/${id}`, { headers: auth(token) });
    return toUniversalTrack(res.data);
  },

  async findByIsrc(token, isrc) {
    const res = await http.get<{ tracks: { items: SpotifyTrack[] } }>('/search', {
      headers: auth(token),
      params: { q: `isrc:${isrc}`, type: 'track', limit: 1 },
    });
    const t = res.data.tracks.items[0];
    return t ? toUniversalTrack(t) : null;
  },

  async createPlaylist(token, args) {
    const me = await http.get<{ id: string }>('/me', { headers: auth(token) });
    const res = await http.post<{ id: string }>(
      `/users/${me.data.id}/playlists`,
      { name: args.name, description: args.description ?? '', public: args.isPublic ?? false },
      { headers: auth(token) },
    );
    return { externalId: `spotify:${res.data.id}` };
  },

  async addTracks(token, externalPlaylistId, externalTrackIds) {
    const id = stripPrefix(externalPlaylistId, 'spotify:');
    // Spotify caps add operations at 100 URIs per request.
    for (let i = 0; i < externalTrackIds.length; i += 100) {
      const batch = externalTrackIds.slice(i, i + 100).map((t) => `spotify:track:${stripPrefix(t, 'spotify:')}`);
      await http.post(`/playlists/${id}/tracks`, { uris: batch }, { headers: auth(token) });
    }
  },

  async removeTracks(token, externalPlaylistId, externalTrackIds) {
    const id = stripPrefix(externalPlaylistId, 'spotify:');
    for (let i = 0; i < externalTrackIds.length; i += 100) {
      const batch = externalTrackIds
        .slice(i, i + 100)
        .map((t) => ({ uri: `spotify:track:${stripPrefix(t, 'spotify:')}` }));
      await http.delete(`/playlists/${id}/tracks`, { headers: auth(token), data: { tracks: batch } });
    }
  },
};

function stripPrefix(s: string, prefix: string) {
  return s.startsWith(prefix) ? s.slice(prefix.length) : s;
}

function emptyLastSynced(): UniversalPlaylist['lastSynced'] {
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
