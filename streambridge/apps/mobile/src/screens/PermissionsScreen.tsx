import React from 'react';
import { ScrollView, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import * as Notifications from 'expo-notifications';
import { Button, Card, theme } from '@streambridge/ui';
import { useNavigation, CommonActions } from '@react-navigation/native';

export function PermissionsScreen() {
  const nav = useNavigation();
  async function finish() {
    await Notifications.requestPermissionsAsync();
    nav.dispatch(CommonActions.reset({ index: 0, routes: [{ name: 'Main' }] }));
  }
  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: theme.colors.bg }}>
      <ScrollView contentContainerStyle={{ padding: 20, gap: 16 }}>
        <Text style={{ ...theme.font.h1, color: theme.colors.text }}>One last thing</Text>
        <Card>
          <Text style={{ color: theme.colors.text, fontWeight: '600', marginBottom: 6 }}>Notifications</Text>
          <Text style={{ color: theme.colors.textMuted }}>
            We notify you when a sync finishes or needs your attention.
          </Text>
        </Card>
        <Card>
          <Text style={{ color: theme.colors.text, fontWeight: '600', marginBottom: 6 }}>Background sync</Text>
          <Text style={{ color: theme.colors.textMuted }}>
            Your playlists stay in sync even when the app is closed.
          </Text>
        </Card>
        <Card>
          <Text style={{ color: theme.colors.text, fontWeight: '600', marginBottom: 6 }}>What we never do</Text>
          <Text style={{ color: theme.colors.textMuted }}>
            We don't sell data, don't share your tokens, and never play audio.
          </Text>
        </Card>
        <View style={{ marginTop: 8 }}>
          <Button title="Get started" onPress={finish} />
        </View>
      </ScrollView>
    </SafeAreaView>
  );
}
