import axios, { AxiosInstance } from 'axios';
import type {
  Platform,
  SyncJob,
  SyncOptions,
  UniversalPlaylist,
  UniversalTrack,
} from '@streambridge/types';

export interface ApiClientOptions {
  baseUrl: string;
  getToken: () => Promise<string | null>;
  onUnauthorized?: () => void;
}

export interface PlaylistsByPlatformResult {
  ok: boolean;
  platform: Platform;
  items: UniversalPlaylist[];
  nextCursor: string | null;
  error?: string;
}

export class StreamBridgeClient {
  private readonly http: AxiosInstance;

  constructor(opts: ApiClientOptions) {
    this.http = axios.create({ baseURL: opts.baseUrl, timeout: 30_000 });
    this.http.interceptors.request.use(async (cfg) => {
      const t = await opts.getToken();
      if (t) cfg.headers.Authorization = `Bearer ${t}`;
      return cfg;
    });
    this.http.interceptors.response.use(
      (r) => r,
      (err) => {
        if (err?.response?.status === 401) opts.onUnauthorized?.();
        throw err;
      },
    );
  }

  async login(email: string, password: string) {
    const { data } = await this.http.post<{ token: string; user: { id: string; email: string; displayName: string } }>('/user/login', { email, password });
    return data;
  }

  async register(email: string, password: string, displayName: string) {
    const { data } = await this.http.post<{ token: string; user: { id: string; email: string; displayName: string } }>('/user/register', { email, password, displayName });
    return data;
  }

  async getConnections() {
    const { data } = await this.http.get<{ connections: Array<{ platform: Platform; expiresAt: string; scopes: string[] }> }>('/user/connections');
    return data.connections;
  }

  async startConnect(platform: Platform) {
    const { data } = await this.http.post<{ authorizationUrl: string; state: string }>(`/auth/${platform}/connect`);
    return data;
  }

  async completeConnect(platform: Platform, code: string, state: string) {
    const { data } = await this.http.post<{ ok: boolean; expiresAt: number }>(`/auth/${platform}/callback`, { code, state });
    return data;
  }

  async disconnect(platform: Platform) {
    await this.http.delete(`/auth/${platform}/disconnect`);
  }

  async listPlaylists(filter?: { platform?: Platform; cursor?: string }) {
    const { data } = await this.http.get<{ results: PlaylistsByPlatformResult[] }>('/playlists', { params: filter });
    return data.results;
  }

  async getPlaylist(id: string) {
    const { data } = await this.http.get<{ playlist: UniversalPlaylist }>(`/playlists/${encodeURIComponent(id)}`);
    return data.playlist;
  }

  async startSync(options: SyncOptions) {
    const { data } = await this.http.post<{ jobId: string }>('/playlists/sync', options);
    return data;
  }

  async getSyncStatus(playlistId: string, jobId: string) {
    const { data } = await this.http.get<{ job: SyncJob }>(`/playlists/${encodeURIComponent(playlistId)}/sync-status/${jobId}`);
    return data.job;
  }

  async searchTracks(q: string, platform?: Platform) {
    const { data } = await this.http.get<{ results: Array<{ ok: boolean; platform: Platform; tracks: UniversalTrack[] }> }>('/tracks/search', { params: { q, platform } });
    return data.results;
  }

  async overrideMatch(args: { sourceTrackId: string; candidateTrackId: string; platform: Platform }) {
    const { data } = await this.http.post<{ match: unknown }>('/tracks/match', args);
    return data;
  }

  async getSyncHistory() {
    const { data } = await this.http.get<{ jobs: SyncJob[] }>('/user/sync-history');
    return data.jobs;
  }
}
