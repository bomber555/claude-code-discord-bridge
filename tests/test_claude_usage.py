"""Tests for claude_usage — fetching and formatting Anthropic rate-limit usage.

Mirrors tests/test_codex_usage.py: the formatting half is pure logic driven by
literal payloads, and the fetching half patches urllib so no network call is
ever made.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from claude_discord.discord_ui.claude_usage import (
    build_claude_usage_lines,
    fetch_claude_usage,
)

_MOD = "claude_discord.discord_ui.claude_usage"

# 2026-07-29T10:49:00+00:00
_NOW = 1785322140

_PAYLOAD: dict[str, Any] = {
    "five_hour": {
        "utilization": 47.0,
        "resets_at": "2026-07-29T13:29:59.665663+00:00",
    },
    "seven_day": {
        "utilization": 53.0,
        "resets_at": "2026-07-30T12:59:59.665688+00:00",
    },
    "seven_day_opus": None,
    "extra_usage": {
        "is_enabled": False,
        "monthly_limit": 20000,
        "used_credits": 15720.0,
        "utilization": 78.6,
        "currency": "USD",
        "decimal_places": 2,
        "disabled_reason": "out_of_credits",
    },
}


# ---------------------------------------------------------------------------
# build_claude_usage_lines
# ---------------------------------------------------------------------------


def test_builds_five_hour_and_weekly_lines() -> None:
    lines = build_claude_usage_lines(_PAYLOAD, now=_NOW)
    # Extra usage is switched off in _PAYLOAD, so only the two windows show.
    assert len(lines) == 2
    assert lines[0] == "5時間残量：53%（リセットまで2時間41分）"
    assert lines[1] == "週間残量：47%（リセットまで1日2時間11分）"


def test_renders_no_block_meter() -> None:
    """Block-character bars read as glare in a dark client — text only."""
    for line in build_claude_usage_lines(_PAYLOAD, now=_NOW):
        assert "█" not in line
        assert "░" not in line


def test_percentage_is_consumption_not_headroom() -> None:
    """The card shows headroom, matching the Codex completion card."""
    untouched = {"five_hour": {"utilization": 0, "resets_at": None}}
    assert build_claude_usage_lines(untouched, now=_NOW)[0] == "5時間残量：100%"

    exhausted = {"five_hour": {"utilization": 100, "resets_at": None}}
    assert build_claude_usage_lines(exhausted, now=_NOW)[0] == "5時間残量：0%"


def test_hides_credits_when_extra_usage_is_off() -> None:
    """Nothing can be spent through a disabled pool — the line is just noise."""
    lines = build_claude_usage_lines(_PAYLOAD, now=_NOW)
    assert not any(line.startswith("\U0001f4b3") for line in lines)
    assert not any("157.20" in line for line in lines)


def test_extra_credits_line_when_enabled() -> None:
    payload = {
        **_PAYLOAD,
        "extra_usage": {
            "is_enabled": True,
            "monthly_limit": 20000,
            "used_credits": 15720.0,
            "utilization": 78.6,
            "currency": "USD",
            "decimal_places": 2,
            "disabled_reason": None,
        },
    }
    line = build_claude_usage_lines(payload, now=_NOW)[2]
    assert line.startswith("追加利用：78%使用")
    assert "$157.20" in line
    assert "$200.00" in line


def test_skips_windows_without_utilization() -> None:
    payload = {"five_hour": {"utilization": None, "resets_at": None}, "seven_day": None}
    assert build_claude_usage_lines(payload, now=_NOW) == []


def test_skips_extra_usage_never_used() -> None:
    payload = {
        "five_hour": _PAYLOAD["five_hour"],
        "extra_usage": {"is_enabled": False, "used_credits": 0, "monthly_limit": 20000},
    }
    lines = build_claude_usage_lines(payload, now=_NOW)
    assert len(lines) == 1


def test_skips_enabled_pool_with_unusable_figures() -> None:
    payload = {
        "five_hour": _PAYLOAD["five_hour"],
        "extra_usage": {"is_enabled": True, "used_credits": None, "monthly_limit": None},
    }
    assert len(build_claude_usage_lines(payload, now=_NOW)) == 1


def test_accepts_epoch_seconds_for_resets_at() -> None:
    """The CLI seeds these windows from response headers, which use epoch seconds."""
    payload = {"five_hour": {"utilization": 10, "resets_at": _NOW + 3600}}
    assert "リセットまで1時間" in build_claude_usage_lines(payload, now=_NOW)[0]


def test_handles_z_suffix_timestamps() -> None:
    payload = {"five_hour": {"utilization": 10, "resets_at": "2026-07-29T11:49:00Z"}}
    assert "リセットまで1時間" in build_claude_usage_lines(payload, now=_NOW)[0]


def test_unknown_and_elapsed_resets() -> None:
    unknown = {"five_hour": {"utilization": 10, "resets_at": None}}
    assert build_claude_usage_lines(unknown, now=_NOW)[0] == "5時間残量：90%"

    past = {"five_hour": {"utilization": 10, "resets_at": _NOW - 5}}
    assert "リセットまで0分" in build_claude_usage_lines(past, now=_NOW)[0]

    garbage = {"five_hour": {"utilization": 10, "resets_at": "not-a-date"}}
    assert build_claude_usage_lines(garbage, now=_NOW)[0] == "5時間残量：90%"


def test_minutes_only_countdown() -> None:
    payload = {"five_hour": {"utilization": 10, "resets_at": _NOW + 1800}}
    assert "リセットまで30分" in build_claude_usage_lines(payload, now=_NOW)[0]


def test_tolerates_garbage_payload() -> None:
    assert build_claude_usage_lines({}, now=_NOW) == []
    assert build_claude_usage_lines({"five_hour": "nope"}, now=_NOW) == []


# ---------------------------------------------------------------------------
# fetch_claude_usage
# ---------------------------------------------------------------------------


def _write_credentials(tmp_path: Path, *, expires_at_ms: int | None = None) -> Path:
    path = tmp_path / ".credentials.json"
    oauth: dict[str, Any] = {"accessToken": "tok-123"}
    if expires_at_ms is not None:
        oauth["expiresAt"] = expires_at_ms
    path.write_text(json.dumps({"claudeAiOauth": oauth}), encoding="utf-8")
    return path


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


async def test_fetch_returns_payload_and_caches(tmp_path: Path) -> None:
    creds = _write_credentials(tmp_path)
    cache = tmp_path / "cache.json"
    body = json.dumps(_PAYLOAD).encode("utf-8")

    with patch(f"{_MOD}.urlopen", return_value=_FakeResponse(body)) as opener:
        result = await fetch_claude_usage(credentials_path=creds, cache_path=cache)

    assert result is not None
    assert result["five_hour"]["utilization"] == 47.0
    assert cache.exists()

    request = opener.call_args.args[0]
    assert request.full_url.endswith("/api/oauth/usage")
    assert request.get_header("Authorization") == "Bearer tok-123"
    assert request.get_method() == "GET"


async def test_fetch_uses_cache_without_network(tmp_path: Path) -> None:
    creds = _write_credentials(tmp_path)
    cache = tmp_path / "cache.json"
    cache.write_text(
        json.dumps({"fetched_at": 9e12, "data": {"five_hour": {"utilization": 1}}}),
        encoding="utf-8",
    )

    with patch(f"{_MOD}.urlopen", side_effect=AssertionError("must not call")) as opener:
        result = await fetch_claude_usage(credentials_path=creds, cache_path=cache)

    assert result == {"five_hour": {"utilization": 1}}
    opener.assert_not_called()


async def test_fetch_ignores_stale_cache(tmp_path: Path) -> None:
    creds = _write_credentials(tmp_path)
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"fetched_at": 0, "data": {"five_hour": {}}}), encoding="utf-8")
    body = json.dumps(_PAYLOAD).encode("utf-8")

    with patch(f"{_MOD}.urlopen", return_value=_FakeResponse(body)):
        result = await fetch_claude_usage(credentials_path=creds, cache_path=cache)

    assert result is not None
    assert result["five_hour"]["utilization"] == 47.0


@pytest.mark.parametrize(
    "error",
    [OSError("boom"), ValueError("bad json")],
)
async def test_fetch_returns_none_on_error(tmp_path: Path, error: Exception) -> None:
    creds = _write_credentials(tmp_path)
    with patch(f"{_MOD}.urlopen", side_effect=error):
        assert (
            await fetch_claude_usage(credentials_path=creds, cache_path=tmp_path / "c.json") is None
        )


async def test_fetch_returns_none_without_credentials(tmp_path: Path) -> None:
    with patch(f"{_MOD}.urlopen", side_effect=AssertionError("must not call")):
        result = await fetch_claude_usage(
            credentials_path=tmp_path / "missing.json",
            cache_path=tmp_path / "c.json",
        )
    assert result is None


async def test_fetch_skips_expired_token(tmp_path: Path) -> None:
    """An expired access token can only 401 — don't spend a request on it."""
    creds = _write_credentials(tmp_path, expires_at_ms=1000)
    with patch(f"{_MOD}.urlopen", side_effect=AssertionError("must not call")):
        result = await fetch_claude_usage(
            credentials_path=creds,
            cache_path=tmp_path / "c.json",
        )
    assert result is None


async def test_fetch_rejects_non_dict_body(tmp_path: Path) -> None:
    creds = _write_credentials(tmp_path)
    with patch(f"{_MOD}.urlopen", return_value=_FakeResponse(b"[1, 2, 3]")):
        result = await fetch_claude_usage(
            credentials_path=creds,
            cache_path=tmp_path / "c.json",
        )
    assert result is None
