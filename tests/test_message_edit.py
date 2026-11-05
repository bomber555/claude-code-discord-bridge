"""An edit by a person reaches the session, with a diff when the old text is known."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from claude_discord.cogs.claude_chat import ClaudeChatCog
from claude_discord.message_edit import build_edit_prompt

URL = "https://discord.com/channels/1/2/3"
PARENT_ID = 999
THREAD_ID = 12345


def _cog(*, record: object = True, allowed: set[int] | None = None) -> ClaudeChatCog:
    bot = MagicMock()
    bot.user = SimpleNamespace(id=1)
    thread = MagicMock(spec=discord.Thread)
    thread.id = THREAD_ID
    thread.parent_id = PARENT_ID
    bot.get_channel = MagicMock(return_value=thread)
    repo = MagicMock()
    stored = SimpleNamespace(session_id="abc", working_dir="/w", backend=None)
    repo.get = AsyncMock(return_value=stored if record else None)
    cog = ClaudeChatCog(bot=bot, repo=repo, runner=MagicMock(), allowed_user_ids=allowed)
    cog._channel_ids = {PARENT_ID}
    cog._inject_into_running_turn = AsyncMock(return_value=False)
    cog._session_id_for_current_backend = AsyncMock(return_value="abc")
    cog._run_claude = AsyncMock()
    return cog


def _payload(
    after: str = "new",
    *,
    before: str | None = "old",
    author_bot: bool = False,
    author_id: int = 7,
    edited_ago: float | None = 1,
    message_type: discord.MessageType = discord.MessageType.default,
) -> SimpleNamespace:
    edited_at = None
    if edited_ago is not None:
        edited_at = datetime.now(UTC) - timedelta(seconds=edited_ago)
    message = MagicMock()
    message.content = after
    message.jump_url = URL
    message.author = SimpleNamespace(bot=author_bot, id=author_id)
    message.type = message_type
    message.edited_at = edited_at
    message.mentions = []
    message.add_reaction = AsyncMock()
    cached = SimpleNamespace(content=before) if before is not None else None
    return SimpleNamespace(message=message, cached_message=cached, channel_id=THREAD_ID)


class TestPrompt:
    def test_diff_shows_what_changed_and_keeps_full_text(self) -> None:
        prompt = build_edit_prompt("keep\nold line", "keep\nnew line", URL)
        assert "-old line" in prompt
        assert "+new line" in prompt
        assert "---" not in prompt
        assert prompt.endswith("Full text after the edit:\nkeep\nnew line")
        assert URL in prompt

    def test_japanese_single_line(self) -> None:
        prompt = build_edit_prompt("明日やります", "今日やります", URL)
        assert "-明日やります" in prompt
        assert "+今日やります" in prompt

    def test_unknown_old_text_says_so_instead_of_diffing(self) -> None:
        prompt = build_edit_prompt(None, "after", URL)
        assert "no diff" in prompt
        assert "```diff" not in prompt
        assert "after" in prompt

    def test_emptied_message(self) -> None:
        prompt = build_edit_prompt("old", "", URL)
        assert "-old" in prompt
        assert "now empty" in prompt


class TestDelivery:
    @pytest.mark.asyncio
    async def test_idle_session_starts_a_turn_with_the_diff(self) -> None:
        cog = _cog()
        payload = _payload()

        await cog.on_raw_message_edit(payload)

        call = cog._run_claude.await_args
        assert "-old" in call.args[2]
        assert "+new" in call.args[2]
        assert call.kwargs["session_id"] == "abc"

    @pytest.mark.asyncio
    async def test_running_turn_gets_it_injected(self) -> None:
        cog = _cog()
        cog._inject_into_running_turn = AsyncMock(return_value=True)
        payload = _payload()

        await cog.on_raw_message_edit(payload)

        cog._run_claude.assert_not_awaited()
        payload.message.add_reaction.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_uncached_recent_edit_is_delivered_without_diff(self) -> None:
        cog = _cog()

        await cog.on_raw_message_edit(_payload(before=None))

        assert "no diff" in cog._run_claude.await_args.args[2]


class TestIgnored:
    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(lambda: _payload(after="same", before="same"), id="unfurl-cached"),
            pytest.param(lambda: _payload(before=None, edited_ago=None), id="never-edited"),
            pytest.param(lambda: _payload(before=None, edited_ago=3600), id="unfurl-old-edit"),
            pytest.param(lambda: _payload(author_bot=True), id="bot"),
            pytest.param(lambda: _payload(message_type=discord.MessageType.pins_add), id="system"),
        ],
    )
    @pytest.mark.asyncio
    async def test_not_a_persons_edit(self, payload) -> None:
        cog = _cog()
        await cog.on_raw_message_edit(payload())
        cog._run_claude.assert_not_awaited()
        cog._inject_into_running_turn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unauthorized_user(self) -> None:
        cog = _cog(allowed={1})
        await cog.on_raw_message_edit(_payload(author_id=7))
        cog._run_claude.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_thread_without_a_session(self) -> None:
        cog = _cog(record=False)
        await cog.on_raw_message_edit(_payload())
        cog._run_claude.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_channel_that_is_not_a_thread(self) -> None:
        cog = _cog()
        cog.bot.get_channel = MagicMock(return_value=MagicMock(spec=discord.TextChannel))
        await cog.on_raw_message_edit(_payload())
        cog._run_claude.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_thread_outside_the_no_mention_scope(self) -> None:
        cog = _cog()
        cog._channel_ids = {1}
        await cog.on_raw_message_edit(_payload())
        cog._run_claude.assert_not_awaited()
