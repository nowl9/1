import React, { useMemo, useState } from 'react';
import { ScrollView, Text, View } from 'react-native';
import { Button, ServiceConnectCard, theme } from '@streambridge/ui';
import type { RouteProp } from '@react-navigation/native';
import { useNavigation, useRoute } from '@react-navigation/native';
import { PLATFORMS, type Platform } from '@streambridge/types';
import { useConnections, useStartSync } from '../hooks/queries';
import type { RootStackParamList } from '../navigation/RootNavigator';

export function PlatformPickerModal() {
  const route = useRoute<RouteProp<RootStackParamList, 'PlatformPickerModal'>>();
  const nav = useNavigation();
  const { data: connections = [] } = useConnections();
  const startSync = useStartSync();
  const eligible = useMemo(
    () => PLATFORMS.filter((p) => p !== route.params.sourcePlatform && connections.some((c) => c.platform === p)),
    [connections, route.params.sourcePlatform],
  );
  const [selected, setSelected] = useState<Set<Platform>>(new Set());

  const toggle = (p: Platform) => {
    const next = new Set(selected);
    if (next.has(p)) next.delete(p); else next.add(p);
    setSelected(next);
  };

  async function start() {
    await startSync.mutateAsync({
      sourcePlatform: route.params.sourcePlatform,
      targetPlatforms: Array.from(selected),
      playlistId: route.params.playlistId,
      conflictStrategy: 'source-wins',
      skipUnavailable: true,
      createIfMissing: true,
    });
    nav.goBack();
  }

  return (
    <ScrollView style={{ flex: 1, backgroundColor: theme.colors.bg }} contentContainerStyle={{ padding: 16, gap: 10 }}>
      <Text style={{ color: theme.colors.text, fontWeight: '700' }}>Sync this playlist to:</Text>
      {eligible.length === 0 && (
        <Text style={{ color: theme.colors.textMuted }}>No other services connected.</Text>
      )}
      {eligible.map((p) => (
        <ServiceConnectCard key={p} platform={p} connected={selected.has(p)} onPress={() => toggle(p)} />
      ))}
      <View style={{ marginTop: 16 }}>
        <Button title={`Start sync (${selected.size})`} onPress={start} disabled={selected.size === 0} loading={startSync.isPending} />
      </View>
    </ScrollView>
  );
}
