import React from 'react';
import { Pressable, Text, View } from 'react-native';
import { PLATFORM_DISPLAY_NAMES, type Platform } from '@streambridge/types';
import { theme } from './theme';

export function ServiceConnectCard({
  platform,
  connected,
  onPress,
}: {
  platform: Platform;
  connected: boolean;
  onPress?: () => void;
}) {
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={`${connected ? 'Disconnect' : 'Connect'} ${PLATFORM_DISPLAY_NAMES[platform]}`}
      style={({ pressed }) => ({
        backgroundColor: theme.colors.bgElevated,
        borderRadius: theme.radius.lg,
        padding: 16,
        borderWidth: 2,
        borderColor: connected ? theme.colors.platform[platform] : theme.colors.border,
        opacity: pressed ? 0.85 : 1,
      })}
    >
      <View style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' }}>
        <View>
          <Text style={{ color: theme.colors.text, fontWeight: '700', fontSize: 16 }}>
            {PLATFORM_DISPLAY_NAMES[platform]}
          </Text>
          <Text style={{ color: connected ? theme.colors.accent : theme.colors.textMuted, fontSize: 12, marginTop: 4 }}>
            {connected ? 'Connected' : 'Tap to connect'}
          </Text>
        </View>
        <View style={{ width: 12, height: 12, borderRadius: 6, backgroundColor: connected ? theme.colors.accent : theme.colors.textMuted }} />
      </View>
    </Pressable>
  );
}
