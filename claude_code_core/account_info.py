"""Identify the Claude account a CLI subprocess will authenticate as.

:func:`detect_api_provider` answers "which endpoint", which is *not* the same
question as "how is this session paid for" — a direct Anthropic endpoint is
reached both by a Max subscription and by a metered API key. This module reads
the account the Claude Code CLI has on disk so the bridge can spell that out:

* ``~/.claude.json`` → ``oauthAccount`` holds the email address, the billing
  type and the organization plan tier.
* ``~/.claude/.credentials.json`` → ``claudeAiOauth`` holds the OAuth tokens
  and ``subscriptionType`` ("max", "pro", …).

Both files are read-only inputs; the bridge never writes them. Every accessor
degrades to ``None`` when a file is missing or malformed so a footer is never
lost to an unreadable cache.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .api_provider import uses_cloud_backend

logger = logging.getLogger(__name__)

_CLAUDE_JSON = Path.home() / ".claude.json"
_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"

# Env vars that make the CLI authenticate with a key instead of the account.
_API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

# Plan identifiers Anthropic returns, mapped to their display casing.
_PLAN_NAMES = {
    "max": "Max",
    "pro": "Pro",
    "team": "Team",
    "enterprise": "Enterprise",
    "free": "Free",
}


@dataclass(frozen=True)
class AccountInfo:
    """The signed-in Claude account, as far as the local files describe it."""

    email: str | None
    subscription_type: str | None
    billing_type: str | None
    organization_type: str | None


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def read_account_info(
    *,
    claude_json_path: Path | None = None,
    credentials_path: Path | None = None,
) -> AccountInfo | None:
    """Return the account the CLI is signed in as, or ``None`` if unknown."""
    account = _read_json(claude_json_path or _CLAUDE_JSON).get("oauthAccount")
    if not isinstance(account, dict):
        account = {}
    oauth = _read_json(credentials_path or _CREDENTIALS).get("claudeAiOauth")
    if not isinstance(oauth, dict):
        oauth = {}

    info = AccountInfo(
        email=account.get("emailAddress") or None,
        subscription_type=oauth.get("subscriptionType") or None,
        billing_type=account.get("billingType") or None,
        organization_type=account.get("organizationType") or None,
    )
    if not any((info.email, info.subscription_type, info.billing_type)):
        return None
    return info


def describe_auth(
    env: Mapping[str, str],
    account: AccountInfo | None = None,
) -> str | None:
    """Return a short label for *how* the session authenticates and pays.

    Args:
        env: The final subprocess environment (``_build_env()`` output), so an
            API key injected through the CLI env overlay is taken into account.
        account: The local account, or ``None`` when it could not be read.

    Returns:
        ``"Max subscription (you@example.com)"``, ``"API key (metered)"``, or
        ``None`` when the question doesn't apply (cloud backends) or the answer
        is unknown.
    """
    if uses_cloud_backend(env):
        return None

    if any((env.get(var) or "").strip() for var in _API_KEY_VARS):
        return "API key (metered)"

    if account is None:
        return None

    if account.subscription_type:
        plan = _PLAN_NAMES.get(account.subscription_type, account.subscription_type)
        label = f"{plan} subscription"
        return f"{label} ({account.email})" if account.email else label

    if account.email:
        return f"Claude account ({account.email})"
    return None
