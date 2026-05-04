import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { Platform, SyncOptions } from '@streambridge/types';
import { api } from '../lib/api';
import { playlistCache } from '../lib/storage';

export const qk = {
  connections: ['connections'] as const,
  playlists: (filter?: Platform | 'all') => ['playlists', filter ?? 'all'] as const,
  playlist: (id: string) => ['playlist', id] as const,
  syncStatus: (playlistId: string, jobId: string) => ['syncStatus', playlistId, jobId] as const,
  syncHistory: ['syncHistory'] as const,
};

export function useConnections() {
  return useQuery({ queryKey: qk.connections, queryFn: () => api.getConnections() });
}

export function usePlaylists(platform?: Platform | 'all') {
  return useQuery({
    queryKey: qk.playlists(platform),
    queryFn: async () => {
      const filter: { platform?: Platform } = {};
      if (platform && platform !== 'all') filter.platform = platform;
      const results = await api.listPlaylists(filter);
      const flat = results.flatMap((r) => (r.ok ? r.items : []));
      playlistCache.setAll(flat);
      return results;
    },
  });
}

export function usePlaylist(id: string) {
  return useQuery({
    queryKey: qk.playlist(id),
    queryFn: async () => {
      const p = await api.getPlaylist(id);
      playlistCache.set(p);
      return p;
    },
    initialData: () => playlistCache.get(id) ?? undefined,
  });
}

export function useStartSync() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (options: SyncOptions) => api.startSync(options),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.syncHistory }),
  });
}

export function useSyncStatus(playlistId: string, jobId: string | null) {
  return useQuery({
    queryKey: qk.syncStatus(playlistId, jobId ?? 'none'),
    enabled: !!jobId,
    queryFn: () => api.getSyncStatus(playlistId, jobId!),
    refetchInterval: (q) => {
      const status = q.state.data?.status;
      return status === 'completed' || status === 'failed' || status === 'partial' ? false : 1500;
    },
  });
}

export function useSyncHistory() {
  return useQuery({ queryKey: qk.syncHistory, queryFn: () => api.getSyncHistory() });
}

export function useStartConnect() {
  return useMutation({ mutationFn: (platform: Platform) => api.startConnect(platform) });
}

export function useDisconnect() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (platform: Platform) => api.disconnect(platform),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.connections }),
  });
}

export function useTrackSearch(q: string, platform?: Platform) {
  return useQuery({
    queryKey: ['trackSearch', q, platform ?? 'all'],
    enabled: q.length > 1,
    queryFn: () => api.searchTracks(q, platform),
  });
}
