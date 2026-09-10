"""Helpers for fetching and formatting Anthropic rate-limit usage.

The Claude Code CLI surfaces the 5-hour and weekly quota windows through its
interactive ``/usage`` view, which a headless ``claude -p`` run can't reach.
The bridge therefore reads the same source the CLI does — the account usage
endpoint, authenticated with the OAuth token the CLI already stores in
``~/.claude/.credentials.json``.

This is deliberately *not* an inference call: it fetches account metadata only,
costs no tokens, and is cached on disk so a burst of sessions ending together
results in a single request. Every failure path returns ``None`` — the footer
must never be lost because the endpoint is slow or the token has rotated.

Rendering is deliberately text-only — no block-character meter. The bars read
as glare in a dark Discord client, and a bare percentage is ambiguous about
which direction it runs, so every figure is spelled out as ``used N%``:
0% is an untouched window, 100% is an exhausted one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_OAUTH_BETA = "oauth-2025-04-20"

_TIMEOUT_SECONDS = 10.0
_CACHE_MAX_AGE_SECONDS = 60
_CACHE_PATH = Path.home() / ".cache" / "ccdb" / "claude-usage-cache.json"
_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"

# Values that turn the footer off via CCDB_USAGE_FOOTER.
_FALSEY = frozenset({"0", "false", "no", "off"})

# Quota windows worth a line, in display order.
_WINDOWS = (("five_hour", "⏱ 5h"), ("seven_day", "\U0001f4c5 7d"))


def usage_footer_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return False when ``CCDB_USAGE_FOOTER`` opts out of the usage lines."""
    source = os.environ if env is None else env
    return (source.get("CCDB_USAGE_FOOTER") or "").strip().lower() not in _FALSEY


def _parse_resets_at(value: object) -> float | None:
    """Accept both the ISO-8601 string and the epoch seconds the CLI uses."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _format_countdown(resets_at: object, now: int | None = None) -> str:
    epoch = _parse_resets_at(resets_at)
    if epoch is None:
        return "reset unknown"
    current = time.time() if now is None else now
    remaining = int(epoch - current)
    if remaining <= 0:
        return "resetting now"
    days, rest = divmod(remaining, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days > 0:
        return f"resets in {days}d {hours}h"
    if hours > 0:
        return f"resets in {hours}h {minutes}m"
    return f"resets in {minutes}m"


def _window_line(window: object, label: str, now: int | None) -> str | None:
    """Render one quota window. ``utilization`` is consumption, not headroom."""
    if not isinstance(window, dict):
        return None
    utilization = window.get("utilization")
    if not isinstance(utilization, (int, float)) or isinstance(utilization, bool):
        return None
    used = int(utilization)
    countdown = _format_countdown(window.get("resets_at"), now=now)
    return f"{label}  used {used}% — {countdown}"


def _format_money(minor_units: float, decimal_places: int, currency: str) -> str:
    amount = minor_units / (10**decimal_places)
    rendered = f"{amount:.{decimal_places}f}"
    return f"${rendered}" if currency == "USD" else f"{rendered} {currency}"


def _extra_usage_line(extra: object) -> str | None:
    """Render the pay-as-you-go credit pool that backs the plan limits.

    Only while the pool is switched on. Switched off, nothing can be spent
    through it, so the line is noise on every single session footer.
    """
    if not isinstance(extra, dict):
        return None
    if not extra.get("is_enabled"):
        return None
    used = extra.get("used_credits")
    limit = extra.get("monthly_limit")
    if not isinstance(used, (int, float)) or not isinstance(limit, (int, float)):
        return None

    places = extra.get("decimal_places")
    places = places if isinstance(places, int) and 0 <= places <= 6 else 2
    currency = extra.get("currency") or "USD"

    utilization = extra.get("utilization")
    if isinstance(utilization, (int, float)) and not isinstance(utilization, bool):
        percent = int(utilization)
    else:
        percent = int(used / limit * 100) if limit else 0

    spend = f"{_format_money(used, places, currency)} / {_format_money(limit, places, currency)}"
    return f"\U0001f4b3 credits  used {percent}%  {spend}"


def build_claude_usage_lines(payload: Mapping[str, Any], now: int | None = None) -> list[str]:
    """Render a ``/api/oauth/usage`` payload into Discord-friendly text lines."""
    lines: list[str] = []
    for key, label in _WINDOWS:
        line = _window_line(payload.get(key), label, now)
        if line:
            lines.append(line)
    credits_line = _extra_usage_line(payload.get("extra_usage"))
    if credits_line:
        lines.append(credits_line)
    return lines


def _read_access_token(path: Path) -> str | None:
    """Return the CLI's current OAuth access token, if it is still usable.

    Read fresh on every call so a token the CLI has since refreshed is picked
    up without restarting the bridge.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        return None
    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, (int, float)) and expires_at / 1000 <= time.time():
        logger.debug("Claude access token expired; skipping usage fetch")
        return None
    return token


def _load_cache(path: Path, max_age_seconds: int) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    fetched_at = raw.get("fetched_at") if isinstance(raw, dict) else None
    data = raw.get("data") if isinstance(raw, dict) else None
    if not isinstance(fetched_at, (int, float)) or not isinstance(data, dict):
        return None
    if time.time() - fetched_at > max_age_seconds:
        return None
    return data


def _write_cache(path: Path, data: dict[str, Any]) -> None:
    """Cache the payload owner-only — it carries the account's spend figures."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched_at": time.time(), "data": data}), encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        logger.debug("Failed to write Claude usage cache", exc_info=True)


def _fetch_sync(token: str, timeout: float) -> dict[str, Any] | None:
    # S310 (scheme audit) is answered by construction: _USAGE_URL is a module
    # constant on https, and no caller-supplied value reaches the URL — so
    # there is no file:/custom scheme to smuggle in.
    request = Request(_USAGE_URL, method="GET")  # noqa: S310
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("anthropic-beta", _OAUTH_BETA)
    request.add_header("Content-Type", "application/json")
    with urlopen(request, timeout=timeout) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else None


async def fetch_claude_usage(
    *,
    timeout: float = _TIMEOUT_SECONDS,
    use_cache: bool = True,
    cache_max_age_seconds: int = _CACHE_MAX_AGE_SECONDS,
    credentials_path: Path | None = None,
    cache_path: Path | None = None,
) -> dict[str, Any] | None:
    """Fetch the account's rate-limit usage, or ``None`` when unavailable."""
    cache_file = cache_path or _CACHE_PATH
    if use_cache:
        cached = _load_cache(cache_file, cache_max_age_seconds)
        if cached is not None:
            return cached

    token = _read_access_token(credentials_path or _CREDENTIALS_PATH)
    if token is None:
        return None

    try:
        result = await asyncio.to_thread(_fetch_sync, token, timeout)
    except (OSError, ValueError, UnicodeDecodeError):
        logger.debug("Failed to fetch Claude usage", exc_info=True)
        return None

    if result is not None and use_cache:
        _write_cache(cache_file, result)
    return result
