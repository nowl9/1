import { createPlatformHttp } from '../utils/http';
import type { PlatformService } from './platform';
import type { AuthToken, UniversalPlaylist, UniversalTrack } from '@streambridge/types';
import { emptyLastSynced, emptyPlatformIds, stripPrefix } from './_shared';

/**
 * Amazon Music exposes a partner API via developer.amazon.com. Surface here
 * mirrors the documented Catalog + Library Music endpoints. Endpoint paths
 * are the publicly documented ones; if Amazon flips the path, change it in
 * one place — every adapter funnels through `http`.
 */

interface AmazonImage { url: string }
interface AmazonTrack {
  asin: string;
  trackId: string;
  title: string;
  artists: { name: string }[];
  album: { title: string };
  durationMs: number;
  isrc?: string;
  images?: AmazonImage[];
  parentalAdvisory?: 'EXPLICIT' | 'CLEAN';
  previewUrl?: string;
}
interface AmazonPlaylist {
  id: string;
  name: string;
  description?: string;
  imageUrl?: string;
  isPublic?: boolean;
}

const http = createPlatformHttp({
  platform: 'amazon-music',
  baseURL: 'https://api.music.amazon.dev/v1',
});

const auth = (t: AuthToken) => ({ Authorization: `Bearer ${t.accessToken}` });

function toTrack(t: AmazonTrack): UniversalTrack {
  return {
    id: `amazon-music:${t.trackId}`,
    universalId: t.isrc ? `isrc:${t.isrc}` : `amazon-music:${t.trackId}`,
    title: t.title,
    artist: t.artists.map((a) => a.name),
    album: t.album.title,
    duration: t.durationMs,
    isrc: t.isrc,
    platformIds: emptyPlatformIds({ 'amazon-music': t.trackId }),
    artwork: t.images?.[0]?.url ?? '',
    previewUrl: t.previewUrl,
    explicit: t.parentalAdvisory === 'EXPLICIT',
    availableOn: ['amazon-music'],
  };
}

export const amazonMusicService: PlatformService = {
  platform: 'amazon-music',

  async listPlaylists(token, cursor) {
    const res = await http.get<{ playlists: AmazonPlaylist[]; nextToken?: string }>(
      '/me/playlists',
      { headers: auth(token), params: { pageSize: 50, nextToken: cursor } },
    );
    return {
      items: res.data.playlists.map((p) => ({
        id: `amazon-music:${p.id}`,
        name: p.name,
        description: p.description,
        tracks: [],
        sourcePlatform: 'amazon-music',
        syncedTo: [],
        lastSynced: emptyLastSynced(),
        artwork: p.imageUrl,
        isPublic: p.isPublic ?? false,
        createdAt: new Date(),
        updatedAt: new Date(),
      })),
      nextCursor: res.data.nextToken ?? null,
    };
  },

  async getPlaylist(token, externalId) {
    const id = stripPrefix(externalId, 'amazon-music:');
    const meta = await http.get<AmazonPlaylist>(`/playlists/${id}`, { headers: auth(token) });
    const tracks: UniversalTrack[] = [];
    let nextToken: string | undefined;
    do {
      const page = await http.get<{ tracks: AmazonTrack[]; nextToken?: string }>(
        `/playlists/${id}/tracks`,
        { headers: auth(token), params: { pageSize: 100, nextToken } },
      );
      tracks.push(...page.data.tracks.map(toTrack));
      nextToken = page.data.nextToken;
    } while (nextToken);
    return {
      id: `amazon-music:${meta.data.id}`,
      name: meta.data.name,
      description: meta.data.description,
      tracks,
      sourcePlatform: 'amazon-music',
      syncedTo: [],
      lastSynced: emptyLastSynced(),
      artwork: meta.data.imageUrl,
      isPublic: meta.data.isPublic ?? false,
      createdAt: new Date(),
      updatedAt: new Date(),
    };
  },

  async searchTracks(token, query, limit = 20) {
    const res = await http.get<{ tracks: AmazonTrack[] }>('/catalog/search/tracks', {
      headers: auth(token),
      params: { q: query, limit },
    });
    return res.data.tracks.map(toTrack);
  },

  async getTrack(token, externalId) {
    const id = stripPrefix(externalId, 'amazon-music:');
    const res = await http.get<AmazonTrack>(`/catalog/tracks/${id}`, { headers: auth(token) });
    return res.data ? toTrack(res.data) : null;
  },

  async findByIsrc(token, isrc) {
    const res = await http.get<{ tracks: AmazonTrack[] }>('/catalog/tracks', {
      headers: auth(token),
      params: { isrc, limit: 1 },
    });
    const t = res.data.tracks[0];
    return t ? toTrack(t) : null;
  },

  async createPlaylist(token, args) {
    const res = await http.post<{ id: string }>(
      '/me/playlists',
      { name: args.name, description: args.description, isPublic: args.isPublic ?? false },
      { headers: auth(token) },
    );
    return { externalId: `amazon-music:${res.data.id}` };
  },

  async addTracks(token, externalPlaylistId, externalTrackIds) {
    const id = stripPrefix(externalPlaylistId, 'amazon-music:');
    for (let i = 0; i < externalTrackIds.length; i += 100) {
      await http.post(
        `/playlists/${id}/tracks`,
        { trackIds: externalTrackIds.slice(i, i + 100).map((t) => stripPrefix(t, 'amazon-music:')) },
        { headers: auth(token) },
      );
    }
  },

  async removeTracks(token, externalPlaylistId, externalTrackIds) {
    const id = stripPrefix(externalPlaylistId, 'amazon-music:');
    await http.delete(`/playlists/${id}/tracks`, {
      headers: auth(token),
      data: { trackIds: externalTrackIds.map((t) => stripPrefix(t, 'amazon-music:')) },
    });
  },
};
