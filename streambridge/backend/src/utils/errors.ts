import type { Platform, PlatformError } from '@streambridge/types';

export class AppError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly details?: unknown,
  ) {
    super(message);
    this.name = 'AppError';
  }
}

export class PlatformApiError extends AppError implements PlatformError {
  retryable: boolean;
  retryAfterMs?: number;
  platform: Platform;

  constructor(args: {
    platform: Platform;
    status: number;
    code: string;
    message: string;
    retryable: boolean;
    retryAfterMs?: number;
  }) {
    super(args.status, args.code, args.message);
    this.name = 'PlatformApiError';
    this.platform = args.platform;
    this.retryable = args.retryable;
    if (args.retryAfterMs !== undefined) this.retryAfterMs = args.retryAfterMs;
  }
}

export class AuthError extends AppError {
  constructor(message: string, code = 'unauthorized') {
    super(401, code, message);
  }
}

export class NotFoundError extends AppError {
  constructor(resource: string) {
    super(404, 'not_found', `${resource} not found`);
  }
}

export class ValidationError extends AppError {
  constructor(message: string, details?: unknown) {
    super(400, 'validation_error', message, details);
  }
}
