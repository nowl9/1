import React, { useState } from 'react';
import { ScrollView, Text, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Card, PlatformBadge, TrackRow, theme } from '@streambridge/ui';
import { useTrackSearch } from '../hooks/queries';

export function DiscoverScreen() {
  const [q, setQ] = useState('');
  const { data = [], isFetching } = useTrackSearch(q);

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: theme.colors.bg }}>
      <ScrollView contentContainerStyle={{ padding: 20, gap: 16 }} keyboardShouldPersistTaps="handled">
        <Text style={{ ...theme.font.h1, color: theme.colors.text }}>Discover</Text>
        <TextInput
          accessibilityLabel="Search tracks"
          placeholder="Search across all your services"
          placeholderTextColor={theme.colors.textMuted}
          value={q}
          onChangeText={setQ}
          style={{
            backgroundColor: theme.colors.bgElevated,
            color: theme.colors.text,
            paddingHorizontal: 16,
            paddingVertical: 14,
            borderRadius: theme.radius.md,
            fontSize: 16,
          }}
        />
        {isFetching && <Text style={{ color: theme.colors.textMuted }}>Searching…</Text>}
        {data.map((r) =>
          r.ok ? (
            <Card key={r.platform}>
              <View style={{ marginBottom: 8, flexDirection: 'row' }}>
                <PlatformBadge platform={r.platform} />
              </View>
              {r.tracks.length === 0 ? (
                <Text style={{ color: theme.colors.textMuted }}>No matches</Text>
              ) : (
                r.tracks.slice(0, 5).map((t) => <TrackRow key={t.id} track={t} />)
              )}
            </Card>
          ) : null,
        )}
      </ScrollView>
    </SafeAreaView>
  );
}
