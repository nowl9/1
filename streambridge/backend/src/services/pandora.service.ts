import { createPlatformHttp } from '../utils/http';
import type { PlatformService } from './platform';
import type { AuthToken, UniversalPlaylist, UniversalTrack } from '@streambridge/types';
import { emptyLastSynced, emptyPlatformIds, stripPrefix } from './_shared';

/**
 * Pandora's Partner API uses opaque pandoraId tokens (e.g. "TR:..." for tracks,
 * "PL:..." for playlists). Catalog and Collections endpoints are documented as
 * `/v7/playlists/...` style routes. ISRC matching is supported via catalog
 * lookup; for unmatched cases the engine falls back to fuzzy matching.
 */

interface PdTrack {
  pandoraId: string; // "TR:1234"
  name: string;
  artistName: string;
  albumName: string;
  duration: number; // seconds
  isrc?: string;
  icon?: { artUrl?: string };
  explicitness?: 'EXPLICIT' | 'CLEAN' | 'UNKNOWN';
}
interface PdPlaylist {
  pandoraId: string; // "PL:1234"
  name: string;
  description?: string;
  isPrivate?: boolean;
  thumbnail?: { artUrl?: string };
}

const http = createPlatformHttp({
  platform: 'pandora',
  baseURL: 'https://www.pandora.com/api',
  defaultHeaders: { 'Content-Type': 'application/json' },
});

const headers = (t: AuthToken) => ({ 'X-AuthToken': t.accessToken });

function toTrack(t: PdTrack): UniversalTrack {
  return {
    id: `pandora:${t.pandoraId}`,
    universalId: t.isrc ? `isrc:${t.isrc}` : `pandora:${t.pandoraId}`,
    title: t.name,
    artist: [t.artistName],
    album: t.albumName,
    duration: (t.duration ?? 0) * 1000,
    isrc: t.isrc,
    platformIds: emptyPlatformIds({ pandora: t.pandoraId }),
    artwork: t.icon?.artUrl ?? '',
    explicit: t.explicitness === 'EXPLICIT',
    availableOn: ['pandora'],
  };
}

export const pandoraService: PlatformService = {
  platform: 'pandora',

  async listPlaylists(token, cursor) {
    const offset = cursor ? Number(cursor) : 0;
    const res = await http.post<{ items: PdPlaylist[]; totalCount?: number }>(
      '/v7/collections/getSortedByTypes.json',
      { request: { types: ['PL'], limit: 50, offset, sortOrder: 'MOST_RECENT_MODIFIED' } },
      { headers: headers(token) },
    );
    return {
      items: res.data.items.map((p) => ({
        id: `pandora:${p.pandoraId}`,
        name: p.name,
        description: p.description,
        tracks: [],
        sourcePlatform: 'pandora',
        syncedTo: [],
        lastSynced: emptyLastSynced(),
        artwork: p.thumbnail?.artUrl,
        isPublic: !(p.isPrivate ?? true),
        createdAt: new Date(),
        updatedAt: new Date(),
      })),
      nextCursor: res.data.items.length === 50 ? String(offset + 50) : null,
      total: res.data.totalCount,
    };
  },

  async getPlaylist(token, externalId) {
    const id = stripPrefix(externalId, 'pandora:');
    const res = await http.post<{ playlist: PdPlaylist; tracks: PdTrack[] }>(
      '/v7/playlists/get.json',
      { request: { pandoraId: id, includeTracks: true } },
      { headers: headers(token) },
    );
    return {
      id: `pandora:${res.data.playlist.pandoraId}`,
      name: res.data.playlist.name,
      description: res.data.playlist.description,
      tracks: res.data.tracks.map(toTrack),
      sourcePlatform: 'pandora',
      syncedTo: [],
      lastSynced: emptyLastSynced(),
      artwork: res.data.playlist.thumbnail?.artUrl,
      isPublic: !(res.data.playlist.isPrivate ?? true),
      createdAt: new Date(),
      updatedAt: new Date(),
    };
  },

  async searchTracks(token, query, limit = 20) {
    const res = await http.post<{ items: PdTrack[] }>(
      '/v7/search/search.json',
      { request: { query, types: ['TR'], count: limit } },
      { headers: headers(token) },
    );
    return res.data.items.map(toTrack);
  },

  async getTrack(token, externalId) {
    const id = stripPrefix(externalId, 'pandora:');
    const res = await http.post<{ tracks: PdTrack[] }>(
      '/v7/catalog/getDetails.json',
      { request: { pandoraIds: [id] } },
      { headers: headers(token) },
    );
    const t = res.data.tracks[0];
    return t ? toTrack(t) : null;
  },

  async findByIsrc(token, isrc) {
    const res = await http.post<{ tracks: PdTrack[] }>(
      '/v7/catalog/lookupByIsrc.json',
      { request: { isrc } },
      { headers: headers(token) },
    );
    const t = res.data.tracks[0];
    return t ? toTrack(t) : null;
  },

  async createPlaylist(token, args) {
    const res = await http.post<{ playlist: { pandoraId: string } }>(
      '/v7/playlists/create.json',
      {
        request: {
          name: args.name,
          description: args.description,
          isPrivate: !args.isPublic,
        },
      },
      { headers: headers(token) },
    );
    return { externalId: `pandora:${res.data.playlist.pandoraId}` };
  },

  async addTracks(token, externalPlaylistId, externalTrackIds) {
    const id = stripPrefix(externalPlaylistId, 'pandora:');
    await http.post(
      '/v7/playlists/appendItems.json',
      {
        request: {
          pandoraId: id,
          itemPandoraIds: externalTrackIds.map((t) => stripPrefix(t, 'pandora:')),
        },
      },
      { headers: headers(token) },
    );
  },

  async removeTracks(token, externalPlaylistId, externalTrackIds) {
    const id = stripPrefix(externalPlaylistId, 'pandora:');
    await http.post(
      '/v7/playlists/removeItems.json',
      {
        request: {
          pandoraId: id,
          itemPandoraIds: externalTrackIds.map((t) => stripPrefix(t, 'pandora:')),
        },
      },
      { headers: headers(token) },
    );
  },
};
