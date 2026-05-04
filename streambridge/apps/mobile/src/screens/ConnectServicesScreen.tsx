import React from 'react';
import { Linking, ScrollView, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Button, ServiceConnectCard, theme } from '@streambridge/ui';
import { PLATFORMS, type Platform } from '@streambridge/types';
import { useConnections, useStartConnect } from '../hooks/queries';
import { useNavigation } from '@react-navigation/native';

export function ConnectServicesScreen() {
  const nav = useNavigation();
  const { data: connections = [] } = useConnections();
  const startConnect = useStartConnect();
  const connectedSet = new Set(connections.map((c) => c.platform));

  const handleConnect = async (p: Platform) => {
    const { authorizationUrl } = await startConnect.mutateAsync(p);
    await Linking.openURL(authorizationUrl);
  };

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: theme.colors.bg }}>
      <ScrollView contentContainerStyle={{ padding: 20, gap: 12 }}>
        <Text style={{ ...theme.font.h1, color: theme.colors.text }}>Connect your music</Text>
        <Text style={{ color: theme.colors.textMuted, marginBottom: 8 }}>
          Pick the streaming services you use. You can change this later in Settings.
        </Text>
        {PLATFORMS.map((p) => (
          <ServiceConnectCard key={p} platform={p} connected={connectedSet.has(p)} onPress={() => handleConnect(p)} />
        ))}
        <View style={{ marginTop: 16 }}>
          <Button title="Continue" onPress={() => nav.navigate('Permissions' as never)} />
        </View>
      </ScrollView>
    </SafeAreaView>
  );
}
