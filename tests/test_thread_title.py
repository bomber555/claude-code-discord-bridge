"""Tests for session-requested thread titles (ThreadTitler + POST /api/threads/{id}/title)."""

from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer

from claude_discord import thread_title
from claude_discord.database.models import init_db
from claude_discord.database.notification_repo import NotificationRepository
from claude_discord.database.settings_repo import SettingsRepository
from claude_discord.ext.api_server import ApiServer
from claude_discord.thread_title import ThreadTitler, normalize_title


@pytest.fixture
async def db_path() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    await init_db(path)
    yield path
    os.unlink(path)


def _thread(name: str = "first message", thread_id: int = 111) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.name = name

    async def edit(*, name: str) -> None:
        thread.name = name

    thread.edit = AsyncMock(side_effect=edit)
    return thread


def test_normalize_title_is_one_line_and_bounded() -> None:
    assert normalize_title("  #1 調査中\n  ゴール  ") == "#1 調査中 ゴール"
    assert len(normalize_title("x" * 300)) == 100
    assert normalize_title(None) == ""


async def test_renames_then_unchanged(db_path: str) -> None:
    titler = ThreadTitler(SettingsRepository(db_path))
    thread = _thread()
    assert (await titler.request(thread, "#1 調査中")).status == "renamed"
    assert thread.name == "#1 調査中"
    assert (await titler.request(thread, "#1 調査中")).status == "unchanged"
    assert thread.edit.await_count == 1


async def test_human_rename_locks_until_override(db_path: str) -> None:
    repo = SettingsRepository(db_path)
    titler = ThreadTitler(repo)
    thread = _thread()
    await titler.request(thread, "#1 調査中")
    thread.name = "人が付けた名前"  # a human renamed it in Discord

    result = await titler.request(thread, "#1 PR待ち")
    assert result.status == "locked"
    assert thread.name == "人が付けた名前"

    # The lock survives a restart (new titler, same DB).
    assert (await ThreadTitler(repo).request(thread, "#1 PR待ち")).status == "locked"

    result = await ThreadTitler(repo).request(thread, "#1 PR待ち", override=True)
    assert result.status == "renamed"
    assert thread.name == "#1 PR待ち"


async def test_rate_limit_coalesces_to_latest(
    db_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(thread_title, "RENAME_WINDOW_SECONDS", 0.2)
    titler = ThreadTitler(SettingsRepository(db_path))
    thread = _thread()
    assert (await titler.request(thread, "a")).status == "renamed"
    assert (await titler.request(thread, "b")).status == "renamed"
    assert (await titler.request(thread, "c")).status == "scheduled"
    assert (await titler.request(thread, "d")).status == "scheduled"
    assert thread.name == "b"
    await asyncio.sleep(0.4)
    assert thread.name == "d"
    assert thread.edit.await_count == 3


async def test_deferred_rename_respects_human_rename(
    db_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(thread_title, "RENAME_WINDOW_SECONDS", 0.2)
    titler = ThreadTitler(SettingsRepository(db_path))
    thread = _thread()
    await titler.request(thread, "a")
    await titler.request(thread, "b")
    assert (await titler.request(thread, "c")).status == "scheduled"
    thread.name = "human"
    await asyncio.sleep(0.4)
    assert thread.name == "human"


@pytest.fixture
async def api(db_path: str) -> tuple[TestClient, MagicMock]:
    notif_repo = NotificationRepository(db_path)
    await notif_repo.init_db()
    bot = MagicMock()
    bot.cogs = {}
    thread = _thread()
    bot.get_channel.side_effect = lambda cid: thread if cid == thread.id else None
    bot.fetch_channel = AsyncMock(side_effect=RuntimeError("Unknown Channel"))
    server = ApiServer(repo=notif_repo, bot=bot, default_channel_id=1, host="127.0.0.1", port=0)
    server.titler.settings_repo = SettingsRepository(db_path)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    yield client, thread
    await client.close()


async def test_api_renames_thread(api: tuple[TestClient, MagicMock]) -> None:
    client, thread = api
    resp = await client.post("/api/threads/111/title", json={"title": "#1284 調査中 | 名前"})
    assert resp.status == 200
    assert (await resp.json())["status"] == "renamed"
    assert thread.name == "#1284 調査中 | 名前"


async def test_api_locked_is_409(api: tuple[TestClient, MagicMock]) -> None:
    client, thread = api
    await client.post("/api/threads/111/title", json={"title": "a"})
    thread.name = "human"
    resp = await client.post("/api/threads/111/title", json={"title": "b"})
    assert resp.status == 409
    assert (await resp.json())["current"] == "human"
    resp = await client.post("/api/threads/111/title", json={"title": "b", "override": True})
    assert resp.status == 200


async def test_api_validation(api: tuple[TestClient, MagicMock]) -> None:
    client, _ = api
    assert (await client.post("/api/threads/111/title", json={})).status == 400
    assert (await client.post("/api/threads/x/title", json={"title": "a"})).status == 400
    assert (await client.post("/api/threads/999/title", json={"title": "a"})).status == 404
