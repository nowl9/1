import React, { useState } from 'react';
import { ScrollView, Text, TextInput, View } from 'react-native';
import { Button, ServiceConnectCard, theme } from '@streambridge/ui';
import { useNavigation } from '@react-navigation/native';
import { PLATFORMS, type Platform } from '@streambridge/types';
import { useConnections } from '../hooks/queries';

export function CreatePlaylistModal() {
  const nav = useNavigation();
  const { data: connections = [] } = useConnections();
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [source, setSource] = useState<Platform | null>(null);

  return (
    <ScrollView style={{ flex: 1, backgroundColor: theme.colors.bg }} contentContainerStyle={{ padding: 16, gap: 12 }}>
      <Text style={{ color: theme.colors.text, fontWeight: '700' }}>Playlist name</Text>
      <TextInput
        accessibilityLabel="Playlist name"
        placeholder="My new playlist"
        placeholderTextColor={theme.colors.textMuted}
        value={name}
        onChangeText={setName}
        style={inputStyle}
      />
      <Text style={{ color: theme.colors.text, fontWeight: '700' }}>Description (optional)</Text>
      <TextInput
        accessibilityLabel="Description"
        placeholder="What's this playlist about?"
        placeholderTextColor={theme.colors.textMuted}
        value={description}
        onChangeText={setDescription}
        style={[inputStyle, { minHeight: 80 }]}
        multiline
      />
      <Text style={{ color: theme.colors.text, fontWeight: '700' }}>Source platform</Text>
      <View style={{ gap: 8 }}>
        {PLATFORMS.map((p) => {
          const eligible = connections.some((c) => c.platform === p);
          if (!eligible) return null;
          return (
            <ServiceConnectCard
              key={p}
              platform={p}
              connected={source === p}
              onPress={() => setSource(p)}
            />
          );
        })}
      </View>
      <View style={{ marginTop: 16 }}>
        <Button title="Create" disabled={!name || !source} onPress={() => nav.goBack()} />
      </View>
    </ScrollView>
  );
}

const inputStyle = {
  backgroundColor: theme.colors.bgElevated,
  color: theme.colors.text,
  paddingHorizontal: 16,
  paddingVertical: 14,
  borderRadius: theme.radius.md,
  fontSize: 16,
};
