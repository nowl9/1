import React, { useMemo, useState } from 'react';
import { Pressable, ScrollView, Text, View } from 'react-native';
import { Button, Card, PlatformBadge, TrackRow, theme } from '@streambridge/ui';
import type { RouteProp } from '@react-navigation/native';
import { useNavigation, useRoute } from '@react-navigation/native';
import { usePlaylist, useTrackSearch } from '../hooks/queries';
import { api } from '../lib/api';
import type { RootStackParamList } from '../navigation/RootNavigator';
import type { Platform, UniversalTrack } from '@streambridge/types';

export function TrackMatchReviewModal() {
  const route = useRoute<RouteProp<RootStackParamList, 'TrackMatchReviewModal'>>();
  const nav = useNavigation();
  const { data: playlist } = usePlaylist(route.params.playlistId);
  const [cursor, setCursor] = useState(0);
  const sourceTrack = playlist?.tracks[cursor];
  const [overrideQuery, setOverrideQuery] = useState('');
  const platform: Platform | null = useMemo(() => {
    const targets = Object.keys((playlist as unknown as { perPlatform?: Record<string, unknown> })?.perPlatform ?? {});
    return (targets[0] as Platform) ?? null;
  }, [playlist]);
  const { data = [] } = useTrackSearch(overrideQuery || (sourceTrack ? `${sourceTrack.title} ${sourceTrack.artist[0] ?? ''}` : ''), platform ?? undefined);

  if (!sourceTrack) {
    return (
      <View style={{ flex: 1, backgroundColor: theme.colors.bg, padding: 20 }}>
        <Text style={{ color: theme.colors.text }}>No tracks need review.</Text>
      </View>
    );
  }

  async function pick(candidate: UniversalTrack, p: Platform) {
    if (!sourceTrack) return;
    await api.overrideMatch({ sourceTrackId: sourceTrack.id, candidateTrackId: candidate.id, platform: p });
    if (cursor + 1 >= (playlist?.tracks.length ?? 0)) nav.goBack();
    else setCursor(cursor + 1);
  }

  return (
    <ScrollView style={{ flex: 1, backgroundColor: theme.colors.bg }} contentContainerStyle={{ padding: 16, gap: 12 }}>
      <Card>
        <Text style={{ color: theme.colors.textMuted, fontSize: 12 }}>Source</Text>
        <TrackRow track={sourceTrack} />
      </Card>
      <Text style={{ color: theme.colors.text, fontWeight: '700' }}>Pick the best match</Text>
      {data.map((r) =>
        r.ok ? (
          <Card key={r.platform}>
            <View style={{ marginBottom: 8 }}>
              <PlatformBadge platform={r.platform} />
            </View>
            {r.tracks.map((t) => (
              <Pressable key={t.id} onPress={() => pick(t, r.platform)}>
                <TrackRow track={t} status="review" />
              </Pressable>
            ))}
          </Card>
        ) : null,
      )}
      <Button title="Skip" variant="ghost" onPress={() => setCursor(cursor + 1)} />
    </ScrollView>
  );
}
