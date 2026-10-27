"""Tests for silent-by-default sends (discord_ui/silent.py)."""

from __future__ import annotations

from typing import Any

import discord
import pytest

from claude_discord.discord_ui import silent as silent_mod


class _Recorder:
    """Stands in for the original Messageable.send and records its kwargs."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _self: Any, *args: Any, **kwargs: Any) -> None:
        self.calls.append({"args": args, **kwargs})


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(discord.abc.Messageable, "send", rec)
    silent_mod.install_silent_default()
    return rec


async def _send(*args: Any, **kwargs: Any) -> None:
    await discord.abc.Messageable.send(object(), *args, **kwargs)  # type: ignore[arg-type]


async def test_plain_send_becomes_silent(recorder: _Recorder) -> None:
    await _send("working…")
    assert recorder.calls[-1]["silent"] is True


async def test_embed_only_send_becomes_silent(recorder: _Recorder) -> None:
    await _send(embed=discord.Embed(title="🔧 コマンド実行中"))
    assert recorder.calls[-1]["silent"] is True


async def test_explicit_silent_false_is_kept(recorder: _Recorder) -> None:
    await _send(embed=discord.Embed(title="done"), silent=False)
    assert recorder.calls[-1]["silent"] is False


@pytest.mark.parametrize("content", ["<@123> your reply is needed", "hi <@!456>"])
async def test_user_mention_notifies(recorder: _Recorder, content: str) -> None:
    await _send(content=content)
    assert recorder.calls[-1]["silent"] is False
    await _send(content)
    assert recorder.calls[-1]["silent"] is False


async def test_install_is_idempotent(recorder: _Recorder) -> None:
    wrapped = discord.abc.Messageable.send
    silent_mod.install_silent_default()
    assert discord.abc.Messageable.send is wrapped
    await _send("x")
    assert len(recorder.calls) == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, True), ("true", True), ("1", True), ("false", False), ("0", False), ("no", False)],
)
def test_env_toggle(monkeypatch: pytest.MonkeyPatch, value: str | None, expected: bool) -> None:
    if value is None:
        monkeypatch.delenv("CCDB_SILENT_BY_DEFAULT", raising=False)
    else:
        monkeypatch.setenv("CCDB_SILENT_BY_DEFAULT", value)
    assert silent_mod.silent_by_default_enabled() is expected


# --- the whitelist: messages that must notify say silent=False explicitly ---


async def test_error_notice_notifies_other_notices_do_not() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from claude_code_core.frontend import Notice, NoticeLevel
    from claude_discord.surface import DiscordSurface

    thread = MagicMock(spec=discord.Thread)
    thread.id = 1
    thread.send = AsyncMock(return_value=MagicMock(id=9))
    surface = DiscordSurface(thread)

    await surface.send_notice(Notice(level=NoticeLevel.ERROR, title="Error", body="boom"))
    assert thread.send.call_args.kwargs["silent"] is False
    await surface.send_notice(Notice(level=NoticeLevel.WARNING, title="stall", body="…"))
    assert thread.send.call_args.kwargs["silent"] is True


async def test_completion_cards_notify() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from claude_discord.discord_ui.codex_completion import post_claude_completion, post_completion

    thread = MagicMock()
    thread.send = AsyncMock(return_value=MagicMock(edit=AsyncMock()))
    await post_completion(
        thread,
        model="m",
        session_id="s",
        input_tokens=None,
        output_tokens=None,
        status_mode="off",
        codex_command="codex",
    )
    assert thread.send.call_args.kwargs["silent"] is False

    thread.send.reset_mock()
    await post_claude_completion(
        thread,
        model="m",
        session_id="s",
        input_tokens=None,
        output_tokens=None,
        duration_ms=None,
        cost_usd=None,
        api_label=None,
        account_label=None,
    )
    assert thread.send.call_args.kwargs["silent"] is False


async def test_session_status_dashboard_is_silent(recorder: _Recorder) -> None:
    """The pinned 📊 Session Status card is re-posted on every start; it must not notify."""
    from claude_discord.discord_ui.thread_dashboard import ThreadStatusDashboard

    class _Channel(discord.abc.Messageable):
        async def _get_channel(self) -> Any:  # pragma: no cover - never reached
            return self

    dashboard = ThreadStatusDashboard(_Channel())  # type: ignore[arg-type]
    await dashboard.initialize()
    assert recorder.calls[-1]["silent"] is True
