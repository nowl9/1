import React from 'react';
import { Image, Text, View } from 'react-native';
import type { UniversalTrack } from '@streambridge/types';
import { theme } from './theme';

export type TrackSyncStatus = 'matched' | 'unavailable' | 'pending' | 'review';

const STATUS_GLYPH: Record<TrackSyncStatus, { icon: string; color: string; label: string }> = {
  matched: { icon: '✓', color: theme.colors.accent, label: 'Matched' },
  unavailable: { icon: '✗', color: theme.colors.danger, label: 'Unavailable' },
  pending: { icon: '⏳', color: theme.colors.textMuted, label: 'Pending' },
  review: { icon: '⚠', color: '#FFB400', label: 'Needs review' },
};

export function TrackRow({
  track,
  status,
  onPress,
}: {
  track: UniversalTrack;
  status?: TrackSyncStatus;
  onPress?: () => void;
}) {
  const s = status ? STATUS_GLYPH[status] : null;
  return (
    <View
      accessibilityRole={onPress ? 'button' : undefined}
      accessibilityLabel={`${track.title} by ${track.artist.join(', ')}${s ? `, ${s.label}` : ''}`}
      style={{ flexDirection: 'row', alignItems: 'center', paddingVertical: 10, gap: 12, minHeight: 44 }}
    >
      {track.artwork ? (
        <Image source={{ uri: track.artwork }} style={{ width: 44, height: 44, borderRadius: theme.radius.sm }} />
      ) : (
        <View style={{ width: 44, height: 44, borderRadius: theme.radius.sm, backgroundColor: theme.colors.bgElevated }} />
      )}
      <View style={{ flex: 1, minWidth: 0 }}>
        <Text numberOfLines={1} style={{ color: theme.colors.text, fontWeight: '600' }}>{track.title}</Text>
        <Text numberOfLines={1} style={{ color: theme.colors.textMuted, fontSize: 13 }}>
          {track.artist.join(', ')}{track.album ? ` · ${track.album}` : ''}
        </Text>
      </View>
      {s && (
        <View accessibilityLabel={s.label} style={{ width: 28, alignItems: 'center' }}>
          <Text style={{ color: s.color, fontSize: 16 }}>{s.icon}</Text>
        </View>
      )}
    </View>
  );
}
