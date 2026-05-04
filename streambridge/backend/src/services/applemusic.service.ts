import { config } from '../utils/config';
import { createPlatformHttp } from '../utils/http';
import type { PlatformService } from './platform';
import type { AuthToken, UniversalPlaylist, UniversalTrack } from '@streambridge/types';
import { emptyLastSynced, emptyPlatformIds, stripPrefix } from './_shared';
import jwt from 'jsonwebtoken';

interface AmAttributes {
  name: string;
  artistName?: string;
  albumName?: string;
  durationInMillis?: number;
  isrc?: string;
  artwork?: { url: string };
  previews?: { url: string }[];
  contentRating?: 'explicit' | 'clean';
  description?: { standard?: string };
  isPublic?: boolean;
}
interface AmResource<T = AmAttributes> { id: string; type: string; attributes: T }
interface AmResponse<T> { data: T[]; next?: string }

const http = createPlatformHttp({
  platform: 'apple-music',
  baseURL: 'https://api.music.apple.com/v1',
});

const STOREFRONT = 'us';

function developerToken(): string {
  // Apple Music requires a JWT signed with the team's MusicKit private key.
  if (!config.APPLE_TEAM_ID || !config.APPLE_KEY_ID || !config.APPLE_PRIVATE_KEY) {
    throw new Error('Apple Music developer credentials missing');
  }
  return jwt.sign({}, config.APPLE_PRIVATE_KEY, {
    algorithm: 'ES256',
    expiresIn: '180d',
    issuer: config.APPLE_TEAM_ID,
    header: { alg: 'ES256', kid: config.APPLE_KEY_ID },
  });
}

const headers = (t: AuthToken) => ({
  Authorization: `Bearer ${developerToken()}`,
  'Music-User-Token': t.accessToken,
});

function toTrack(r: AmResource): UniversalTrack {
  const a = r.attributes;
  const isrc = a.isrc;
  const artwork = a.artwork?.url
    ? a.artwork.url.replace('{w}', '600').replace('{h}', '600')
    : '';
  return {
    id: `apple-music:${r.id}`,
    universalId: isrc ? `isrc:${isrc}` : `apple-music:${r.id}`,
    title: a.name,
    artist: a.artistName ? [a.artistName] : [],
    album: a.albumName ?? '',
    duration: a.durationInMillis ?? 0,
    isrc,
    platformIds: emptyPlatformIds({ 'apple-music': r.id }),
    artwork,
    previewUrl: a.previews?.[0]?.url,
    explicit: a.contentRating === 'explicit',
    availableOn: ['apple-music'],
  };
}

export const appleMusicService: PlatformService = {
  platform: 'apple-music',

  async listPlaylists(token, cursor) {
    const offset = cursor ? Number(cursor) : 0;
    const res = await http.get<AmResponse<AmResource>>('/me/library/playlists', {
      headers: headers(token),
      params: { limit: 100, offset },
    });
    const items: UniversalPlaylist[] = res.data.data.map((p) => ({
      id: `apple-music:${p.id}`,
      name: p.attributes.name,
      description: p.attributes.description?.standard,
      tracks: [],
      sourcePlatform: 'apple-music',
      syncedTo: [],
      lastSynced: emptyLastSynced(),
      artwork: p.attributes.artwork?.url,
      isPublic: p.attributes.isPublic ?? false,
      createdAt: new Date(),
      updatedAt: new Date(),
    }));
    return { items, nextCursor: res.data.next ? String(offset + 100) : null };
  },

  async getPlaylist(token, externalId) {
    const id = stripPrefix(externalId, 'apple-music:');
    const meta = await http.get<AmResponse<AmResource>>(`/me/library/playlists/${id}`, {
      headers: headers(token),
    });
    const head = meta.data.data[0];
    if (!head) throw new Error('playlist not found');
    const tracks: UniversalTrack[] = [];
    let offset = 0;
    while (true) {
      const page = await http.get<AmResponse<AmResource>>(`/me/library/playlists/${id}/tracks`, {
        headers: headers(token),
        params: { limit: 100, offset },
      });
      tracks.push(...page.data.data.map(toTrack));
      if (!page.data.next) break;
      offset += 100;
    }
    return {
      id: `apple-music:${head.id}`,
      name: head.attributes.name,
      description: head.attributes.description?.standard,
      tracks,
      sourcePlatform: 'apple-music',
      syncedTo: [],
      lastSynced: emptyLastSynced(),
      artwork: head.attributes.artwork?.url,
      isPublic: head.attributes.isPublic ?? false,
      createdAt: new Date(),
      updatedAt: new Date(),
    };
  },

  async searchTracks(token, query, limit = 20) {
    const res = await http.get<{ results: { songs?: { data: AmResource[] } } }>(
      `/catalog/${STOREFRONT}/search`,
      { headers: headers(token), params: { term: query, types: 'songs', limit } },
    );
    return res.data.results.songs?.data.map(toTrack) ?? [];
  },

  async getTrack(token, externalId) {
    const id = stripPrefix(externalId, 'apple-music:');
    const res = await http.get<AmResponse<AmResource>>(`/catalog/${STOREFRONT}/songs/${id}`, {
      headers: headers(token),
    });
    const t = res.data.data[0];
    return t ? toTrack(t) : null;
  },

  async findByIsrc(token, isrc) {
    const res = await http.get<AmResponse<AmResource>>(`/catalog/${STOREFRONT}/songs`, {
      headers: headers(token),
      params: { 'filter[isrc]': isrc, limit: 1 },
    });
    const t = res.data.data[0];
    return t ? toTrack(t) : null;
  },

  async createPlaylist(token, args) {
    const res = await http.post<AmResponse<AmResource>>(
      '/me/library/playlists',
      { attributes: { name: args.name, description: args.description, isPublic: args.isPublic ?? false } },
      { headers: headers(token) },
    );
    const created = res.data.data[0];
    if (!created) throw new Error('apple music: create returned no playlist');
    return { externalId: `apple-music:${created.id}` };
  },

  async addTracks(token, externalPlaylistId, externalTrackIds) {
    const id = stripPrefix(externalPlaylistId, 'apple-music:');
    const data = externalTrackIds.map((t) => ({ id: stripPrefix(t, 'apple-music:'), type: 'songs' }));
    for (let i = 0; i < data.length; i += 100) {
      await http.post(
        `/me/library/playlists/${id}/tracks`,
        { data: data.slice(i, i + 100) },
        { headers: headers(token) },
      );
    }
  },

  async removeTracks(token, externalPlaylistId, externalTrackIds) {
    const id = stripPrefix(externalPlaylistId, 'apple-music:');
    // Apple Music's library API removes by library item id; iterate one at a time.
    for (const t of externalTrackIds) {
      await http.delete(`/me/library/playlists/${id}/tracks/${stripPrefix(t, 'apple-music:')}`, {
        headers: headers(token),
      });
    }
  },
};
