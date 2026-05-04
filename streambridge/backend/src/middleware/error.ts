import type { ErrorRequestHandler } from 'express';
import { ZodError } from 'zod';
import { AppError, PlatformApiError } from '../utils/errors';
import { logger } from '../utils/logger';

export const errorHandler: ErrorRequestHandler = (err, req, res, _next) => {
  if (err instanceof PlatformApiError) {
    logger.warn({ err, path: req.path }, 'platform api error');
    res.status(err.status || 502).json({
      error: { code: err.code, message: err.message, platform: err.platform, retryable: err.retryable },
    });
    return;
  }
  if (err instanceof AppError) {
    res.status(err.status).json({ error: { code: err.code, message: err.message, details: err.details } });
    return;
  }
  if (err instanceof ZodError) {
    res.status(400).json({ error: { code: 'validation_error', message: 'Invalid input', details: err.flatten() } });
    return;
  }
  logger.error({ err, path: req.path }, 'unhandled error');
  res.status(500).json({ error: { code: 'internal_error', message: 'Internal server error' } });
};
