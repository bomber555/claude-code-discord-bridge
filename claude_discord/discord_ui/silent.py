"""Send bot messages as @silent unless they are worth a notification.

A running session posts many messages — tool activity, interim text, and the
Stop button that is re-posted at the bottom after each one. Discord treats
every one of them as new, so a user following several threads gets a push and
an in-app banner every few seconds.

Installing this makes ``silent=True`` the default for every ``Messageable.send``.
The few messages that should notify say so explicitly with ``silent=False``
(completion cards, error notices, ``/api/notify``, scheduled notifications).
A message that @mentions a user also notifies, so input prompts (questions,
plan approval, permission requests, the owner ping) need no changes.

Opt out with ``CCDB_SILENT_BY_DEFAULT=false``.
"""

from __future__ import annotations

import functools
import os
import re
from typing import Any

import discord

_USER_MENTION = re.compile(r"<@!?\d+>")
_INSTALLED_ATTR = "__ccdb_silent_default__"


def silent_by_default_enabled() -> bool:
    """Return whether ``CCDB_SILENT_BY_DEFAULT`` leaves the feature on (default: on)."""
    return os.getenv("CCDB_SILENT_BY_DEFAULT", "true").lower() not in ("false", "0", "no")


def _wants_notification(kwargs: dict[str, Any], args: tuple[Any, ...]) -> bool:
    content = kwargs.get("content", args[0] if args else None)
    return isinstance(content, str) and bool(_USER_MENTION.search(content))


def install_silent_default() -> bool:
    """Patch ``discord.abc.Messageable.send`` once. Returns True if it is (now) installed."""
    original = discord.abc.Messageable.send
    if getattr(original, _INSTALLED_ATTR, False):
        return True

    @functools.wraps(original)
    async def send(self: Any, *args: Any, **kwargs: Any) -> Any:
        if "silent" not in kwargs:
            kwargs["silent"] = not _wants_notification(kwargs, args)
        return await original(self, *args, **kwargs)

    setattr(send, _INSTALLED_ATTR, True)
    send.__wrapped_original__ = original  # type: ignore[attr-defined]
    discord.abc.Messageable.send = send  # type: ignore[method-assign]
    return True


def uninstall_silent_default() -> None:
    """Restore the original ``send`` (used by tests)."""
    current = discord.abc.Messageable.send
    original = getattr(current, "__wrapped_original__", None)
    if original is not None:
        discord.abc.Messageable.send = original  # type: ignore[method-assign]
