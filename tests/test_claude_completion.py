"""Regression tests for the unified Claude completion card."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from claude_code_core.runner import ClaudeRunner
from claude_code_core.types import MessageType, RateLimitInfo, StreamEvent
from claude_discord.cogs.event_processor import EventProcessor
from claude_discord.cogs.run_config import RunConfig
from claude_discord.discord_ui.codex_completion import (
    CompletionDetailsView,
    post_claude_completion,
)

SID = "1ff264c7-756c-49df-a680-4d0293daf252"


async def test_claude_completion_shows_stored_quota_with_reset_time(monkeypatch) -> None:
    monkeypatch.setenv("CCDB_DISPLAY_TZ", "Asia/Tokyo")
    thread = AsyncMock()
    windows = [
        RateLimitInfo("five_hour", "allowed", 0.09, 1789063740),
        RateLimitInfo("seven_day", "allowed", 0.10, 1789162140),
        RateLimitInfo("seven_day_overage_included", "allowed", 0.01, 1789162140),
    ]
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
        quota_windows=windows,
        now=1789054740,
    )

    thread.send.assert_awaited_once()
    initial = thread.send.await_args.kwargs
    assert isinstance(initial["view"], CompletionDetailsView)
    assert initial["embed"].title == "✅ 応答完了"
    assert "実行環境：Claude" in initial["embed"].description
    assert "モデル：opus" in initial["embed"].description
    assert "アカウント：Max subscription (sasaki@saqutto.co.jp)" in initial["embed"].description

    final = initial["embed"]
    assert "5時間残量：91%（9/11(金) 03:09 リセット・あと2時間30分）" in final.description
    assert "週間残量：90%（9/12(土) 06:29 リセット・あと1日5時間50分）" in final.description
    assert "overage" not in final.description
    assert "API:" not in final.description
    thread.send.return_value.edit.assert_not_awaited()

    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    await initial["view"].children[0].callback(interaction)
    details = interaction.response.send_message.await_args.kwargs["embed"].description
    assert SID in details
    assert "10.9秒" in details
    assert "入力 2 / 出力 57" in details
    assert "Anthropic API (direct)" in details


async def test_window_past_its_reset_is_shown_as_reset() -> None:
    thread = AsyncMock()
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
        quota_windows=[RateLimitInfo("five_hour", "rejected", 1.0, 1789000000)],
        now=1789054740,
    )

    final = thread.send.await_args.kwargs["embed"]
    assert final.title == "✅ 応答完了"
    assert "アカウント：API key (metered)" in final.description
    assert "5時間残量：—（リセット済み）" in final.description
    assert "100%" not in final.description


async def test_no_stored_windows_keeps_completion_and_account() -> None:
    thread = AsyncMock()
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

    final = thread.send.await_args.kwargs["embed"]
    assert "実行環境：Claude" in final.description
    assert "アカウント：API key (metered)" in final.description
    assert "残量" not in final.description


async def test_usage_footer_opt_out_hides_quota(monkeypatch) -> None:
    monkeypatch.setenv("CCDB_USAGE_FOOTER", "off")
    thread = AsyncMock()
    await post_claude_completion(
        thread,
        model="opus",
        session_id=None,
        input_tokens=None,
        output_tokens=None,
        duration_ms=None,
        cost_usd=None,
        api_label=None,
        account_label=None,
        quota_windows=[RateLimitInfo("seven_day", "allowed", 0.1, 1789162140)],
        now=1789054740,
    )
    assert "残量" not in (thread.send.await_args.kwargs["embed"].description or "")


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
