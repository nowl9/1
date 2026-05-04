import { create } from 'zustand';
import { sessionTokenStorage } from '../lib/api';
import { api } from '../lib/api';

interface User { id: string; email: string; displayName: string }

interface AuthState {
  user: User | null;
  hydrated: boolean;
  hydrate: () => Promise<void>;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string, displayName: string) => Promise<void>;
  logout: () => Promise<void>;
}

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  hydrated: false,
  async hydrate() {
    const token = await sessionTokenStorage.get();
    set({ hydrated: true, user: token ? { id: 'self', email: '', displayName: '' } : null });
  },
  async login(email, password) {
    const res = await api.login(email, password);
    await sessionTokenStorage.set(res.token);
    set({ user: res.user });
  },
  async register(email, password, displayName) {
    const res = await api.register(email, password, displayName);
    await sessionTokenStorage.set(res.token);
    set({ user: res.user });
  },
  async logout() {
    await sessionTokenStorage.clear();
    set({ user: null });
  },
}));
