import { MMKV } from 'react-native-mmkv';
import type { UniversalPlaylist } from '@streambridge/types';

// MMKV is the offline cache for last-known-good playlist data. Reads are
// synchronous so screens can render stale-while-revalidate without flicker.
const mmkv = new MMKV({ id: 'streambridge-cache' });

export const playlistCache = {
  get(id: string): UniversalPlaylist | null {
    const raw = mmkv.getString(`playlist:${id}`);
    return raw ? (JSON.parse(raw) as UniversalPlaylist) : null;
  },
  set(playlist: UniversalPlaylist): void {
    mmkv.set(`playlist:${playlist.id}`, JSON.stringify(playlist));
  },
  setAll(playlists: UniversalPlaylist[]): void {
    mmkv.set('playlists:index', JSON.stringify(playlists.map((p) => p.id)));
    for (const p of playlists) this.set(p);
  },
  index(): string[] {
    const raw = mmkv.getString('playlists:index');
    return raw ? (JSON.parse(raw) as string[]) : [];
  },
};
