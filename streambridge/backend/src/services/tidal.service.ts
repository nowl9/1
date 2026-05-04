import { createPlatformHttp } from '../utils/http';
import type { PlatformService } from './platform';
import type { AuthToken, UniversalPlaylist, UniversalTrack } from '@streambridge/types';
import { emptyLastSynced, emptyPlatformIds, stripPrefix } from './_shared';

interface TidalImage { href: string }
interface TidalArtist { attributes: { name: string } }
interface TidalAlbum { attributes: { title: string } }
interface TidalTrackAttrs {
  title: string;
  durationMs?: number;
  isrc?: string;
  explicit?: boolean;
  popularity?: number;
}
interface TidalResource<A = TidalTrackAttrs> {
  id: string;
  type: string;
  attributes: A;
  relationships?: {
    artists?: { data: { id: string }[] };
    album?: { data: { id: string } };
  };
}
interface TidalListResp<A = TidalTrackAttrs> {
  data: TidalResource<A>[];
  included?: TidalResource[];
  links?: { next?: string };
}

const http = createPlatformHttp({
  platform: 'tidal',
  baseURL: 'https://openapi.tidal.com/v2',
  defaultHeaders: { Accept: 'application/vnd.api+json' },
});

const auth = (t: AuthToken) => ({ Authorization: `Bearer ${t.accessToken}` });

function toTrack(
  r: TidalResource,
  included: Map<string, TidalResource> = new Map(),
): UniversalTrack {
  const artistIds = r.relationships?.artists?.data?.map((a) => a.id) ?? [];
  const artists = artistIds
    .map((id) => included.get(`artists:${id}`)?.attributes as { name?: string } | undefined)
    .map((a) => a?.name)
    .filter((n): n is string => !!n);
  const albumId = r.relationships?.album?.data?.id;
  const album = albumId
    ? ((included.get(`albums:${albumId}`)?.attributes as { title?: string } | undefined)?.title ?? '')
    : '';
  const isrc = r.attributes.isrc;
  return {
    id: `tidal:${r.id}`,
    universalId: isrc ? `isrc:${isrc}` : `tidal:${r.id}`,
    title: r.attributes.title,
    artist: artists,
    album,
    duration: r.attributes.durationMs ?? 0,
    isrc,
    platformIds: emptyPlatformIds({ tidal: r.id }),
    artwork: '',
    explicit: r.attributes.explicit ?? false,
    availableOn: ['tidal'],
  };
}

function indexIncluded(inc?: TidalResource[]): Map<string, TidalResource> {
  const m = new Map<string, TidalResource>();
  for (const r of inc ?? []) m.set(`${r.type}:${r.id}`, r);
  return m;
}

export const tidalService: PlatformService = {
  platform: 'tidal',

  async listPlaylists(token, cursor) {
    const res = await http.get<TidalListResp<{ name: string; description?: string }>>(
      '/playlists',
      { headers: auth(token), params: { 'page[cursor]': cursor, 'filter[r.owners.id]': 'me' } },
    );
    return {
      items: res.data.data.map((p) => ({
        id: `tidal:${p.id}`,
        name: p.attributes.name,
        description: p.attributes.description,
        tracks: [],
        sourcePlatform: 'tidal',
        syncedTo: [],
        lastSynced: emptyLastSynced(),
        isPublic: false,
        createdAt: new Date(),
        updatedAt: new Date(),
      })),
      nextCursor: res.data.links?.next ?? null,
    };
  },

  async getPlaylist(token, externalId) {
    const id = stripPrefix(externalId, 'tidal:');
    const meta = await http.get<{ data: TidalResource<{ name: string; description?: string }> }>(
      `/playlists/${id}`,
      { headers: auth(token) },
    );
    const tracks: UniversalTrack[] = [];
    let cursor: string | undefined;
    do {
      const page = await http.get<TidalListResp>(`/playlists/${id}/relationships/items`, {
        headers: auth(token),
        params: { 'page[cursor]': cursor, include: 'items.artists,items.album' },
      });
      const included = indexIncluded(page.data.included);
      tracks.push(...page.data.data.map((r) => toTrack(r, included)));
      cursor = page.data.links?.next;
    } while (cursor);
    return {
      id: `tidal:${meta.data.data.id}`,
      name: meta.data.data.attributes.name,
      description: meta.data.data.attributes.description,
      tracks,
      sourcePlatform: 'tidal',
      syncedTo: [],
      lastSynced: emptyLastSynced(),
      isPublic: false,
      createdAt: new Date(),
      updatedAt: new Date(),
    };
  },

  async searchTracks(token, query, limit = 20) {
    const res = await http.get<TidalListResp>('/searchresults/tracks', {
      headers: auth(token),
      params: { query, 'page[limit]': limit, include: 'artists,album' },
    });
    const included = indexIncluded(res.data.included);
    return res.data.data.map((r) => toTrack(r, included));
  },

  async getTrack(token, externalId) {
    const id = stripPrefix(externalId, 'tidal:');
    const res = await http.get<{ data: TidalResource; included?: TidalResource[] }>(
      `/tracks/${id}`,
      { headers: auth(token), params: { include: 'artists,album' } },
    );
    return res.data.data ? toTrack(res.data.data, indexIncluded(res.data.included)) : null;
  },

  async findByIsrc(token, isrc) {
    const res = await http.get<TidalListResp>('/tracks', {
      headers: auth(token),
      params: { 'filter[isrc]': isrc, 'page[limit]': 1, include: 'artists,album' },
    });
    const t = res.data.data[0];
    return t ? toTrack(t, indexIncluded(res.data.included)) : null;
  },

  async createPlaylist(token, args) {
    const res = await http.post<{ data: { id: string } }>(
      '/playlists',
      {
        data: {
          type: 'playlists',
          attributes: { name: args.name, description: args.description, accessType: args.isPublic ? 'PUBLIC' : 'UNLISTED' },
        },
      },
      { headers: { ...auth(token), 'Content-Type': 'application/vnd.api+json' } },
    );
    return { externalId: `tidal:${res.data.data.id}` };
  },

  async addTracks(token, externalPlaylistId, externalTrackIds) {
    const id = stripPrefix(externalPlaylistId, 'tidal:');
    const data = externalTrackIds.map((t) => ({ id: stripPrefix(t, 'tidal:'), type: 'tracks' }));
    for (let i = 0; i < data.length; i += 100) {
      await http.post(
        `/playlists/${id}/relationships/items`,
        { data: data.slice(i, i + 100) },
        { headers: { ...auth(token), 'Content-Type': 'application/vnd.api+json' } },
      );
    }
  },

  async removeTracks(token, externalPlaylistId, externalTrackIds) {
    const id = stripPrefix(externalPlaylistId, 'tidal:');
    const data = externalTrackIds.map((t) => ({ id: stripPrefix(t, 'tidal:'), type: 'tracks' }));
    await http.delete(`/playlists/${id}/relationships/items`, {
      headers: { ...auth(token), 'Content-Type': 'application/vnd.api+json' },
      data: { data },
    });
  },
};
