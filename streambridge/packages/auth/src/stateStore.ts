import type { StateStore } from './types';

/**
 * In-memory state store for OAuth flows. For multi-instance deployments,
 * swap this for a Redis-backed implementation that satisfies the same
 * interface — nothing else in the package needs to change.
 */
export class MemoryStateStore implements StateStore {
  private readonly map = new Map<string, { value: Parameters<StateStore['put']>[1]; expiresAt: number }>();

  async put(state: string, value: Parameters<StateStore['put']>[1]): Promise<void> {
    this.map.set(state, { value, expiresAt: Date.now() + 10 * 60_000 });
  }

  async take(state: string): Promise<Parameters<StateStore['put']>[1] | null> {
    const row = this.map.get(state);
    if (!row) return null;
    this.map.delete(state);
    if (row.expiresAt < Date.now()) return null;
    return row.value;
  }
}

let store: StateStore = new MemoryStateStore();
export const getStateStore = (): StateStore => store;
export const setStateStore = (s: StateStore): void => {
  store = s;
};
