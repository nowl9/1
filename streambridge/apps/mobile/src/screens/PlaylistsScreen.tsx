import React from 'react';
import { Pressable, ScrollView, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { FlashList } from '@shopify/flash-list';
import { PlaylistCard, theme } from '@streambridge/ui';
import { PLATFORMS, PLATFORM_DISPLAY_NAMES, type Platform, type UniversalPlaylist } from '@streambridge/types';
import { useNavigation } from '@react-navigation/native';
import { usePlaylists } from '../hooks/queries';
import { usePlaylistStore } from '../stores/playlists';

export function PlaylistsScreen() {
  const nav = useNavigation();
  const filter = usePlaylistStore((s) => s.filter);
  const setFilter = usePlaylistStore((s) => s.setFilter);
  const { data = [], isLoading, refetch } = usePlaylists(filter);
  const flat: UniversalPlaylist[] = data.flatMap((r) => (r.ok ? r.items : []));

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: theme.colors.bg }}>
      <View style={{ paddingHorizontal: 20, paddingTop: 12 }}>
        <Text style={{ ...theme.font.h1, color: theme.colors.text }}>Playlists</Text>
      </View>
      <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ paddingHorizontal: 16, gap: 8, marginTop: 12 }}>
        {(['all', ...PLATFORMS] as const).map((p) => {
          const active = filter === p;
          return (
            <Pressable
              key={p}
              onPress={() => setFilter(p)}
              accessibilityRole="button"
              accessibilityState={{ selected: active }}
              style={{
                paddingHorizontal: 14,
                paddingVertical: 8,
                borderRadius: theme.radius.pill,
                backgroundColor: active ? theme.colors.primary : theme.colors.bgElevated,
                minHeight: 44,
                alignSelf: 'center',
                justifyContent: 'center',
              }}
            >
              <Text style={{ color: theme.colors.text, fontWeight: '600' }}>
                {p === 'all' ? 'All' : PLATFORM_DISPLAY_NAMES[p as Platform]}
              </Text>
            </Pressable>
          );
        })}
      </ScrollView>

      <View style={{ flex: 1, paddingHorizontal: 12, paddingTop: 12 }}>
        <FlashList
          data={flat}
          numColumns={2}
          estimatedItemSize={220}
          refreshing={isLoading}
          onRefresh={refetch}
          keyExtractor={(p) => p.id}
          renderItem={({ item }) => (
            <View style={{ flex: 1, padding: 8 }}>
              <PlaylistCard playlist={item} onPress={() => nav.navigate('PlaylistDetail' as never, { playlistId: item.id } as never)} />
            </View>
          )}
        />
      </View>
    </SafeAreaView>
  );
}
