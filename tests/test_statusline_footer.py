"""Tests for _post_statusline_footer.

The footer now always surfaces the current API provider line (when known),
even if no ``statusLine`` is configured, and appends the statusLine output
below it when present.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from claude_discord.cogs.event_processor import _post_statusline_footer

_STATUSLINE_MOD = "claude_discord.discord_ui.statusline"
_USAGE_MOD = "claude_discord.discord_ui.claude_usage"


async def test_posts_api_line_when_no_statusline_configured() -> None:
    thread = AsyncMock()
    with patch(f"{_STATUSLINE_MOD}.read_statusline_command", return_value=None):
        await _post_statusline_footer(
            thread=thread,
            working_dir=None,
            model="opus",
            context_window=None,
            input_tokens=None,
            cache_creation_tokens=None,
            cache_read_tokens=None,
            api_label="Anthropic API (direct)",
        )
    thread.send.assert_awaited_once()
    body = thread.send.await_args.args[0]
    assert "API: Anthropic API (direct)" in body


async def test_no_post_when_neither_api_label_nor_statusline() -> None:
    thread = AsyncMock()
    with patch(f"{_STATUSLINE_MOD}.read_statusline_command", return_value=None):
        await _post_statusline_footer(
            thread=thread,
            working_dir=None,
            model="opus",
            context_window=None,
            input_tokens=None,
            cache_creation_tokens=None,
            cache_read_tokens=None,
            api_label=None,
        )
    thread.send.assert_not_awaited()


async def test_combines_api_line_and_statusline_output() -> None:
    thread = AsyncMock()
    with (
        patch(f"{_STATUSLINE_MOD}.read_statusline_command", return_value="cmd"),
        patch(
            f"{_STATUSLINE_MOD}.render_statusline",
            new=AsyncMock(return_value="Ctx 45% | quota OK"),
        ),
    ):
        await _post_statusline_footer(
            thread=thread,
            working_dir=None,
            model="opus",
            context_window=200000,
            input_tokens=1000,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            api_label="Azure AI Foundry (jbs-llm-platform)",
        )
    body = thread.send.await_args.args[0]
    assert "API: Azure AI Foundry (jbs-llm-platform)" in body
    assert "Ctx 45%" in body
    # The API line should appear above the statusline output.
    assert body.index("API:") < body.index("Ctx 45%")


async def test_appends_account_label_to_api_line() -> None:
    thread = AsyncMock()
    with (
        patch(f"{_STATUSLINE_MOD}.read_statusline_command", return_value=None),
        patch(f"{_USAGE_MOD}.fetch_claude_usage", new=AsyncMock(return_value=None)),
    ):
        await _post_statusline_footer(
            thread=thread,
            working_dir=None,
            model="opus",
            context_window=None,
            input_tokens=None,
            cache_creation_tokens=None,
            cache_read_tokens=None,
            api_label="Anthropic API (direct)",
            account_label="Max subscription (someone@example.com)",
        )
    body = thread.send.await_args.args[0]
    assert "API: Anthropic API (direct) · Max subscription (someone@example.com)" in body


async def test_appends_usage_lines_below_api_line() -> None:
    thread = AsyncMock()
    usage = {"five_hour": {"utilization": 47.0, "resets_at": None}}
    with (
        patch(f"{_STATUSLINE_MOD}.read_statusline_command", return_value=None),
        patch(f"{_USAGE_MOD}.fetch_claude_usage", new=AsyncMock(return_value=usage)),
    ):
        await _post_statusline_footer(
            thread=thread,
            working_dir=None,
            model="opus",
            context_window=None,
            input_tokens=None,
            cache_creation_tokens=None,
            cache_read_tokens=None,
            api_label="Anthropic API (direct)",
        )
    body = thread.send.await_args.args[0]
    assert "47%" in body
    assert body.index("API:") < body.index("47%")


async def test_usage_failure_does_not_break_footer() -> None:
    """A dead usage endpoint must never cost the user their API line."""
    thread = AsyncMock()
    with (
        patch(f"{_STATUSLINE_MOD}.read_statusline_command", return_value=None),
        patch(f"{_USAGE_MOD}.fetch_claude_usage", new=AsyncMock(side_effect=OSError("down"))),
    ):
        await _post_statusline_footer(
            thread=thread,
            working_dir=None,
            model="opus",
            context_window=None,
            input_tokens=None,
            cache_creation_tokens=None,
            cache_read_tokens=None,
            api_label="Anthropic API (direct)",
        )
    body = thread.send.await_args.args[0]
    assert "API: Anthropic API (direct)" in body


async def test_usage_can_be_disabled_by_env(monkeypatch) -> None:
    thread = AsyncMock()
    monkeypatch.setenv("CCDB_USAGE_FOOTER", "0")
    fetcher = AsyncMock(return_value={"five_hour": {"utilization": 47.0}})
    with (
        patch(f"{_STATUSLINE_MOD}.read_statusline_command", return_value=None),
        patch(f"{_USAGE_MOD}.fetch_claude_usage", new=fetcher),
    ):
        await _post_statusline_footer(
            thread=thread,
            working_dir=None,
            model="opus",
            context_window=None,
            input_tokens=None,
            cache_creation_tokens=None,
            cache_read_tokens=None,
            api_label="Anthropic API (direct)",
        )
    fetcher.assert_not_awaited()
    assert "47%" not in thread.send.await_args.args[0]


async def test_posts_usage_even_without_api_label() -> None:
    thread = AsyncMock()
    usage = {"five_hour": {"utilization": 12.0, "resets_at": None}}
    with (
        patch(f"{_STATUSLINE_MOD}.read_statusline_command", return_value=None),
        patch(f"{_USAGE_MOD}.fetch_claude_usage", new=AsyncMock(return_value=usage)),
    ):
        await _post_statusline_footer(
            thread=thread,
            working_dir=None,
            model="opus",
            context_window=None,
            input_tokens=None,
            cache_creation_tokens=None,
            cache_read_tokens=None,
            api_label=None,
        )
    assert "12%" in thread.send.await_args.args[0]
