import React from 'react';
import { ScrollView, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Card, PlatformBadge, ProgressBar, theme } from '@streambridge/ui';
import { PLATFORM_DISPLAY_NAMES } from '@streambridge/types';
import { useSyncHistory } from '../hooks/queries';

export function SyncScreen() {
  const { data: jobs = [], refetch, isFetching } = useSyncHistory();
  const inFlight = jobs.filter((j) => j.status !== 'completed' && j.status !== 'failed' && j.status !== 'partial');
  const recent = jobs.filter((j) => !inFlight.includes(j)).slice(0, 20);

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: theme.colors.bg }}>
      <ScrollView contentContainerStyle={{ padding: 20, gap: 16 }} refreshing={isFetching} onScroll={() => {}}>
        <Text style={{ ...theme.font.h1, color: theme.colors.text }}>Sync</Text>

        <Text style={{ color: theme.colors.textMuted, fontWeight: '600' }}>In progress</Text>
        {inFlight.length === 0 ? (
          <Text style={{ color: theme.colors.textMuted }}>No active syncs.</Text>
        ) : (
          inFlight.map((j) => (
            <Card key={j.id}>
              <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
                <PlatformBadge platform={j.options.sourcePlatform} />
                <Text style={{ color: theme.colors.text }}>→ {j.options.targetPlatforms.length} platform(s)</Text>
              </View>
              <Text style={{ color: theme.colors.textMuted, marginTop: 8, marginBottom: 8 }}>
                {j.matched}/{j.total} matched · {j.unavailable} unavailable
              </Text>
              <ProgressBar progress={j.progress} />
              {j.options.targetPlatforms.map((p) => {
                const pp = j.perPlatform[p];
                if (!pp) return null;
                return (
                  <Text key={p} style={{ color: theme.colors.textMuted, fontSize: 12, marginTop: 6 }}>
                    {PLATFORM_DISPLAY_NAMES[p]}: {pp.matched}/{pp.total} matched · {pp.status}
                  </Text>
                );
              })}
            </Card>
          ))
        )}

        <Text style={{ color: theme.colors.textMuted, fontWeight: '600', marginTop: 8 }}>History</Text>
        {recent.map((j) => (
          <Card key={j.id}>
            <View style={{ flexDirection: 'row', justifyContent: 'space-between' }}>
              <PlatformBadge platform={j.options.sourcePlatform} />
              <Text style={{ color: j.status === 'failed' ? theme.colors.danger : theme.colors.accent }}>
                {j.status}
              </Text>
            </View>
            <Text style={{ color: theme.colors.text, marginTop: 8 }}>{j.matched}/{j.total} matched</Text>
            {j.error && <Text style={{ color: theme.colors.danger, marginTop: 4 }}>{j.error}</Text>}
          </Card>
        ))}
      </ScrollView>
    </SafeAreaView>
  );
}
