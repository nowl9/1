import React from 'react';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import { useAuthStore } from '../stores/auth';
import { OnboardingNavigator } from './OnboardingNavigator';
import { TabNavigator } from './TabNavigator';
import { PlaylistDetailScreen } from '../screens/PlaylistDetailScreen';
import { TrackMatchReviewModal } from '../screens/TrackMatchReviewModal';
import { PlatformPickerModal } from '../screens/PlatformPickerModal';
import { CreatePlaylistModal } from '../screens/CreatePlaylistModal';
import type { Platform, UniversalPlaylist, UniversalTrack } from '@streambridge/types';

export type RootStackParamList = {
  Onboarding: undefined;
  Main: undefined;
  PlaylistDetail: { playlistId: string };
  TrackMatchReviewModal: { jobId: string; playlistId: string };
  PlatformPickerModal: { playlistId: string; sourcePlatform: Platform };
  CreatePlaylistModal: { sourcePlatform?: Platform } | undefined;
};

const Stack = createNativeStackNavigator<RootStackParamList>();

export function RootNavigator() {
  const user = useAuthStore((s) => s.user);
  return (
    <Stack.Navigator screenOptions={{ headerShown: false }}>
      {user ? (
        <>
          <Stack.Screen name="Main" component={TabNavigator} />
          <Stack.Screen name="PlaylistDetail" component={PlaylistDetailScreen} options={{ headerShown: true, title: '' }} />
          <Stack.Group screenOptions={{ presentation: 'modal' }}>
            <Stack.Screen name="TrackMatchReviewModal" component={TrackMatchReviewModal} options={{ headerShown: true, title: 'Review matches' }} />
            <Stack.Screen name="PlatformPickerModal" component={PlatformPickerModal} options={{ headerShown: true, title: 'Sync to' }} />
            <Stack.Screen name="CreatePlaylistModal" component={CreatePlaylistModal} options={{ headerShown: true, title: 'New playlist' }} />
          </Stack.Group>
        </>
      ) : (
        <Stack.Screen name="Onboarding" component={OnboardingNavigator} />
      )}
    </Stack.Navigator>
  );
}

// Re-exports for convenience
export type { Platform, UniversalPlaylist, UniversalTrack };
