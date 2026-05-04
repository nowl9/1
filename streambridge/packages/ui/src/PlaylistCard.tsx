import React from 'react';
import { Image, Pressable, Text, View } from 'react-native';
import type { UniversalPlaylist } from '@streambridge/types';
import { theme } from './theme';
import { PlatformBadge } from './PlatformBadge';

export function PlaylistCard({ playlist, onPress }: { playlist: UniversalPlaylist; onPress?: () => void }) {
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={`Playlist ${playlist.name}, ${playlist.tracks.length} tracks`}
      style={({ pressed }) => ({
        backgroundColor: theme.colors.bgElevated,
        borderRadius: theme.radius.lg,
        overflow: 'hidden',
        opacity: pressed ? 0.85 : 1,
      })}
    >
      {playlist.artwork ? (
        <Image source={{ uri: playlist.artwork }} style={{ width: '100%', aspectRatio: 1 }} />
      ) : (
        <View style={{ width: '100%', aspectRatio: 1, backgroundColor: theme.colors.bg }} />
      )}
      <View style={{ padding: 12, gap: 6 }}>
        <Text numberOfLines={1} style={{ color: theme.colors.text, fontWeight: '700' }}>{playlist.name}</Text>
        <Text style={{ color: theme.colors.textMuted, fontSize: 12 }}>{playlist.tracks.length} tracks</Text>
        <PlatformBadge platform={playlist.sourcePlatform} />
      </View>
    </Pressable>
  );
}
