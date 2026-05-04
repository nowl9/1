import { createPlatformHttp } from '../utils/http';
import type { PlatformService } from './platform';
import type { AuthToken, UniversalPlaylist, UniversalTrack } from '@streambridge/types';
import { emptyLastSynced, emptyPlatformIds, stripPrefix } from './_shared';

/**
 * YouTube Music sync uses YouTube Data API v3 against the user's playlists.
 * YT Music doesn't expose ISRC directly through the public API, so cross-
 * platform matching for this adapter falls back to fuzzy metadata.
 */

interface YtSnippet {
  title: string;
  description?: string;
  channelTitle?: string;
  videoOwnerChannelTitle?: string;
  thumbnails?: { high?: { url: string }; default?: { url: string } };
  resourceId?: { videoId: string };
}
interface YtPlaylist { id: string; snippet: YtSnippet; contentDetails?: { itemCount: number }; status?: { privacyStatus: string } }
interface YtPlaylistItem { id: string; snippet: YtSnippet; contentDetails: { videoId: string; videoPublishedAt?: string } }
interface YtVideo {
  id: string;
  snippet: YtSnippet;
  contentDetails: { duration: string };
}
interface YtListResp<T> { items: T[]; nextPageToken?: string }

const http = createPlatformHttp({
  platform: 'youtube-music',
  baseURL: 'https://www.googleapis.com/youtube/v3',
});

const auth = (t: AuthToken) => ({ Authorization: `Bearer ${t.accessToken}` });

function parseIsoDuration(d: string): number {
  // "PT3M21S" -> ms
  const m = /^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$/.exec(d);
  if (!m) return 0;
  const [, h, mm, s] = m;
  return ((Number(h ?? 0) * 3600) + (Number(mm ?? 0) * 60) + Number(s ?? 0)) * 1000;
}

function videoToTrack(v: YtVideo): UniversalTrack {
  return {
    id: `youtube-music:${v.id}`,
    universalId: `youtube-music:${v.id}`,
    title: v.snippet.title,
    artist: v.snippet.channelTitle ? [v.snippet.channelTitle.replace(/ - Topic$/, '')] : [],
    album: '',
    duration: parseIsoDuration(v.contentDetails.duration),
    platformIds: emptyPlatformIds({ 'youtube-music': v.id }),
    artwork: v.snippet.thumbnails?.high?.url ?? v.snippet.thumbnails?.default?.url ?? '',
    explicit: false,
    availableOn: ['youtube-music'],
  };
}

async function videosByIds(token: AuthToken, ids: string[]): Promise<YtVideo[]> {
  const out: YtVideo[] = [];
  for (let i = 0; i < ids.length; i += 50) {
    const res = await http.get<YtListResp<YtVideo>>('/videos', {
      headers: auth(token),
      params: { part: 'snippet,contentDetails', id: ids.slice(i, i + 50).join(',') },
    });
    out.push(...res.data.items);
  }
  return out;
}

