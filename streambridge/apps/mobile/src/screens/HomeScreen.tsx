import React from 'react';
import { ScrollView, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Button, ButtonRow, Card, PlatformBadge, theme } from '@streambridge/ui';
import { PLATFORM_DISPLAY_NAMES } from '@streambridge/types';
import { useConnections, useSyncHistory } from '../hooks/queries';
import { useNavigation } from '@react-navigation/native';

export function HomeScreen() {
  const nav = useNavigation();
  const { data: connections = [] } = useConnections();
  const { data: history = [] } = useSyncHistory();

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: theme.colors.bg }}>
      <ScrollView contentContainerStyle={{ padding: 20, gap: 16 }}>
        <Text style={{ ...theme.font.h1, color: theme.colors.text }}>Today</Text>

        <Card>
          <Text style={{ color: theme.colors.text, fontWeight: '700', fontSize: 16 }}>Connected services</Text>
          <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginTop: 12 }}>
            {connections.length === 0 ? (
              <Text style={{ color: theme.colors.textMuted }}>None yet — tap below to connect.</Text>
            ) : (
              connections.map((c) => <PlatformBadge key={c.platform} platform={c.platform} />)
            )}
          </View>
          <View style={{ marginTop: 16 }}>
            <ButtonRow>
              <Button title="Manage" variant="ghost" onPress={() => nav.navigate('Settings' as never)} />
              <Button title="New playlist" onPress={() => nav.navigate('CreatePlaylistModal' as never)} />
            </ButtonRow>
          </View>
        </Card>

        <Card>
          <Text style={{ color: theme.colors.text, fontWeight: '700', fontSize: 16 }}>Recent syncs</Text>
          <View style={{ marginTop: 12, gap: 8 }}>
            {history.length === 0 ? (
              <Text style={{ color: theme.colors.textMuted }}>No syncs yet.</Text>
            ) : (
              history.slice(0, 5).map((j) => (
                <View key={j.id} style={{ flexDirection: 'row', justifyContent: 'space-between' }}>
                  <Text style={{ color: theme.colors.text, flex: 1 }} numberOfLines={1}>
                    {PLATFORM_DISPLAY_NAMES[j.options.sourcePlatform]} → {j.options.targetPlatforms.length} services
                  </Text>
                  <Text style={{ color: theme.colors.textMuted }}>{j.matched}/{j.total}</Text>
                </View>
              ))
            )}
          </View>
        </Card>
      </ScrollView>
    </SafeAreaView>
  );
}
