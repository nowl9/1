/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./App.tsx', './src/**/*.{ts,tsx}', '../../packages/ui/src/**/*.{ts,tsx}'],
  presets: [require('nativewind/preset')],
  theme: {
    extend: {
      colors: {
        bg: '#0A0E1A',
        elevated: '#13182A',
        primary: '#7B2FBE',
        accent: '#00D4AA',
      },
    },
  },
};
