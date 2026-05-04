import React from 'react';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import { WelcomeScreen } from '../screens/WelcomeScreen';
import { ConnectServicesScreen } from '../screens/ConnectServicesScreen';
import { PermissionsScreen } from '../screens/PermissionsScreen';

export type OnboardingParamList = {
  Welcome: undefined;
  ConnectServices: undefined;
  Permissions: undefined;
};

const Stack = createNativeStackNavigator<OnboardingParamList>();

export function OnboardingNavigator() {
  return (
    <Stack.Navigator screenOptions={{ headerShown: false }}>
      <Stack.Screen name="Welcome" component={WelcomeScreen} />
      <Stack.Screen name="ConnectServices" component={ConnectServicesScreen} />
      <Stack.Screen name="Permissions" component={PermissionsScreen} />
    </Stack.Navigator>
  );
}
