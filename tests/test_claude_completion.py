"""Regression tests for the unified Claude completion card."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from claude_code_core.runner import ClaudeRunner
from claude_code_core.types import MessageType, StreamEvent
from claude_discord.cogs.event_processor import EventProcessor
from claude_discord.cogs.run_config import RunConfig
from claude_discord.discord_ui.codex_completion import (
    CompletionDetailsView,
    post_claude_completion,
)

SID = "1ff264c7-756c-49df-a680-4d0293daf252"


async def test_claude_completion_matches_codex_card_and_updates_in_place() -> None:
    thread = AsyncMock()
    usage = {
        "five_hour": {"utilization": 9, "resets_at": 1789063740},
        "seven_day": {"utilization": 10, "resets_at": 1789162140},
    }
    with patch(
        "claude_discord.discord_ui.codex_completion.fetch_claude_usage",
        AsyncMock(return_value=usage),
    ):
        await post_claude_completion(
            thread,
            model="opus",
            session_id=SID,
            input_tokens=2,
            output_tokens=57,
            duration_ms=10900,
            cost_usd=None,
            api_label="Anthropic API (direct)",
            account_label="Max subscription (sasaki@saqutto.co.jp)",
            now=1789054740,
        )

    thread.send.assert_awaited_once()
    initial = thread.send.await_args.kwargs
    assert isinstance(initial["view"], CompletionDetailsView)
    assert initial["embed"].title == "✅ 応答完了"
    assert "実行環境：Claude" in initial["embed"].description
    assert "モデル：opus" in initial["embed"].description
    assert "アカウント：Max subscription (sasaki@saqutto.co.jp)" in initial["embed"].description

    final = thread.send.return_value.edit.await_args.kwargs["embed"]
    assert "5時間残量：91%（リセットまで2時間30分）" in final.description
    assert "週間残量：90%（リセットまで1日5時間50分）" in final.description
    assert "used" not in final.description
    assert "API:" not in final.description

    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    await initial["view"].children[0].callback(interaction)
    details = interaction.response.send_message.await_args.kwargs["embed"].description
    assert SID in details
    assert "10.9秒" in details
    assert "入力 2 / 出力 57" in details
    assert "Anthropic API (direct)" in details


async def test_claude_usage_failure_keeps_completion_and_account() -> None:
    thread = AsyncMock()
    with patch(
        "claude_discord.discord_ui.codex_completion.fetch_claude_usage",
        AsyncMock(side_effect=OSError("offline")),
    ):
        await post_claude_completion(
            thread,
            model="sonnet",
            session_id=None,
            input_tokens=None,
            output_tokens=None,
            duration_ms=None,
            cost_usd=None,
            api_label=None,
            account_label="API key (metered)",
        )

    thread.send.assert_awaited_once()
    final = thread.send.return_value.edit.await_args.kwargs["embed"]
    assert final.title == "✅ 応答完了"
    assert "実行環境：Claude" in final.description
    assert "アカウント：API key (metered)" in final.description


async def test_claude_stream_emits_only_the_shared_completion_card() -> None:
    thread = MagicMock()
    thread.id = 123
    thread.send = AsyncMock()
    runner = ClaudeRunner(model="opus")
    with (
        patch.object(runner, "describe_api", return_value="Anthropic API (direct)"),
        patch.object(
            runner,
            "describe_account",
            return_value="Max subscription (owner@example.com)",
        ),
        patch(
            "claude_discord.discord_ui.codex_completion.fetch_claude_usage",
            AsyncMock(return_value=None),
        ),
    ):
        processor = EventProcessor(RunConfig(thread=thread, runner=runner, prompt="test"))
        await processor.process(
            StreamEvent(raw={}, message_type=MessageType.SYSTEM, session_id=SID)
        )
        await processor.process(
            StreamEvent(
                raw={},
                message_type=MessageType.RESULT,
                is_complete=True,
                text="OK",
                duration_ms=10900,
                input_tokens=2,
                output_tokens=57,
            )
        )
        await processor.finalize()
        await asyncio.sleep(0)

    titles = [
        call.kwargs["embed"].title for call in thread.send.await_args_list if "embed" in call.kwargs
    ]
    assert titles == ["✅ 応答完了"]
