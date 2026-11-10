"""Compact CLI completion cards, with operational details behind a button."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

import discord

from .claude_usage import build_quota_summary, usage_footer_enabled
from .embeds import COLOR_ERROR, COLOR_INFO, COLOR_SUCCESS
from .engine_status import get_codex_status_line

if TYPE_CHECKING:
    from claude_code_core.types import RateLimitInfo

logger = logging.getLogger(__name__)


def _plain(value: str) -> str:
    return discord.utils.escape_markdown(" ".join(value.split()))


def completion_embed(
    model: str | None,
    status_line: str | None = None,
    *,
    backend: str = "Codex",
) -> discord.Embed:
    """Keep the actual model separate from the execution environment and quota."""
    rows = [f"実行環境：{_plain(backend)}", f"モデル：{_plain(model) if model else '未取得'}"]
    if status_line:
        rows.append(discord.utils.escape_markdown(status_line))
    return discord.Embed(
        title="✅ 応答完了", description="\n".join(rows)[:4096], color=COLOR_SUCCESS
    )


class CompletionDetailsView(discord.ui.View):
    """Show technical details only to the reader who asks for them."""

    def __init__(self, details: str) -> None:
        super().__init__(timeout=3600)
        self.details = details

    @discord.ui.button(label="詳細", style=discord.ButtonStyle.secondary)
    async def show_details(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="詳細",
                description=discord.utils.escape_markdown(self.details)[:4096],
                color=COLOR_INFO,
            ),
            ephemeral=True,
        )


async def post_completion(
    thread: discord.Thread | discord.TextChannel,
    *,
    model: str | None,
    session_id: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    status_mode: str,
    codex_command: str,
) -> None:
    """Post immediately, then enrich that same card when quota becomes available.

    The caller runs this in the background. A quota failure must never remove
    the completion or create a second message.
    """
    details = [f"セッションID：{session_id or '未取得'}"]
    if input_tokens is not None and output_tokens is not None:
        details.append(f"今回のトークン使用量：入力 {input_tokens:,} / 出力 {output_tokens:,}")
        details.append("利用枠の残量とは別の数値です。")
    view = CompletionDetailsView("\n".join(details))
    try:
        message = await thread.send(embed=completion_embed(model), view=view, silent=False)
        if status_mode not in {"auto", "on"}:
            return
        try:
            status = await get_codex_status_line(codex_command)
        except Exception:
            logger.debug("Codex quota lookup failed", exc_info=True)
            status = None
        if status is None and status_mode == "on":
            status = "利用枠の残量：未取得"
        if status:
            await message.edit(embed=completion_embed(model, status))
    except discord.HTTPException:
        logger.warning("Could not deliver Codex completion card", exc_info=True)


_CLAUDE_MODEL_ID = re.compile(r"^claude-([a-z]+)-(\d+(?:-\d{1,2})?)(?:-\d{8})?(?:\[.*\])?$")


def short_claude_model(model: str | None) -> str | None:
    """``claude-opus-5-5`` → ``opus5.5``; anything else is returned as given."""
    if not model:
        return None
    match = _CLAUDE_MODEL_ID.match(model.strip())
    if match is None:
        return model.strip()
    family, version = match.groups()
    return f"{family}{version.replace('-', '.')}"


def plan_name(account_label: str | None) -> str | None:
    """``Max subscription (a@b.c)`` → ``Max``; ``API key (metered)`` → ``API従量``."""
    if not account_label:
        return None
    match = re.match(r"^(.+?) subscription\b", account_label)
    if match:
        return match.group(1)
    if account_label.startswith("API key"):
        return "API従量"
    return re.sub(r"\s*\(.*\)$", "", account_label) or None


def claude_completion_embed(
    model: str | None, effort: str | None, summary: str | None
) -> discord.Embed:
    """``✅ 応答完了 by opus5.5 medium`` with one plan/quota line."""
    title = " ".join(p for p in (model, effort) if p)
    title = f"✅ 応答完了 by {title}" if title else "✅ 応答完了"
    return discord.Embed(
        title=_plain(title)[:256],
        description=discord.utils.escape_markdown(summary) if summary else None,
        color=COLOR_SUCCESS,
    )


async def post_claude_completion(
    thread: discord.Thread | discord.TextChannel,
    *,
    model: str | None,
    session_id: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    duration_ms: int | None,
    cost_usd: float | None,
    api_label: str | None,
    account_label: str | None,
    quota_windows: Sequence[RateLimitInfo] | None = None,
    effort: str | None = None,
    now: int | None = None,
) -> None:
    """Post one Claude card, with the stored quota windows when there are any."""
    details = [f"セッションID：{session_id or '未取得'}"]
    if duration_ms is not None:
        details.append(f"実行時間：{duration_ms / 1000:.1f}秒")
    if input_tokens is not None and output_tokens is not None:
        details.append(f"今回のトークン使用量：入力 {input_tokens:,} / 出力 {output_tokens:,}")
        details.append("利用枠の残量とは別の数値です。")
    if cost_usd is not None:
        details.append(f"推定費用：${cost_usd:.4f}")
    if api_label:
        details.append(f"接続先：{api_label}")
    if account_label:
        details.append(f"アカウント：{account_label}")

    view = CompletionDetailsView("\n".join(details))
    parts: list[str] = []
    plan = plan_name(account_label)
    if plan:
        parts.append(f"プラン{plan}")
    if quota_windows and usage_footer_enabled():
        quota = build_quota_summary(quota_windows, now=now)
        if quota:
            parts.append(quota)
    try:
        await thread.send(
            embed=claude_completion_embed(
                short_claude_model(model), effort, " / ".join(parts) or None
            ),
            view=view,
            silent=False,
        )
    except discord.HTTPException:
        logger.warning("Could not deliver Claude completion card", exc_info=True)


class CommandActivity:
    """Keep shell commands and their output in details throughout the lifecycle."""

    def __init__(self, message: discord.Message, command: str, view: CompletionDetailsView) -> None:
        self._message = message
        self._command = command
        self._view = view
        self._finished = False

    async def update(self, detail: str) -> None:
        if not self._finished:
            self._view.details = f"コマンド：\n{self._command}\n\n{detail}"

    async def complete(self, result: str | None, *, ok: bool = True) -> None:
        if self._finished:
            return
        self._finished = True
        self._view.details = (
            f"コマンド：\n{self._command[:1800]}\n\n結果：\n{(result or '')[:1800]}"
        )
        await self._edit("🔧 コマンド完了" if ok else "⚠️ コマンド失敗", ok=ok)

    async def cancel(self) -> None:
        if self._finished:
            return
        self._finished = True
        await self._edit("⏹ コマンド終了", ok=False)

    async def _edit(self, title: str, *, ok: bool) -> None:
        try:
            await self._message.edit(
                embed=discord.Embed(title=title, color=COLOR_INFO if ok else COLOR_ERROR),
                view=self._view,
            )
        except discord.HTTPException:
            logger.debug("Could not update command activity", exc_info=True)


async def open_command_activity(
    thread: discord.Thread | discord.TextChannel, command: str
) -> CommandActivity:
    view = CompletionDetailsView(f"コマンド：\n{command}")
    message = await thread.send(
        embed=discord.Embed(title="🔧 コマンド実行中", color=COLOR_INFO), view=view
    )
    return CommandActivity(message, command, view)
