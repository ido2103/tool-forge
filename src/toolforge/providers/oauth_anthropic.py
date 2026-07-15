"""Anthropic OAuth helpers — refresh + per-call token resolution.

``refresh_anthropic_oauth`` performs single-use refresh-token rotation;
``read_or_refresh`` is the per-call entry point used by the AnthropicClient
adapter.

Ported from Zeemon ``providers/oauth_anthropic.py`` (itself grafted from the
Hermes agent). Dropped the PKCE provisioning flow — credentials are provisioned
externally; the JSON file shape ``{"accessToken", "refreshToken", "expiresAt"}``
(epoch ms) is unchanged, so a Zeemon-provisioned file works as-is.

Caveat: refresh tokens are single-use. Two processes refreshing the same
credentials file (e.g. Zeemon and toolforge sharing one) can race and
invalidate each other's token pair.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

ANTHROPIC_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_TOKEN_REFRESH_URL = "https://platform.claude.com/v1/oauth/token"
_TOKEN_REFRESH_FALLBACK_URL = "https://console.anthropic.com/v1/oauth/token"


def refresh_anthropic_oauth(refresh_token: str) -> dict[str, Any]:
    """Refresh the Anthropic OAuth token. Single-use refresh discipline.

    Tries ``_TOKEN_REFRESH_URL`` first, falls back to
    ``_TOKEN_REFRESH_FALLBACK_URL``. Returns
    ``{"accessToken", "refreshToken", "expiresAt"}`` (epoch ms).
    """
    if not refresh_token:
        raise ValueError("refresh_token is required")

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": ANTHROPIC_OAUTH_CLIENT_ID,
    }

    token_endpoints = [_TOKEN_REFRESH_URL, _TOKEN_REFRESH_FALLBACK_URL]
    last_error: Exception | None = None

    for endpoint in token_endpoints:
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(
                    endpoint,
                    data=payload,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                resp.raise_for_status()
                result: dict[str, Any] = resp.json()
        except Exception as exc:
            last_error = exc
            logger.debug("Anthropic token refresh failed endpoint=%s error=%s", endpoint, exc)
            continue

        access_token = result.get("access_token", "")
        if not access_token:
            raise ValueError("Anthropic refresh response was missing access_token")

        next_refresh = result.get("refresh_token", refresh_token)
        expires_in = result.get("expires_in", 3600)
        expires_at_ms = int(time.time() * 1000) + (int(expires_in) * 1000)
        return {
            "accessToken": access_token,
            "refreshToken": next_refresh,
            "expiresAt": expires_at_ms,
        }

    if last_error is not None:
        raise last_error
    raise ValueError("Anthropic token refresh failed")


def read_or_refresh(credentials_path: Path) -> str:
    """Read credentials from disk, refresh if stale, return the access token.

    Atomic write-back via temp-file + ``os.replace``; preserves ``0o600`` mode.
    """
    creds: dict[str, Any] = json.loads(credentials_path.read_text())
    expires_at_ms = int(creds.get("expiresAt", 0))
    if (expires_at_ms - int(time.time() * 1000)) < 60_000:
        creds = refresh_anthropic_oauth(str(creds["refreshToken"]))
        _atomic_write_creds(credentials_path, creds)
    return str(creds["accessToken"])


def _atomic_write_creds(path: Path, creds: dict[str, Any]) -> None:
    """Atomically write credentials to disk, preserving 0o600 permissions."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(creds))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
