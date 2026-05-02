"""Shared Kalshi REST authentication helpers.

Kalshi authenticates every request (no fully unauthenticated read path
exists) by signing ``timestamp + method + path`` with an RSA-PSS-SHA256
private key.  The private key + key id are configured via ``settings``
(``kalshi_api_key_id``, ``kalshi_private_key_path``).

This module exists so both the Kalshi *feed* (``feeds.kalshi.KalshiFeed``)
and the Kalshi *executor* (``execution.orders.KalshiExecutor``) sign
requests through identical code paths.  Round 6 introduced the feed and
factored these helpers out of ``KalshiExecutor``; ``orders.py`` was
updated to import from here so there is exactly one source of truth.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

import structlog

logger: structlog.BoundLogger = structlog.get_logger(__name__)


def load_key(path: str | Path) -> Any | None:
    """Load an RSA private key from a PEM file.

    Returns the loaded key, or None on failure.  Failure is logged at
    warning level via ``kalshi_auth.key_load_failed`` so callers don't
    need their own error log; callers should treat None as
    "subsequent signed requests will be unauthenticated and fail".
    """
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        with open(path, "rb") as f:
            return load_pem_private_key(f.read(), password=None)
    except Exception as exc:
        logger.warning(
            "kalshi_auth.key_load_failed",
            path=str(path),
            error=str(exc),
        )
        return None


def signed_headers(method: str, path: str, key: Any, key_id: str) -> dict[str, str]:
    """Build Kalshi auth headers using RSA-PSS-SHA256.

    Args:
        method:  HTTP method (e.g. "GET", "POST", "DELETE").
        path:    Request path including any query string (e.g.
                 "/trade-api/v2/markets?series_ticker=KXBTC").
        key:     The loaded RSA private key (from :func:`load_key`).  If
                 None, returns an empty dict — caller's request will fail
                 auth, which is the right user-visible signal for a
                 misconfigured key path.
        key_id:  The Kalshi API key identifier (``settings.kalshi_api_key_id``).

    Returns:
        dict suitable for passing as ``headers=`` to httpx.
    """
    if key is None:
        return {}
    ts_ms = str(int(time.time() * 1000))
    msg = (ts_ms + method.upper() + path).encode()
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    sig = key.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }
