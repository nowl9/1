import React from 'react';
import { Pressable, Text, View, ViewStyle, ActivityIndicator } from 'react-native';
import { theme } from './theme';

export interface ButtonProps {
  title: string;
  onPress?: () => void;
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger';
  loading?: boolean;
  disabled?: boolean;
  style?: ViewStyle;
  accessibilityLabel?: string;
}

export function Button({ title, onPress, variant = 'primary', loading, disabled, style, accessibilityLabel }: ButtonProps) {
  const bg =
    variant === 'primary' ? theme.colors.primary :
    variant === 'secondary' ? theme.colors.bgElevated :
    variant === 'danger' ? theme.colors.danger :
    'transparent';
  const fg = variant === 'ghost' ? theme.colors.primary : theme.colors.text;
  const borderColor = variant === 'ghost' ? theme.colors.primary : 'transparent';
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled || loading}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel ?? title}
      accessibilityState={{ disabled: !!disabled || !!loading, busy: !!loading }}
      style={({ pressed }) => [{
        backgroundColor: bg,
        borderColor,
        borderWidth: variant === 'ghost' ? 1 : 0,
        opacity: disabled ? 0.5 : pressed ? 0.85 : 1,
        paddingVertical: 14,
        paddingHorizontal: 20,
        borderRadius: theme.radius.pill,
        minHeight: 48,
        alignItems: 'center',
        justifyContent: 'center',
      }, style]}
    >
      {loading ? <ActivityIndicator color={fg} /> : (
        <Text style={{ color: fg, fontSize: 16, fontWeight: '600' }}>{title}</Text>
      )}
    </Pressable>
  );
}

export const ButtonRow = ({ children }: { children: React.ReactNode }) => (
  <View style={{ flexDirection: 'row', gap: 8 }}>{children}</View>
);
