import React, { useEffect } from 'react';
import { View } from 'react-native';
import Animated, { useAnimatedStyle, useSharedValue, withTiming, Easing } from 'react-native-reanimated';
import { theme } from './theme';

export function ProgressBar({ progress, color = theme.colors.accent }: { progress: number; color?: string }) {
  const sv = useSharedValue(progress);
  useEffect(() => {
    sv.value = withTiming(Math.max(0, Math.min(1, progress)), { duration: 350, easing: Easing.out(Easing.cubic) });
  }, [progress, sv]);
  const fillStyle = useAnimatedStyle(() => ({ width: `${sv.value * 100}%` }));

  return (
    <View
      accessibilityRole="progressbar"
      accessibilityValue={{ now: Math.round(progress * 100), min: 0, max: 100 }}
      style={{ height: 8, backgroundColor: theme.colors.border, borderRadius: theme.radius.pill, overflow: 'hidden' }}
    >
      <Animated.View style={[{ height: '100%', backgroundColor: color }, fillStyle]} />
    </View>
  );
}
