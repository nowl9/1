import { StreamBridgeClient } from '@streambridge/api-client';
import * as SecureStore from 'expo-secure-store';
import { useAuthStore } from '../stores/auth';

const BASE_URL = process.env.EXPO_PUBLIC_API_URL ?? 'http://localhost:4000';
const TOKEN_KEY = 'streambridge.session';

export const sessionTokenStorage = {
  async get(): Promise<string | null> {
    return SecureStore.getItemAsync(TOKEN_KEY);
  },
  async set(token: string): Promise<void> {
    await SecureStore.setItemAsync(TOKEN_KEY, token);
  },
  async clear(): Promise<void> {
    await SecureStore.deleteItemAsync(TOKEN_KEY);
  },
};

export const api = new StreamBridgeClient({
  baseUrl: BASE_URL,
  getToken: () => sessionTokenStorage.get(),
  onUnauthorized: () => {
    void sessionTokenStorage.clear();
    useAuthStore.getState().logout();
  },
});
