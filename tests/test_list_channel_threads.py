"""Tests for GET /api/channels/{channel_id}/threads."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from claude_discord.ext.api_server import ApiServer

GUILD_ID = 10
CHANNEL_ID = 100
BASE = datetime(2026, 11, 1, tzinfo=UTC)


def thread(thread_id: int, *, parent: int = CHANNEL_ID, archived=False, private=False):
    return SimpleNamespace(
        id=thread_id,
        name=f"t{thread_id}",
        parent_id=parent,
        archived=archived,
        archive_timestamp=BASE - timedelta(minutes=thread_id),
        is_private=lambda: private,
    )


class FakeChannel:
    def __init__(self, guild, archived=(), *, readable=True) -> None:
        self.id = CHANNEL_ID
        self.guild = guild
        self.archived = list(archived)
        self.readable = readable
        self.archive_calls: list[dict] = []

    def permissions_for(self, _member):
        return SimpleNamespace(view_channel=self.readable, read_message_history=self.readable)

    async def archived_threads(self, *, limit, before, private):
        self.archive_calls.append({"limit": limit, "before": before, "private": private})
        items = [t for t in self.archived if before is None or t.archive_timestamp < before]
        for t in items[:limit]:
            yield t


def make_guild(active=()):
    return SimpleNamespace(id=GUILD_ID, me=object(), active_threads=AsyncMock(return_value=active))


@pytest.fixture
async def make_client(tmp_path):
    clients = []

    async def _make(channel) -> TestClient:
        bot = MagicMock()
        bot.get_channel = MagicMock(side_effect=lambda cid: channel if cid == CHANNEL_ID else None)
        bot.fetch_channel = AsyncMock(side_effect=RuntimeError("unknown channel"))
        bot.guilds = [SimpleNamespace(id=GUILD_ID)]
        api = ApiServer(repo=MagicMock(), bot=bot)
        client = TestClient(TestServer(api.app))
        await client.start_server()
        clients.append(client)
        return client

    yield _make
    for client in clients:
        await client.close()


URL = f"/api/channels/{CHANNEL_ID}/threads"


async def test_returns_requested_fields_and_only_this_channels_public_threads(make_client):
    active = [thread(1), thread(2, parent=999), thread(3, private=True)]
    channel = FakeChannel(make_guild(active), [thread(4, archived=True)])
    resp = await (await make_client(channel)).get(URL)
    assert resp.status == 200
    body = await resp.json()
    assert body["threads"] == [
        {
            "id": "1",
            "name": "t1",
            "parent_id": str(CHANNEL_ID),
            "archived": False,
            "url": f"https://discord.com/channels/{GUILD_ID}/1",
        },
        {
            "id": "4",
            "name": "t4",
            "parent_id": str(CHANNEL_ID),
            "archived": True,
            "url": f"https://discord.com/channels/{GUILD_ID}/4",
        },
    ]
    assert body["next_cursor"] is None
    assert channel.archive_calls[0]["private"] is False


async def test_pages_through_active_then_archived_without_gaps(make_client):
    active = [thread(i) for i in (20, 21, 22)]
    archived = [thread(i, archived=True) for i in (1, 2, 3)]
    client = await make_client(FakeChannel(make_guild(active), archived))
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        params = {"limit": "2"} | ({"cursor": cursor} if cursor else {})
        body = await (await client.get(URL, params=params)).json()
        assert len(body["threads"]) <= 2
        seen += [t["id"] for t in body["threads"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == ["22", "21", "20", "1", "2", "3"]


async def test_active_page_exactly_full_still_leads_to_archived(make_client):
    channel = FakeChannel(make_guild([thread(20)]), [thread(1, archived=True)])
    client = await make_client(channel)
    first = await (await client.get(URL, params={"limit": "1"})).json()
    assert [t["id"] for t in first["threads"]] == ["20"]
    second = await (
        await client.get(URL, params={"limit": "1", "cursor": first["next_cursor"]})
    ).json()
    assert [t["id"] for t in second["threads"]] == ["1"]


@pytest.mark.parametrize(
    ("state", "expected"), [("active", ["20"]), ("archived", ["1"]), ("all", ["20", "1"])]
)
async def test_state_filter(make_client, state, expected):
    channel = FakeChannel(make_guild([thread(20)]), [thread(1, archived=True)])
    body = await (await (await make_client(channel)).get(URL, params={"state": state})).json()
    assert [t["id"] for t in body["threads"]] == expected
    assert body["next_cursor"] is None


async def test_limit_is_capped_at_100(make_client):
    channel = FakeChannel(make_guild(), [])
    await (await make_client(channel)).get(URL, params={"limit": "5000"})
    assert channel.archive_calls[0]["limit"] == 101


@pytest.mark.parametrize(
    "params",
    [
        {"state": "private"},
        {"limit": "many"},
        {"cursor": "nonsense"},
        {"cursor": "active:abc"},
        {"cursor": "archived:not-a-date"},
        {"state": "active", "cursor": "archived:"},
        {"state": "archived", "cursor": "active:5"},
    ],
)
async def test_bad_parameters_are_rejected(make_client, params):
    channel = FakeChannel(make_guild(), [])
    assert (await (await make_client(channel)).get(URL, params=params)).status == 400


async def test_channel_the_bot_cannot_read_is_refused(make_client):
    guild = make_guild([thread(1)])
    channel = FakeChannel(guild, [], readable=False)
    assert (await (await make_client(channel)).get(URL)).status == 403
    guild.active_threads.assert_not_awaited()


async def test_channel_in_another_guild_is_refused(make_client):
    guild = make_guild([thread(1)])
    guild.id = 999
    assert (await (await make_client(FakeChannel(guild))).get(URL)).status == 403
    guild.active_threads.assert_not_awaited()


async def test_channel_without_threads_is_rejected(make_client):
    channel = SimpleNamespace(id=CHANNEL_ID, guild=make_guild())
    assert (await (await make_client(channel)).get(URL)).status == 400


async def test_unknown_channel_is_404(make_client):
    client = await make_client(FakeChannel(make_guild()))
    assert (await client.get("/api/channels/555/threads")).status == 404


async def test_invalid_channel_id_is_400(make_client):
    client = await make_client(FakeChannel(make_guild()))
    assert (await client.get("/api/channels/abc/threads")).status == 400


async def test_discord_failure_is_502(make_client):
    guild = make_guild()
    guild.active_threads.side_effect = RuntimeError("discord down")
    assert (await (await make_client(FakeChannel(guild))).get(URL)).status == 502


async def test_route_is_not_exposed_on_external_listener(make_client, tmp_path):
    api = ApiServer(repo=MagicMock(), bot=MagicMock())
    paths = {r.resource.canonical for r in api._build_external_app().router.routes()}
    assert not any("/threads" in p for p in paths)
