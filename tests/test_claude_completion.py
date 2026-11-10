"""Regression tests for the unified Claude completion card."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from claude_code_core.runner import ClaudeRunner
from claude_code_core.types import MessageType, RateLimitInfo, StreamEvent
from claude_discord.cogs.event_processor import EventProcessor
from claude_discord.cogs.run_config import RunConfig
from claude_discord.discord_ui.codex_completion import (
    post_claude_completion,
    short_claude_model,
)

SID = "1ff264c7-756c-49df-a680-4d0293daf252"


def _card(**overrides):
    kwargs = dict(
        model="claude-opus-5-5",
        session_id=SID,
        input_tokens=2,
        output_tokens=57,
        duration_ms=10900,
        cost_usd=None,
        api_label="Anthropic API (direct)",
        account_label="Max subscription (owner@example.com)",
        quota_windows=[
            RateLimitInfo("five_hour", "allowed", 0.09, 1789063740),
            RateLimitInfo("seven_day", "allowed", 0.07, 1789162140),
            RateLimitInfo("seven_day_overage_included", "allowed", 0.5, 1789162140),
        ],
        effort="medium",
        now=1789054740,
    )
    kwargs.update(overrides)
    return kwargs


async def test_claude_card_is_title_plus_one_plan_quota_line(monkeypatch) -> None:
    monkeypatch.setenv("CCDB_DISPLAY_TZ", "Asia/Tokyo")
    thread = AsyncMock()
    await post_claude_completion(thread, **_card())

    thread.send.assert_awaited_once()
    sent = thread.send.await_args.kwargs
    assert sent["embed"].title == "✅ 応答完了 by Claude opus5.5 medium"
    # The 5-hour window has less left (91%) than the weekly one (93%).
    assert sent["embed"].description == "プランMax / 残り91% / リセット9/11(金) 03:09"
    thread.send.return_value.edit.assert_not_awaited()

    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    await sent["view"].children[0].callback(interaction)
    details = interaction.response.send_message.await_args.kwargs["embed"].description
    assert SID in details
    assert "10.9秒" in details
    assert "入力 2 / 出力 57" in details
    assert "Anthropic API (direct)" in details
    assert "owner@example.com" in details


async def test_stale_window_is_skipped_for_the_live_one(monkeypatch) -> None:
    monkeypatch.setenv("CCDB_DISPLAY_TZ", "Asia/Tokyo")
    thread = AsyncMock()
    windows = [
        RateLimitInfo("five_hour", "rejected", 1.0, 1789000000),
        RateLimitInfo("seven_day", "allowed", 0.07, 1789162140),
    ]
    await post_claude_completion(thread, **_card(quota_windows=windows))
    assert (
        thread.send.await_args.kwargs["embed"].description
        == "プランMax / 残り93% / リセット9/12(土) 06:29"
    )


async def test_unknown_effort_and_quota_degrade_to_what_is_known() -> None:
    thread = AsyncMock()
    await post_claude_completion(
        thread,
        **_card(model="opus", effort=None, quota_windows=None, account_label="API key (metered)"),
    )
    embed = thread.send.await_args.kwargs["embed"]
    assert embed.title == "✅ 応答完了 by Claude opus"
    assert embed.description == "プランAPI従量"


async def test_nothing_known_leaves_only_the_title() -> None:
    thread = AsyncMock()
    await post_claude_completion(
        thread, **_card(model=None, effort=None, quota_windows=None, account_label=None)
    )
    embed = thread.send.await_args.kwargs["embed"]
    assert embed.title == "✅ 応答完了 by Claude"
    assert embed.description is None


async def test_usage_footer_opt_out_hides_quota(monkeypatch) -> None:
    monkeypatch.setenv("CCDB_USAGE_FOOTER", "off")
    thread = AsyncMock()
    await post_claude_completion(thread, **_card())
    assert thread.send.await_args.kwargs["embed"].description == "プランMax"


def test_short_claude_model() -> None:
    assert short_claude_model("claude-opus-5-5") == "opus5.5"
    assert short_claude_model("claude-sonnet-5-5[1m]") == "sonnet5.5"
    assert short_claude_model("claude-haiku-4-5-20251001") == "haiku4.5"
    assert short_claude_model("claude-fable-5-2") == "fable5.2"
    assert short_claude_model("opus") == "opus"
    assert short_claude_model(None) is None


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
            StreamEvent(
                raw={"model": "claude-opus-5-5"},
                message_type=MessageType.SYSTEM,
                session_id=SID,
            )
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
    assert titles == ["✅ 応答完了 by Claude opus5.5"]
