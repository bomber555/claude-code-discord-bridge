"""Let a session keep its own Discord thread title current.

A thread's title is the only part of a session a human sees without opening
it, so a session whose goal moved on ("investigate X" → "PR for Y, waiting on
review") leaves a stale label in the sidebar.  The session knows when that
happens; ccdb does not.  So the session asks, through
``POST /api/threads/{thread_id}/title``, and this module decides whether and
when the rename actually reaches Discord.

Two rules are enforced here rather than left to the prompt:

* **A human rename wins.**  ccdb remembers the last title it set.  If the
  thread's current name is something else, a person (or another bot) renamed
  it, and the thread is locked against further automatic renames until a
  request arrives with ``override: true`` — which the session should only send
  when the human has said so.
* **Discord's rename limit is respected.**  Discord allows two name changes per
  thread per ten minutes; past that, discord.py silently sleeps inside
  ``edit()`` for minutes.  Extra requests inside the window are coalesced and
  only the latest is applied once the window opens.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Discord: 2 thread renames per 10 minutes per channel.
RENAME_LIMIT = 2
RENAME_WINDOW_SECONDS = 600.0
MAX_TITLE_LENGTH = 100

_SETTINGS_KEY = "thread_title:{thread_id}"


def normalize_title(raw: object) -> str:
    """Collapse a caller-supplied title to one line within Discord's limit."""
    return " ".join(str(raw or "").split())[:MAX_TITLE_LENGTH]


@dataclass
class TitleResult:
    """Outcome of a title request, shaped for the API response."""

    status: str  # "renamed" | "unchanged" | "scheduled" | "locked"
    title: str
    current: str | None = None
    apply_in_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status, "title": self.title}
        if self.current is not None:
            out["current"] = self.current
        if self.apply_in_seconds is not None:
            out["apply_in_seconds"] = round(self.apply_in_seconds)
        return out


class ThreadTitler:
    """Apply session-requested thread titles with a human-rename lock.

    State that must survive a restart (the last title ccdb set, and whether a
    human has locked the thread) lives in the settings table.  Rate-limit
    bookkeeping is process-local: after a restart the worst case is one extra
    rename that discord.py throttles itself.
    """

    def __init__(self, settings_repo: Any | None) -> None:
        self.settings_repo = settings_repo
        self._edits: dict[int, deque[float]] = {}
        self._pending: dict[int, str] = {}
        self._timers: dict[int, asyncio.Task[None]] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def _load(self, thread_id: int) -> dict[str, Any]:
        if self.settings_repo is None:
            return {}
        raw = await self.settings_repo.get(_SETTINGS_KEY.format(thread_id=thread_id))
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    async def _save(self, thread_id: int, state: dict[str, Any]) -> None:
        if self.settings_repo is None:
            return
        await self.settings_repo.set(
            _SETTINGS_KEY.format(thread_id=thread_id), json.dumps(state, ensure_ascii=False)
        )

    def _wait_seconds(self, thread_id: int, now: float) -> float:
        edits = self._edits.setdefault(thread_id, deque())
        while edits and now - edits[0] >= RENAME_WINDOW_SECONDS:
            edits.popleft()
        if len(edits) < RENAME_LIMIT:
            return 0.0
        return RENAME_WINDOW_SECONDS - (now - edits[0])

    async def request(self, thread: Any, title: str, *, override: bool = False) -> TitleResult:
        """Rename *thread* to *title* now, later, or not at all."""
        thread_id = int(thread.id)
        lock = self._locks.setdefault(thread_id, asyncio.Lock())
        async with lock:
            state = await self._load(thread_id)
            current = getattr(thread, "name", None)
            last_set = state.get("last_set")

            if override:
                state["locked"] = False
            elif not state.get("locked") and last_set is not None and current != last_set:
                state["locked"] = True
                await self._save(thread_id, state)
                logger.info("thread %d renamed by someone else; auto-title locked", thread_id)
            if state.get("locked"):
                self._cancel(thread_id)
                return TitleResult("locked", title, current=current)

            if title == current:
                self._cancel(thread_id)
                state["last_set"] = title
                await self._save(thread_id, state)
                return TitleResult("unchanged", title)

            wait = self._wait_seconds(thread_id, time.monotonic())
            if wait > 0:
                self._pending[thread_id] = title
                if thread_id not in self._timers:
                    self._timers[thread_id] = asyncio.create_task(self._apply_later(thread, wait))
                return TitleResult("scheduled", title, apply_in_seconds=wait)

            self._cancel(thread_id)
            await self._edit(thread, title, state)
            return TitleResult("renamed", title)

    def _cancel(self, thread_id: int) -> None:
        self._pending.pop(thread_id, None)
        timer = self._timers.pop(thread_id, None)
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()

    async def _edit(self, thread: Any, title: str, state: dict[str, Any]) -> None:
        await thread.edit(name=title)
        self._edits.setdefault(int(thread.id), deque()).append(time.monotonic())
        state["last_set"] = title
        await self._save(int(thread.id), state)

    async def _apply_later(self, thread: Any, wait: float) -> None:
        thread_id = int(thread.id)
        try:
            await asyncio.sleep(wait)
            async with self._locks.setdefault(thread_id, asyncio.Lock()):
                self._timers.pop(thread_id, None)
                title = self._pending.pop(thread_id, None)
                if title is None:
                    return
                state = await self._load(thread_id)
                current = getattr(thread, "name", None)
                if state.get("locked") or (
                    state.get("last_set") is not None and current != state["last_set"]
                ):
                    state["locked"] = True
                    await self._save(thread_id, state)
                    return
                if title != current:
                    await self._edit(thread, title, state)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Deferred rename of thread %d failed", thread_id, exc_info=True)
