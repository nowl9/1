import React from 'react';
import { Linking, ScrollView, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Button, Card, ServiceConnectCard, theme } from '@streambridge/ui';
import { PLATFORMS, type Platform } from '@streambridge/types';
import { useConnections, useDisconnect, useStartConnect } from '../hooks/queries';
import { useAuthStore } from '../stores/auth';

export function SettingsScreen() {
  const { data: connections = [] } = useConnections();
  const startConnect = useStartConnect();
  const disconnect = useDisconnect();
  const logout = useAuthStore((s) => s.logout);
  const connectedSet = new Set(connections.map((c) => c.platform));

  const onPress = async (p: Platform) => {
    if (connectedSet.has(p)) {
      await disconnect.mutateAsync(p);
    } else {
      const { authorizationUrl } = await startConnect.mutateAsync(p);
      await Linking.openURL(authorizationUrl);
    }
  };

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: theme.colors.bg }}>
      <ScrollView contentContainerStyle={{ padding: 20, gap: 16 }}>
        <Text style={{ ...theme.font.h1, color: theme.colors.text }}>Settings</Text>

        <Card>
          <Text style={{ color: theme.colors.text, fontWeight: '700', marginBottom: 12 }}>Streaming services</Text>
          <View style={{ gap: 8 }}>
            {PLATFORMS.map((p) => (
              <ServiceConnectCard key={p} platform={p} connected={connectedSet.has(p)} onPress={() => onPress(p)} />
            ))}
          </View>
        </Card>

        <Card>
          <Text style={{ color: theme.colors.text, fontWeight: '700', marginBottom: 8 }}>Account</Text>
          <Button title="Log out" variant="danger" onPress={() => logout()} />
        </Card>
      </ScrollView>
    </SafeAreaView>
  );
}
