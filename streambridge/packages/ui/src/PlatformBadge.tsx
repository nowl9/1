import React from 'react';
import { Text, View } from 'react-native';
import { PLATFORM_DISPLAY_NAMES, type Platform } from '@streambridge/types';
import { theme } from './theme';

export function PlatformBadge({ platform, size = 'sm' }: { platform: Platform; size?: 'sm' | 'md' }) {
  const px = size === 'sm' ? 8 : 12;
  const py = size === 'sm' ? 4 : 6;
  const fs = size === 'sm' ? 11 : 13;
  return (
    <View
      accessibilityLabel={PLATFORM_DISPLAY_NAMES[platform]}
      style={{
        backgroundColor: theme.colors.platform[platform],
        paddingHorizontal: px,
        paddingVertical: py,
        borderRadius: theme.radius.pill,
        alignSelf: 'flex-start',
      }}
    >
      <Text style={{ color: '#fff', fontSize: fs, fontWeight: '700' }}>
        {PLATFORM_DISPLAY_NAMES[platform]}
      </Text>
    </View>
  );
}
