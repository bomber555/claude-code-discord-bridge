"""Regression tests for the readable Codex completion card."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_code_core.codex_runner import CodexRunner
from claude_code_core.types import MessageType, StreamEvent
from claude_discord.cogs.event_processor import EventProcessor
from claude_discord.cogs.run_config import RunConfig
from claude_discord.discord_ui.codex_completion import (
    CompletionDetailsView,
    completion_embed,
    open_command_activity,
    post_completion,
)
from claude_discord.discord_ui.views import StopView

SID = "01a08a21-04a4-7503-997f-2f2d9dad225d"


def test_card_separates_backend_model_and_usage():
    embed = completion_embed("gpt-6-astra", "アカウント：user@example.com\n週間残量：74%")
    assert embed.title == "✅ 応答完了"
    assert "実行環境：Codex" in embed.description
    assert "モデル：gpt-6-astra" in embed.description
    assert "週間残量：74%" in embed.description
    assert "Tokens" not in embed.description
    assert "prolite" not in embed.description
    assert "未取得" in completion_embed(None).description


async def test_completion_updates_one_card_and_keeps_tokens_in_details():
    thread = AsyncMock()
    with patch(
        "claude_discord.discord_ui.codex_completion.get_codex_status_line",
        AsyncMock(return_value="週間残量：74%"),
    ):
        await post_completion(
            thread,
            model="gpt-6-astra",
            session_id=SID,
            input_tokens=3119,
            output_tokens=281,
            status_mode="on",
            codex_command="codex",
        )
    thread.send.assert_awaited_once()
    initial = thread.send.await_args.kwargs
    assert isinstance(initial["view"], CompletionDetailsView)
    assert "3119" not in initial["embed"].description
    final = thread.send.return_value.edit.await_args.kwargs["embed"]
    assert "週間残量：74%" in final.description
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    await initial["view"].children[0].callback(interaction)
    details = interaction.response.send_message.await_args.kwargs
    assert details["ephemeral"] is True
    assert "3,119" in details["embed"].description
    assert "281" in details["embed"].description
    assert SID in details["embed"].description


@pytest.mark.parametrize("mode", ["off", "auto", "on"])
async def test_missing_quota_does_not_hide_completion(mode):
    thread = AsyncMock()
    with patch(
        "claude_discord.discord_ui.codex_completion.get_codex_status_line",
        AsyncMock(return_value=None),
    ) as fetch:
        await post_completion(
            thread,
            model=None,
            session_id=SID,
            input_tokens=None,
            output_tokens=None,
            status_mode=mode,
            codex_command="codex",
        )
    thread.send.assert_awaited_once()
    if mode == "off":
        fetch.assert_not_called()
    assert "モデル：未取得" in thread.send.await_args.kwargs["embed"].description


async def test_completed_session_removes_running_control():
    view = StopView(MagicMock())
    msg = AsyncMock()
    view.set_message(msg)
    await view.disable()
    msg.delete.assert_awaited_once()


async def test_command_is_hidden_in_details_even_after_completion():
    thread = AsyncMock()
    activity = await open_command_activity(thread, "python3 -c 'print(123)' ")
    sent = thread.send.await_args.kwargs
    assert "python3" not in sent["embed"].title
    assert "python3" not in (sent["embed"].description or "")
    await activity.complete("123")
    final = thread.send.return_value.edit.await_args.kwargs
    assert "完了" in final["embed"].title
    assert "python3" not in final["embed"].title
    assert final["view"] is not None


async def test_default_model_reads_latest_session_context(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    sessions = tmp_path / "sessions" / "2026" / "09" / "10"
    sessions.mkdir(parents=True)
    rollout = sessions / f"rollout-2026-09-10T00-00-00-{SID}.jsonl"
    records = [
        {"type": "turn_context", "payload": {"model": "gpt-5.3-codex"}},
        {"type": "turn_context", "payload": {"model": "gpt-6-astra"}},
        {"type": "event_msg", "payload": {"model": "ignore-this"}},
    ]
    rollout.write_text("\n".join(json.dumps(r) for r in records) + "\n{broken")
    runner = CodexRunner(model=None)
    assert await runner.get_session_model(SID) == "gpt-6-astra"
    assert runner.model is None  # display lookup must never change model selection
    assert await runner.get_session_model("../../secrets") is None
    assert await runner.get_session_model("00000000-0000-0000-0000-000000000000") is None


async def test_codex_stream_emits_only_one_completion_card():
    thread = MagicMock()
    thread.id = 123
    thread.send = AsyncMock()
    runner = CodexRunner()
    runner.get_session_model = AsyncMock(return_value="gpt-6-astra")
    processor = EventProcessor(RunConfig(thread=thread, runner=runner, prompt="test"))
    with patch(
        "claude_discord.discord_ui.codex_completion.get_codex_status_line",
        AsyncMock(return_value=None),
    ):
        await processor.process(
            StreamEvent(raw={}, message_type=MessageType.SYSTEM, session_id=SID)
        )
        await processor.process(
            StreamEvent(
                raw={},
                message_type=MessageType.RESULT,
                is_complete=True,
                text="OK",
                input_tokens=3119,
                output_tokens=281,
            )
        )
        await processor.finalize()
        # Let the non-blocking quota update finish.
        import asyncio

        await asyncio.sleep(0)
    titles = [c.kwargs["embed"].title for c in thread.send.await_args_list if "embed" in c.kwargs]
    assert titles == ["✅ 応答完了"]
    assert all("CLI default" not in str(c) for c in thread.send.await_args_list)


async def test_quota_exception_keeps_the_success_card():
    thread = AsyncMock()
    with patch(
        "claude_discord.discord_ui.codex_completion.get_codex_status_line",
        AsyncMock(side_effect=RuntimeError("offline")),
    ):
        await post_completion(
            thread,
            model="gpt-6-astra",
            session_id=SID,
            input_tokens=None,
            output_tokens=None,
            status_mode="on",
            codex_command="codex",
        )
    thread.send.assert_awaited_once()
    embed = thread.send.return_value.edit.await_args.kwargs["embed"]
    assert embed.title == "✅ 応答完了"
    assert "未取得" in embed.description


async def test_running_control_falls_back_to_finished_if_delete_fails():
    import discord

    view = StopView(MagicMock())
    msg = AsyncMock()
    msg.delete.side_effect = discord.Forbidden(MagicMock(), "cannot delete")
    await view.disable(msg)
    msg.edit.assert_awaited_once_with(content="-# セッション終了", view=None)


async def test_model_never_falls_back_to_an_older_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    rollout = sessions / f"rollout-{SID}.jsonl"
    records = [
        {"type": "turn_context", "payload": {"model": "old-model"}},
        {"type": "turn_context", "payload": {}},
    ]
    rollout.write_text("\n".join(json.dumps(r) for r in records))
    assert await CodexRunner(model="configured-model").get_session_model(SID) is None


async def test_model_lookup_does_not_read_unbounded_history(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    rollout = sessions / f"rollout-{SID}.jsonl"
    rollout.write_text(
        json.dumps({"type": "turn_context", "payload": {"model": "outside-tail"}})
        + "\n"
        + "x" * (8 * 1024 * 1024 + 1)
        + "\n"
    )
    assert await CodexRunner().get_session_model(SID) is None
