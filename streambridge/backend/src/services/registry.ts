import type { Platform } from '@streambridge/types';
import type { PlatformService } from './platform';
import { spotifyService } from './spotify.service';
import { appleMusicService } from './applemusic.service';
import { amazonMusicService } from './amazonmusic.service';
import { tidalService } from './tidal.service';
import { youtubeMusicService } from './youtubemusic.service';
import { deezerService } from './deezer.service';
import { pandoraService } from './pandora.service';

const SERVICES: Record<Platform, PlatformService> = {
  spotify: spotifyService,
  'apple-music': appleMusicService,
  'amazon-music': amazonMusicService,
  tidal: tidalService,
  'youtube-music': youtubeMusicService,
  deezer: deezerService,
  pandora: pandoraService,
};

export function getService(platform: Platform): PlatformService {
  return SERVICES[platform];
}

export function allServices(): PlatformService[] {
  return Object.values(SERVICES);
}
