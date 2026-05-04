import { PLATFORM_BRAND_COLORS } from '@streambridge/types';
export const theme = {
  colors: {
    bg: '#0A0E1A',
    bgElevated: '#13182A',
    primary: '#7B2FBE',
    accent: '#00D4AA',
    text: '#FFFFFF',
    textMuted: 'rgba(255,255,255,0.65)',
    danger: '#FF4D6D',
    border: 'rgba(255,255,255,0.08)',
    platform: PLATFORM_BRAND_COLORS,
  },
  radius: { sm: 8, md: 12, lg: 20, pill: 999 },
  spacing: (n: number) => n * 4,
  font: {
    h1: { fontSize: 28, fontWeight: '700' as const },
    h2: { fontSize: 22, fontWeight: '700' as const },
    body: { fontSize: 15, fontWeight: '400' as const },
    caption: { fontSize: 12, fontWeight: '500' as const },
  },
};
export type Theme = typeof theme;
