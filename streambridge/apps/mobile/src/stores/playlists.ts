import { create } from 'zustand';
import type { Platform, UniversalPlaylist } from '@streambridge/types';

interface PlaylistState {
  filter: Platform | 'all';
  setFilter: (p: Platform | 'all') => void;
  cached: Record<string, UniversalPlaylist>;
  setCached: (p: UniversalPlaylist) => void;
}

export const usePlaylistStore = create<PlaylistState>((set) => ({
  filter: 'all',
  setFilter: (filter) => set({ filter }),
  cached: {},
  setCached: (p) => set((s) => ({ cached: { ...s.cached, [p.id]: p } })),
}));
