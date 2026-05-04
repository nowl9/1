import React, { useState } from 'react';
import { Text, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Button, theme } from '@streambridge/ui';
import { useNavigation } from '@react-navigation/native';
import { useAuthStore } from '../stores/auth';

export function WelcomeScreen() {
  const nav = useNavigation();
  const login = useAuthStore((s) => s.login);
  const register = useAuthStore((s) => s.register);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [name, setName] = useState('');
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit() {
    setLoading(true);
    setErr(null);
    try {
      if (mode === 'login') await login(email, password);
      else await register(email, password, name || email.split('@')[0]!);
      nav.navigate('ConnectServices' as never);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: theme.colors.bg, padding: 24, justifyContent: 'center', gap: 16 }}>
      <Text style={{ ...theme.font.h1, color: theme.colors.text }}>StreamBridge</Text>
      <Text style={{ color: theme.colors.textMuted }}>One playlist library across every streaming service.</Text>
      <TextInput
        accessibilityLabel="Email"
        placeholder="Email"
        placeholderTextColor={theme.colors.textMuted}
        autoCapitalize="none"
        keyboardType="email-address"
        value={email}
        onChangeText={setEmail}
        style={inputStyle}
      />
      <TextInput
        accessibilityLabel="Password"
        placeholder="Password"
        placeholderTextColor={theme.colors.textMuted}
        secureTextEntry
        value={password}
        onChangeText={setPassword}
        style={inputStyle}
      />
      {mode === 'register' && (
        <TextInput
          accessibilityLabel="Display name"
          placeholder="Display name"
          placeholderTextColor={theme.colors.textMuted}
          value={name}
          onChangeText={setName}
          style={inputStyle}
        />
      )}
      {err && <Text style={{ color: theme.colors.danger }}>{err}</Text>}
      <Button title={mode === 'login' ? 'Log in' : 'Create account'} onPress={submit} loading={loading} />
      <Button
        title={mode === 'login' ? 'Need an account? Sign up' : 'Have an account? Log in'}
        variant="ghost"
        onPress={() => setMode(mode === 'login' ? 'register' : 'login')}
      />
    </SafeAreaView>
  );
}

const inputStyle = {
  backgroundColor: theme.colors.bgElevated,
  color: theme.colors.text,
  borderRadius: theme.radius.md,
  paddingHorizontal: 16,
  paddingVertical: 14,
  fontSize: 16,
};
