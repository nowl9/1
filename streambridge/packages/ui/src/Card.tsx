import React from 'react';
import { View, ViewProps, ViewStyle } from 'react-native';
import { theme } from './theme';

export function Card({ style, children, ...rest }: ViewProps & { style?: ViewStyle }) {
  return (
    <View
      {...rest}
      style={[{
        backgroundColor: theme.colors.bgElevated,
        borderRadius: theme.radius.lg,
        padding: theme.spacing(4),
        borderWidth: 1,
        borderColor: theme.colors.border,
      }, style]}
    >
      {children}
    </View>
  );
}
