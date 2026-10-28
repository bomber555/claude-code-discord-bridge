"""Thread status dashboard — live embed showing all active session states.

Posts and maintains a pinned embed in the main channel that shows which
threads are processing automatically vs. waiting for user input.
When a thread transitions to WAITING_INPUT, the bot mentions the owner
so Discord's notification system surfaces the request immediately.

When THREAD_INBOX_ENABLED is set, the dashboard also shows a persistent
inbox section (📬) that survives bot restarts and surfaces threads where
the user owes a reply.

Issue: https://github.com/ebibibi/ebi-agent-chat-relay/issues/67
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import discord

if TYPE_CHECKING:
    from ..database.inbox_repo import InboxEntry, ThreadInboxRepository

logger = logging.getLogger(__name__)


def _format_time(stored: str) -> str:
    """Render a DB timestamp as ``10/28 18:59`` in ``CCDB_DISPLAY_TZ`` (default: server local).

    ``thread_inbox.updated_at`` is ``datetime('now', 'localtime')`` — the
    server's local time, naive — so it is read in the server's zone first.
    """
    try:
        dt = datetime.strptime(stored, "%Y-%m-%d %H:%M:%S").astimezone()
    except (TypeError, ValueError):
        return stored
    tz_name = os.getenv("CCDB_DISPLAY_TZ", "").strip()
    if tz_name:
        with contextlib.suppress(Exception):
            dt = dt.astimezone(ZoneInfo(tz_name))
    return f"{dt.month}/{dt.day} {dt:%H:%M}"


def _fit(lines: list[str], limit: int) -> str:
    """Join *lines*, dropping the tail with a count once *limit* would be exceeded."""
    out: list[str] = []
    size = 0
    for i, line in enumerate(lines):
        rest = len(lines) - i
        tail = f"…ほか {rest} 件"
        if size + len(line) + 1 + len(tail) > limit:
            out.append(tail)
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


#: Discord's embed description limit.
_DESCRIPTION_LIMIT = 4096


_DASHBOARD_TITLE = "📊 Session Status"
#: How far back to look for this bot's older cards when posting a new one.
_STALE_CARD_SCAN_LIMIT = 200

# Embed colours
_COLOR_PROCESSING = 0x5865F2  # Discord blurple — all good, keep working
_COLOR_WAITING = 0xFEE75C  # Yellow — needs owner attention
_COLOR_IDLE = 0x99AAB5  # Grey — no active sessions

# State icons and labels shown in the embed
_STATE_ICON: dict[str, str] = {
    "processing": "🟢",
    "waiting": "🟡",
}
_STATE_LABEL: dict[str, str] = {
    "processing": "Auto-processing",
    "waiting": "Waiting for input",
}

# Threads older than this are pruned from the dashboard automatically.
# Keeps the embed from accumulating stale entries after a long idle period.
_STALE_HOURS = 4


class ThreadState(str, Enum):  # noqa: UP042 — requires-python = ">=3.10", StrEnum is 3.11+
    """Lifecycle state of a Claude Code session thread."""

    PROCESSING = "processing"
    """Claude Code CLI is currently running in this thread."""

    WAITING_INPUT = "waiting"
    """Claude finished responding; awaiting the next user message."""


@dataclass
class _ThreadInfo:
    thread_id: int
    description: str
    state: ThreadState
    started_at: float = field(default_factory=time.monotonic)
    state_changed_at: float = field(default_factory=time.monotonic)


class ThreadStatusDashboard:
    """Maintains a live status embed in the bot's main channel.

    Lifecycle
    ---------
    1. Call ``await dashboard.initialize()`` once after the bot is ready.
    2. Call ``await dashboard.set_state(...)`` on every state transition.
    3. Call ``await dashboard.remove(thread_id)`` when a thread is no longer
       relevant (optional — stale entries are auto-pruned after ``_STALE_HOURS``).

    Thread safety
    -------------
    All public methods are coroutines protected by an ``asyncio.Lock``.
    """

    def __init__(
        self,
        channel: discord.TextChannel,
        owner_id: int | None = None,
    ) -> None:
        self._channel = channel
        self._owner_id = owner_id
        self._threads: dict[int, _ThreadInfo] = {}
        self._dashboard_message: discord.Message | None = None
        self._lock = asyncio.Lock()
        # Persistent inbox entries (populated from DB when inbox is enabled).
        self._inbox: list[InboxEntry] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Post a fresh dashboard embed and delete this bot's older ones."""
        async with self._lock:
            await self._post_card()

    async def rebind(self, channel: discord.TextChannel) -> None:
        """Point the dashboard at *channel* after a gateway re-IDENTIFY.

        ``on_ready`` fires again after a READY, and discord.py rebuilds its
        Guild cache then, so the old channel's ``guild.get_thread()`` stops
        seeing renames and new threads. Keep this object (its live rows, and
        every reference cogs and running sessions hold) and swap the channel.
        """
        async with self._lock:
            self._channel = channel
            if self._dashboard_message is None:
                # The first post failed; retry it as on_ready used to.
                await self._post_card()
            else:
                await self._refresh_dashboard()

    async def _post_card(self) -> None:
        """Post the card and delete stale ones. Caller holds ``self._lock``."""
        embed = self._build_embed()
        self._dashboard_message = await self._channel.send(embed=embed)
        await self._delete_stale_cards(self._dashboard_message)

    async def _delete_stale_cards(self, keep: discord.Message) -> None:
        """Delete this bot's earlier Session Status cards in recent channel history."""
        try:
            async for msg in self._channel.history(limit=_STALE_CARD_SCAN_LIMIT):
                if msg.id == keep.id or msg.author.id != keep.author.id:
                    continue
                if any(e.title == _DASHBOARD_TITLE for e in msg.embeds):
                    with contextlib.suppress(discord.HTTPException):
                        await msg.delete()
        except (discord.HTTPException, TypeError, AttributeError):
            logger.debug("Could not clean up old dashboard cards", exc_info=True)

    async def set_state(
        self,
        thread_id: int,
        state: ThreadState,
        description: str,
        thread: discord.Thread | discord.TextChannel | None = None,
    ) -> None:
        """Update a thread's state and refresh the dashboard embed.

        When transitioning to ``WAITING_INPUT`` for the first time, the bot
        posts a reply in *thread* mentioning the owner so Discord surfaces the
        notification immediately.

        Parameters
        ----------
        thread_id:
            Discord thread ID.
        state:
            New ``ThreadState`` value.
        description:
            Short human-readable summary (e.g. the first 100 chars of the prompt).
        thread:
            The ``discord.Thread`` object, required for owner mentions.
        """
        async with self._lock:
            prev_state = self._threads[thread_id].state if thread_id in self._threads else None

            if thread_id not in self._threads:
                self._threads[thread_id] = _ThreadInfo(
                    thread_id=thread_id,
                    description=description,
                    state=state,
                )
            else:
                info = self._threads[thread_id]
                info.state = state
                info.state_changed_at = time.monotonic()
                if description:
                    info.description = description

            # Mention owner on first WAITING_INPUT transition
            should_mention = (
                state == ThreadState.WAITING_INPUT
                and prev_state != ThreadState.WAITING_INPUT
                and self._owner_id is not None
                and thread is not None
            )

            await self._refresh_dashboard()

        # Send mention outside the lock to avoid holding it during an HTTP call
        if should_mention and thread is not None:
            try:
                await thread.send(
                    f"🟡 <@{self._owner_id}> The agent has finished — your reply is needed here."
                )
            except (discord.HTTPException, RuntimeError):
                logger.debug("Failed to send owner mention in thread %d", thread_id, exc_info=True)

    async def remove(self, thread_id: int) -> None:
        """Remove a thread from the dashboard and refresh."""
        async with self._lock:
            self._threads.pop(thread_id, None)
            await self._refresh_dashboard()

    async def refresh_inbox(self, inbox_repo: ThreadInboxRepository) -> None:
        """Reload inbox entries from DB and refresh the dashboard embed.

        Called after every inbox upsert/remove so the embed stays current.
        """
        entries = await inbox_repo.list_all()
        async with self._lock:
            self._inbox = entries
            await self._refresh_dashboard()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _refresh_dashboard(self) -> None:
        """Edit the dashboard message with current state.

        Must be called while the caller holds ``self._lock``.
        Falls back to posting a new message if the original is gone.
        """
        self._prune_stale()

        if self._dashboard_message is None:
            return

        embed = self._build_embed()
        try:
            await self._dashboard_message.edit(embed=embed)
        except discord.NotFound:
            logger.debug("Dashboard message was deleted; re-posting")
            try:
                self._dashboard_message = await self._channel.send(embed=embed)
            except (discord.HTTPException, RuntimeError):
                logger.warning("Failed to re-post dashboard message", exc_info=True)
        except (discord.HTTPException, RuntimeError):
            logger.debug("Failed to edit dashboard message", exc_info=True)

    def _thread_label(self, thread_id: int) -> tuple[str, str]:
        """Return the thread's name as plain text and a jump URL.

        ``<#id>`` mentions are resolved by the client, and the mobile app shows
        ``#unknown`` for threads it has not loaded yet — which is most of them.
        """
        guild: Any = getattr(self._channel, "guild", None)
        guild_id = getattr(guild, "id", None)
        thread: Any = None
        with contextlib.suppress(Exception):
            thread = guild.get_thread(thread_id) if guild is not None else None
        name = getattr(thread, "name", None)
        label = name[:80] if isinstance(name, str) and name else f"スレッド {thread_id}"
        label = label.replace("[", "(").replace("]", ")")
        return label, f"https://discord.com/channels/{guild_id}/{thread_id}"

    def _prune_stale(self) -> None:
        """Remove threads that haven't changed state in ``_STALE_HOURS`` hours."""
        cutoff = time.monotonic() - _STALE_HOURS * 3600
        stale = [tid for tid, info in self._threads.items() if info.state_changed_at < cutoff]
        for tid in stale:
            logger.debug("Pruning stale dashboard entry for thread %d", tid)
            del self._threads[tid]

    def _build_embed(self) -> discord.Embed:
        """Construct the Discord embed reflecting current thread states."""
        has_live = bool(self._threads)
        has_inbox = bool(self._inbox)
        any_inbox_waiting = any(e.status == "waiting" for e in self._inbox)

        if not has_live and not has_inbox:
            return discord.Embed(
                title=_DASHBOARD_TITLE,
                description="No active sessions.",
                color=_COLOR_IDLE,
            )

        any_live_waiting = any(t.state == ThreadState.WAITING_INPUT for t in self._threads.values())
        color = _COLOR_WAITING if (any_live_waiting or any_inbox_waiting) else _COLOR_PROCESSING

        # One line per thread in the description: embed field names cannot
        # carry links, and fields cap the list at 25.
        lines: list[str] = []

        # ── Live sessions ──────────────────────────────────────────────
        now = time.monotonic()
        for info in sorted(self._threads.values(), key=lambda t: t.started_at):
            icon = _STATE_ICON[info.state.value]
            label = _STATE_LABEL[info.state.value]
            elapsed = int(now - info.state_changed_at)
            mins, secs = divmod(elapsed, 60)
            elapsed_str = f"{mins}m {secs}s" if mins else f"{secs}s"
            desc_preview = info.description[:60] + ("…" if len(info.description) > 60 else "")
            name, url = self._thread_label(info.thread_id)
            lines.append(f"{icon} [{name}]({url}) · {label} · {elapsed_str} ago")
            if desc_preview:
                lines.append(f"-# {desc_preview}")

        # ── Persistent inbox ───────────────────────────────────────────
        if has_inbox:
            if lines:
                lines.append("")
            lines.append("**📬 Inbox**")
            for entry in self._inbox:
                icon = "🟡" if entry.status == "waiting" else "❓"
                conf = "" if entry.confidence == "high" else " _(uncertain)_"
                name, url = self._thread_label(entry.thread_id)
                lines.append(
                    f"{icon} [{name}]({entry.last_message_url or url}){conf} "
                    f"{_format_time(entry.updated_at)}"
                )

        embed = discord.Embed(
            title=_DASHBOARD_TITLE, description=_fit(lines, _DESCRIPTION_LIMIT), color=color
        )
        embed.set_footer(text="Updates automatically · stale live entries removed after 4h")
        return embed
