import React from 'react';
import { Text } from 'react-native';
import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';
import { theme } from '@streambridge/ui';
import { HomeScreen } from '../screens/HomeScreen';
import { PlaylistsScreen } from '../screens/PlaylistsScreen';
import { SyncScreen } from '../screens/SyncScreen';
import { DiscoverScreen } from '../screens/DiscoverScreen';
import { SettingsScreen } from '../screens/SettingsScreen';

const Tab = createBottomTabNavigator();

const tabIcon = (label: string) => ({ color }: { color: string }) => (
  <Text style={{ color, fontSize: 16 }}>{label}</Text>
);

export function TabNavigator() {
  return (
    <Tab.Navigator
      screenOptions={{
        headerStyle: { backgroundColor: theme.colors.bg },
        headerTitleStyle: { color: theme.colors.text },
        tabBarStyle: { backgroundColor: theme.colors.bgElevated, borderTopColor: theme.colors.border },
        tabBarActiveTintColor: theme.colors.primary,
        tabBarInactiveTintColor: theme.colors.textMuted,
      }}
    >
      <Tab.Screen name="Home" component={HomeScreen} options={{ tabBarIcon: tabIcon('🏠') }} />
      <Tab.Screen name="Playlists" component={PlaylistsScreen} options={{ tabBarIcon: tabIcon('📚') }} />
      <Tab.Screen name="Sync" component={SyncScreen} options={{ tabBarIcon: tabIcon('🔄') }} />
      <Tab.Screen name="Discover" component={DiscoverScreen} options={{ tabBarIcon: tabIcon('✨') }} />
      <Tab.Screen name="Settings" component={SettingsScreen} options={{ tabBarIcon: tabIcon('⚙️') }} />
    </Tab.Navigator>
  );
}
