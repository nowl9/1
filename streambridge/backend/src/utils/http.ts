import axios, { AxiosError, AxiosInstance, AxiosRequestConfig } from 'axios';
import axiosRetry from 'axios-retry';
import type { Platform } from '@streambridge/types';
import { PlatformApiError } from './errors';

export interface PlatformHttpOptions {
  platform: Platform;
  baseURL: string;
  timeoutMs?: number;
  defaultHeaders?: Record<string, string>;
}

export function createPlatformHttp(opts: PlatformHttpOptions): AxiosInstance {
  const client = axios.create({
    baseURL: opts.baseURL,
    timeout: opts.timeoutMs ?? 15_000,
    headers: { Accept: 'application/json', ...(opts.defaultHeaders ?? {}) },
  });

  // Exponential backoff for transient errors and 429s. Honors Retry-After when present.
  axiosRetry(client, {
    retries: 5,
    retryDelay: (retryCount, error) => {
      const ra = retryAfterMs(error);
      if (ra !== undefined) return ra;
      return Math.min(30_000, 2 ** retryCount * 250 + Math.random() * 250);
    },
    retryCondition: (error) => {
      const s = error.response?.status ?? 0;
      return axiosRetry.isNetworkError(error) || s === 429 || (s >= 500 && s < 600);
    },
  });

  client.interceptors.response.use(
    (r) => r,
    (err: AxiosError) => {
      throw toPlatformError(opts.platform, err);
    },
  );

  return client;
}

function retryAfterMs(error: AxiosError): number | undefined {
  const ra = error.response?.headers?.['retry-after'];
  if (!ra) return undefined;
  const asNum = Number(ra);
  if (Number.isFinite(asNum)) return asNum * 1000;
  const date = Date.parse(String(ra));
  if (!Number.isNaN(date)) return Math.max(0, date - Date.now());
  return undefined;
}

export function toPlatformError(platform: Platform, err: unknown): PlatformApiError {
  if (axios.isAxiosError(err)) {
    const status = err.response?.status ?? 0;
    return new PlatformApiError({
      platform,
      status,
      code: status === 429 ? 'rate_limited' : status === 401 ? 'unauthorized' : 'platform_error',
      message: err.message,
      retryable: status === 429 || status >= 500,
      retryAfterMs: retryAfterMs(err),
    });
  }
  return new PlatformApiError({
    platform,
    status: 500,
    code: 'unknown',
    message: (err as Error)?.message ?? 'unknown platform error',
    retryable: false,
  });
}

export type { AxiosRequestConfig };
