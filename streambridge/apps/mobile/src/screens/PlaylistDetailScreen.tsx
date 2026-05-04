import React from 'react';
import { Text, View } from 'react-native';
import { FlashList } from '@shopify/flash-list';
import { Button, ButtonRow, PlatformBadge, TrackRow, theme } from '@streambridge/ui';
import { useNavigation, useRoute, type RouteProp } from '@react-navigation/native';
import { usePlaylist } from '../hooks/queries';
import type { RootStackParamList } from '../navigation/RootNavigator';

export function PlaylistDetailScreen() {
  const route = useRoute<RouteProp<RootStackParamList, 'PlaylistDetail'>>();
  const nav = useNavigation();
  const { data: playlist, isLoading } = usePlaylist(route.params.playlistId);

  if (!playlist && !isLoading) {
    return (
      <View style={{ flex: 1, backgroundColor: theme.colors.bg, padding: 20 }}>
        <Text style={{ color: theme.colors.textMuted }}>Playlist not found.</Text>
      </View>
    );
  }
  if (!playlist) {
    return <View style={{ flex: 1, backgroundColor: theme.colors.bg }} />;
  }

  return (
    <View style={{ flex: 1, backgroundColor: theme.colors.bg }}>
      <View style={{ padding: 20, gap: 8 }}>
        <Text style={{ ...theme.font.h1, color: theme.colors.text }}>{playlist.name}</Text>
        {playlist.description && (
          <Text style={{ color: theme.colors.textMuted }}>{playlist.description}</Text>
        )}
        <View style={{ flexDirection: 'row', gap: 6, marginTop: 6, flexWrap: 'wrap' }}>
          <PlatformBadge platform={playlist.sourcePlatform} />
          {playlist.syncedTo.map((p) => (
            <PlatformBadge key={p} platform={p} />
          ))}
        </View>
        <ButtonRow>
          <Button
            title="Sync to…"
            onPress={() =>
              nav.navigate('PlatformPickerModal' as never, {
                playlistId: playlist.id,
                sourcePlatform: playlist.sourcePlatform,
              } as never)
            }
          />
          <Button title={`${playlist.tracks.length} tracks`} variant="ghost" />
        </ButtonRow>
      </View>
      <View style={{ flex: 1, paddingHorizontal: 16 }}>
        <FlashList
          data={playlist.tracks}
          estimatedItemSize={64}
          keyExtractor={(t) => t.id}
          renderItem={({ item }) => <TrackRow track={item} />}
        />
      </View>
    </View>
  );
}
