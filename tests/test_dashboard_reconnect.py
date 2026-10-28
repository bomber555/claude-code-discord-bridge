"""Session Status card across a gateway re-IDENTIFY (on_ready firing again).

discord.py rebuilds its Guild cache on READY, so a dashboard still holding the
pre-reconnect channel shows stale thread names, and a second dashboard object
meant two cards each getting half the updates. The bot must keep one dashboard
and point it at the fresh channel.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from claude_discord.bot import ClaudeDiscordBot
from claude_discord.cogs.claude_chat import ClaudeChatCog
from claude_discord.discord_ui.thread_dashboard import ThreadState, ThreadStatusDashboard


async def _no_history(**_kwargs: object):  # type: ignore[no-untyped-def]
    return
    yield


def _make_channel(thread_names: dict[int, str] | None = None) -> MagicMock:
    """A TextChannel whose guild knows *thread_names* (a fresh guild per call)."""
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 500
    msg = MagicMock(spec=discord.Message)
    msg.edit = AsyncMock()
    channel.send = AsyncMock(return_value=msg)
    channel.history = _no_history
    names = thread_names or {}
    guild = MagicMock()
    guild.id = 9

    def _get_thread(thread_id: int) -> MagicMock | None:
        if thread_id not in names:
            return None
        t = MagicMock()
        t.name = names[thread_id]
        return t

    guild.get_thread = _get_thread
    channel.guild = guild
    return channel


def _make_bot(channel: MagicMock) -> SimpleNamespace:
    bot = SimpleNamespace(
        user=MagicMock(id=42),
        channel_id=500,
        owner_id=None,
        thread_dashboard=None,
        inbox_repo=None,
        worktree_manager=None,
        guilds=[],
        tree=MagicMock(),
        _restore_pending_ask_views=AsyncMock(),
    )
    bot.get_channel = lambda _cid: bot._channel
    bot._channel = channel
    return bot


async def _on_ready(bot: SimpleNamespace) -> None:
    await ClaudeDiscordBot.on_ready(bot)  # type: ignore[arg-type]


def _reconnect(bot: SimpleNamespace, channel: MagicMock) -> None:
    bot._channel = channel


def _last_description(msg: MagicMock) -> str:
    return msg.edit.call_args.kwargs["embed"].description


class TestDashboardAcrossReconnect:
    async def test_first_on_ready_creates_and_posts(self) -> None:
        ch = _make_channel()
        bot = _make_bot(ch)
        await _on_ready(bot)
        assert isinstance(bot.thread_dashboard, ThreadStatusDashboard)
        ch.send.assert_awaited_once()

    async def test_reconnect_keeps_one_dashboard_and_edits_the_same_card(self) -> None:
        ch1 = _make_channel()
        bot = _make_bot(ch1)
        await _on_ready(bot)
        first = bot.thread_dashboard
        card = ch1.send.return_value

        ch2 = _make_channel()
        _reconnect(bot, ch2)
        await _on_ready(bot)

        assert bot.thread_dashboard is first
        ch1.send.assert_awaited_once()
        ch2.send.assert_not_awaited()
        card.edit.assert_awaited()

    async def test_live_rows_survive_reconnect(self) -> None:
        ch1 = _make_channel({123: "作業中"})
        bot = _make_bot(ch1)
        await _on_ready(bot)
        await bot.thread_dashboard.set_state(123, ThreadState.PROCESSING, "doing")
        card = ch1.send.return_value
        card.edit.reset_mock()

        _reconnect(bot, _make_channel({123: "作業中"}))
        await _on_ready(bot)

        card.edit.assert_awaited()
        assert "作業中" in _last_description(card)

    async def test_names_come_from_the_fresh_guild(self) -> None:
        ch1 = _make_channel({123: "古い名前"})
        bot = _make_bot(ch1)
        await _on_ready(bot)
        await bot.thread_dashboard.set_state(123, ThreadState.PROCESSING, "doing")

        _reconnect(bot, _make_channel({123: "新しい名前", 456: "再接続後のスレッド"}))
        await _on_ready(bot)
        await bot.thread_dashboard.set_state(456, ThreadState.PROCESSING, "new")

        desc = _last_description(ch1.send.return_value)
        assert "新しい名前" in desc
        assert "再接続後のスレッド" in desc
        assert "古い名前" not in desc

    async def test_card_deleted_while_disconnected_is_reposted_to_fresh_channel(self) -> None:
        ch1 = _make_channel()
        bot = _make_bot(ch1)
        await _on_ready(bot)
        ch1.send.return_value.edit.side_effect = discord.NotFound(
            MagicMock(status=404), "Unknown Message"
        )

        ch2 = _make_channel()
        _reconnect(bot, ch2)
        await _on_ready(bot)

        ch2.send.assert_awaited_once()

    async def test_cog_reference_taken_before_reconnect_stays_current(self) -> None:
        ch1 = _make_channel()
        bot = _make_bot(ch1)
        await _on_ready(bot)
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())  # type: ignore[arg-type]
        held = cog._get_dashboard()

        _reconnect(bot, _make_channel())
        await _on_ready(bot)

        assert held is bot.thread_dashboard
        assert cog._get_dashboard() is bot.thread_dashboard

    async def test_updates_through_held_reference_do_not_post_a_second_card(self) -> None:
        """Regression: the held (pre-reconnect) dashboard used to re-post card B."""
        ch1 = _make_channel({123: "x"})
        bot = _make_bot(ch1)
        await _on_ready(bot)
        cog = ClaudeChatCog(bot=bot, repo=MagicMock(), runner=MagicMock())  # type: ignore[arg-type]
        held = cog._get_dashboard()

        ch2 = _make_channel({123: "x"})
        _reconnect(bot, ch2)
        await _on_ready(bot)
        assert held is not None
        await held.set_state(123, ThreadState.PROCESSING, "doing")

        assert ch1.send.await_count + ch2.send.await_count == 1

    async def test_failed_first_post_is_retried_on_next_on_ready(self) -> None:
        ch1 = _make_channel()
        ch1.send.side_effect = discord.HTTPException(MagicMock(status=500), "boom")
        bot = _make_bot(ch1)
        with pytest.raises(discord.HTTPException):
            await _on_ready(bot)

        ch2 = _make_channel()
        _reconnect(bot, ch2)
        await _on_ready(bot)

        ch2.send.assert_awaited_once()