export const youtubeMusicService: PlatformService = {
  platform: 'youtube-music',

  async listPlaylists(token, cursor) {
    const res = await http.get<YtListResp<YtPlaylist>>('/playlists', {
      headers: auth(token),
      params: { part: 'snippet,status,contentDetails', mine: true, maxResults: 50, pageToken: cursor },
    });
    return {
      items: res.data.items.map((p) => ({
        id: `youtube-music:${p.id}`,
        name: p.snippet.title,
        description: p.snippet.description,
        tracks: [],
        sourcePlatform: 'youtube-music',
        syncedTo: [],
        lastSynced: emptyLastSynced(),
        artwork: p.snippet.thumbnails?.high?.url,
        isPublic: p.status?.privacyStatus === 'public',
        createdAt: new Date(),
        updatedAt: new Date(),
      })),
      nextCursor: res.data.nextPageToken ?? null,
    };
  },

  async getPlaylist(token, externalId) {
    const id = stripPrefix(externalId, 'youtube-music:');
    const meta = await http.get<YtListResp<YtPlaylist>>('/playlists', {
      headers: auth(token),
      params: { part: 'snippet,status', id },
    });
    const head = meta.data.items[0];
    if (!head) throw new Error('playlist not found');
    const videoIds: string[] = [];
    let pageToken: string | undefined;
    do {
      const page = await http.get<YtListResp<YtPlaylistItem>>('/playlistItems', {
        headers: auth(token),
        params: { part: 'snippet,contentDetails', playlistId: id, maxResults: 50, pageToken },
      });
      for (const it of page.data.items) videoIds.push(it.contentDetails.videoId);
      pageToken = page.data.nextPageToken;
    } while (pageToken);
    const videos = await videosByIds(token, videoIds);
    return {
      id: `youtube-music:${head.id}`,
      name: head.snippet.title,
      description: head.snippet.description,
      tracks: videos.map(videoToTrack),
      sourcePlatform: 'youtube-music',
      syncedTo: [],
      lastSynced: emptyLastSynced(),
      artwork: head.snippet.thumbnails?.high?.url,
      isPublic: head.status?.privacyStatus === 'public',
      createdAt: new Date(),
      updatedAt: new Date(),
    };
  },

  async searchTracks(token, query, limit = 20) {
    const res = await http.get<YtListResp<{ id: { videoId: string } }>>('/search', {
      headers: auth(token),
      params: { part: 'snippet', q: `${query} song`, type: 'video', maxResults: limit, videoCategoryId: '10' },
    });
    const ids = res.data.items.map((i) => i.id.videoId).filter(Boolean);
    if (!ids.length) return [];
    return (await videosByIds(token, ids)).map(videoToTrack);
  },

  async getTrack(token, externalId) {
    const [v] = await videosByIds(token, [stripPrefix(externalId, 'youtube-music:')]);
    return v ? videoToTrack(v) : null;
  },

  async findByIsrc(token, _isrc) {
    // YouTube Data API doesn't accept ISRC filters; matcher will fall back to fuzzy.
    void token;
    return null;
  },

  async createPlaylist(token, args) {
    const res = await http.post<YtPlaylist>(
      '/playlists',
      {
        snippet: { title: args.name, description: args.description ?? '' },
        status: { privacyStatus: args.isPublic ? 'public' : 'private' },
      },
      { headers: auth(token), params: { part: 'snippet,status' } },
    );
    return { externalId: `youtube-music:${res.data.id}` };
  },

  async addTracks(token, externalPlaylistId, externalTrackIds) {
    const playlistId = stripPrefix(externalPlaylistId, 'youtube-music:');
    // YouTube has no batch insert; one request per video, observe quota cost.
    for (const t of externalTrackIds) {
      await http.post(
        '/playlistItems',
        {
          snippet: {
            playlistId,
            resourceId: { kind: 'youtube#video', videoId: stripPrefix(t, 'youtube-music:') },
          },
        },
        { headers: auth(token), params: { part: 'snippet' } },
      );
    }
  },

  async removeTracks(token, externalPlaylistId, externalTrackIds) {
    // YouTube identifies playlist items by playlistItemId; resolve first.
    const playlistId = stripPrefix(externalPlaylistId, 'youtube-music:');
    let pageToken: string | undefined;
    const wanted = new Set(externalTrackIds.map((t) => stripPrefix(t, 'youtube-music:')));
    do {
      const page = await http.get<YtListResp<YtPlaylistItem>>('/playlistItems', {
        headers: auth(token),
        params: { part: 'contentDetails', playlistId, maxResults: 50, pageToken },
      });
      for (const it of page.data.items) {
        if (wanted.has(it.contentDetails.videoId)) {
          await http.delete('/playlistItems', { headers: auth(token), params: { id: it.id } });
        }
      }
      pageToken = page.data.nextPageToken;
    } while (pageToken);
  },
};
