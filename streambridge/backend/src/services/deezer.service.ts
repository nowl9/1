import { createPlatformHttp } from '../utils/http';
import type { PlatformService } from './platform';
import type { AuthToken, UniversalPlaylist, UniversalTrack } from '@streambridge/types';
import { emptyLastSynced, emptyPlatformIds, stripPrefix } from './_shared';

interface DzImage { picture_xl?: string; picture_big?: string; picture_medium?: string; picture?: string }
interface DzArtist { id: number; name: string }
interface DzAlbum { id: number; title: string; cover_xl?: string; cover_big?: string }
interface DzTrack {
  id: number;
  title: string;
  duration: number;
  isrc?: string;
  preview?: string;
  explicit_lyrics?: boolean;
  artist: DzArtist;
  album: DzAlbum;
  contributors?: DzArtist[];
}
interface DzPlaylist {
  id: number;
  title: string;
  description?: string;
  picture_xl?: string;
  picture_big?: string;
  is_loved_track?: boolean;
  public?: boolean;
}
interface DzList<T> { data: T[]; next?: string; total?: number }

const http = createPlatformHttp({ platform: 'deezer', baseURL: 'https://api.deezer.com' });

const params = (t: AuthToken, extra: Record<string, string | number | undefined> = {}) => ({
  access_token: t.accessToken,
  ...extra,
});

function toTrack(t: DzTrack): UniversalTrack {
  const artists = (t.contributors?.length ? t.contributors : [t.artist]).map((a) => a.name);
  return {
    id: `deezer:${t.id}`,
    universalId: t.isrc ? `isrc:${t.isrc}` : `deezer:${t.id}`,
    title: t.title,
    artist: artists,
    album: t.album.title,
    duration: t.duration * 1000,
    isrc: t.isrc,
    platformIds: emptyPlatformIds({ deezer: String(t.id) }),
    artwork: t.album.cover_xl ?? t.album.cover_big ?? '',
    previewUrl: t.preview,
    explicit: t.explicit_lyrics ?? false,
    availableOn: ['deezer'],
  };
}

export const deezerService: PlatformService = {
  platform: 'deezer',

  async listPlaylists(token, cursor) {
    const index = cursor ? Number(cursor) : 0;
    const res = await http.get<DzList<DzPlaylist>>('/user/me/playlists', {
      params: params(token, { index, limit: 50 }),
    });
    return {
      items: res.data.data.map((p) => ({
        id: `deezer:${p.id}`,
        name: p.title,
        description: p.description,
        tracks: [],
        sourcePlatform: 'deezer',
        syncedTo: [],
        lastSynced: emptyLastSynced(),
        artwork: p.picture_xl ?? p.picture_big,
        isPublic: p.public ?? false,
        createdAt: new Date(),
        updatedAt: new Date(),
      })),
      nextCursor: res.data.next ? String(index + 50) : null,
      total: res.data.total,
    };
  },

  async getPlaylist(token, externalId) {
    const id = stripPrefix(externalId, 'deezer:');
    const meta = await http.get<DzPlaylist>(`/playlist/${id}`, { params: params(token) });
    const tracks: UniversalTrack[] = [];
    let index = 0;
    while (true) {
      const page = await http.get<DzList<DzTrack>>(`/playlist/${id}/tracks`, {
        params: params(token, { index, limit: 100 }),
      });
      tracks.push(...page.data.data.map(toTrack));
      if (!page.data.next) break;
      index += 100;
    }
    return {
      id: `deezer:${meta.data.id}`,
      name: meta.data.title,
      description: meta.data.description,
      tracks,
      sourcePlatform: 'deezer',
      syncedTo: [],
      lastSynced: emptyLastSynced(),
      artwork: meta.data.picture_xl ?? meta.data.picture_big,
      isPublic: meta.data.public ?? false,
      createdAt: new Date(),
      updatedAt: new Date(),
    };
  },

  async searchTracks(token, query, limit = 20) {
    const res = await http.get<DzList<DzTrack>>('/search/track', {
      params: params(token, { q: query, limit }),
    });
    return res.data.data.map(toTrack);
  },

  async getTrack(token, externalId) {
    const id = stripPrefix(externalId, 'deezer:');
    const res = await http.get<DzTrack>(`/track/${id}`, { params: params(token) });
    return res.data ? toTrack(res.data) : null;
  },

  async findByIsrc(token, isrc) {
    const res = await http.get<DzTrack>(`/track/isrc:${isrc}`, { params: params(token) });
    return res.data?.id ? toTrack(res.data) : null;
  },

  async createPlaylist(token, args) {
    const res = await http.post<{ id: number }>(
      '/user/me/playlists',
      undefined,
      { params: params(token, { title: args.name }) },
    );
    if (args.description) {
      await http.post(`/playlist/${res.data.id}`, undefined, {
        params: params(token, { description: args.description }),
      });
    }
    return { externalId: `deezer:${res.data.id}` };
  },

  async addTracks(token, externalPlaylistId, externalTrackIds) {
    const id = stripPrefix(externalPlaylistId, 'deezer:');
    for (let i = 0; i < externalTrackIds.length; i += 50) {
      const songs = externalTrackIds.slice(i, i + 50).map((t) => stripPrefix(t, 'deezer:')).join(',');
      await http.post(`/playlist/${id}/tracks`, undefined, { params: params(token, { songs }) });
    }
  },

  async removeTracks(token, externalPlaylistId, externalTrackIds) {
    const id = stripPrefix(externalPlaylistId, 'deezer:');
    const songs = externalTrackIds.map((t) => stripPrefix(t, 'deezer:')).join(',');
    await http.delete(`/playlist/${id}/tracks`, { params: params(token, { songs }) });
  },
};
