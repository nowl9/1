import { create } from 'zustand';
import type { SyncJob } from '@streambridge/types';

interface SyncState {
  active: Record<string, SyncJob>;
  setJob: (job: SyncJob) => void;
  removeJob: (id: string) => void;
}

export const useSyncStore = create<SyncState>((set) => ({
  active: {},
  setJob: (job) => set((s) => ({ active: { ...s.active, [job.id]: job } })),
  removeJob: (id) =>
    set((s) => {
      const next = { ...s.active };
      delete next[id];
      return { active: next };
    }),
}));
