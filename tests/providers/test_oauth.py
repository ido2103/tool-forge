"""OAuth helper tests — token freshness, refresh rotation, atomic write-back."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
import respx

from toolforge.providers.oauth_anthropic import (
    _atomic_write_creds,
    read_or_refresh,
    refresh_anthropic_oauth,
)

_PRIMARY_URL = "https://platform.claude.com/v1/oauth/token"
_FALLBACK_URL = "https://console.anthropic.com/v1/oauth/token"


def _write_creds(path: Path, *, expires_in_ms: int) -> None:
    path.write_text(
        json.dumps(
            {
                "accessToken": "tok-old",
                "refreshToken": "ref-old",
                "expiresAt": int(time.time() * 1000) + expires_in_ms,
            }
        )
    )
    path.chmod(0o600)


def test_fresh_token_returned_without_refresh(tmp_path: Path) -> None:
    creds = tmp_path / "creds.json"
    _write_creds(creds, expires_in_ms=3_600_000)
    # No respx routes active — any HTTP call would raise.
    assert read_or_refresh(creds) == "tok-old"


def test_stale_token_refreshed_and_persisted(tmp_path: Path, respx_mock: respx.MockRouter) -> None:
    creds = tmp_path / "creds.json"
    _write_creds(creds, expires_in_ms=-1000)
    respx_mock.post(_PRIMARY_URL).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "tok-new", "refresh_token": "ref-new", "expires_in": 3600},
        )
    )

    assert read_or_refresh(creds) == "tok-new"

    stored = json.loads(creds.read_text())
    assert stored["accessToken"] == "tok-new"
    assert stored["refreshToken"] == "ref-new"
    assert stored["expiresAt"] > int(time.time() * 1000)
    assert creds.stat().st_mode & 0o777 == 0o600


def test_refresh_falls_back_to_console_endpoint(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(_PRIMARY_URL).mock(return_value=httpx.Response(500))
    respx_mock.post(_FALLBACK_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "tok-fb", "refresh_token": "ref-fb", "expires_in": 60}
        )
    )
    result = refresh_anthropic_oauth("ref-old")
    assert result["accessToken"] == "tok-fb"


def test_refresh_raises_when_all_endpoints_fail(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(_PRIMARY_URL).mock(return_value=httpx.Response(500))
    respx_mock.post(_FALLBACK_URL).mock(return_value=httpx.Response(502))
    with pytest.raises(httpx.HTTPStatusError):
        refresh_anthropic_oauth("ref-old")


def test_refresh_missing_access_token_raises(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(_PRIMARY_URL).mock(return_value=httpx.Response(200, json={"nope": True}))
    with pytest.raises(ValueError, match="missing access_token"):
        refresh_anthropic_oauth("ref-old")


def test_refresh_requires_refresh_token() -> None:
    with pytest.raises(ValueError, match="refresh_token is required"):
        refresh_anthropic_oauth("")


def test_atomic_write_sets_0600(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    _atomic_write_creds(path, {"accessToken": "x", "refreshToken": "y", "expiresAt": 1})
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["accessToken"] == "x"
    assert not path.with_suffix(".json.tmp").exists()
