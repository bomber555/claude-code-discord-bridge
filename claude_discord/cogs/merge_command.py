"""``/merge`` — merge a pull request from Discord, by a person pressing a button.

The point of this Cog is who decides. A session may *ask* for a merge
(``POST /api/threads/{id}/merge-request``), which only posts the button; nothing
reachable from a session performs the merge. The merge runs when a Discord user
listed in ``CCDB_MERGE_USER_IDS`` presses it, so "a person looked and decided"
stays true without opening GitHub on a phone. Do not add a REST route that
merges: it would turn this approval into a formality.

Each button is bound to the head SHA that was on screen. GitHub refuses the merge
if the branch has moved since, so a late push cannot ride on an earlier decision.
State is re-read on every press, never trusted from the message.

Off unless ``CCDB_MERGE_REPO`` and ``CCDB_MERGE_USER_IDS`` are both set.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)

_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GH_TIMEOUT_SECONDS = 30
# GitHub computes ``mergeable`` lazily and answers null until it has.
_MERGEABLE_RETRIES = 3
_MERGEABLE_RETRY_SECONDS = 2.0
_MAX_TITLE_CHARS = 200
_OK_CONCLUSIONS = {"success", "neutral", "skipped"}


class GitHubError(Exception):
    """A ``gh api`` call failed; the message is safe to show in Discord."""


@dataclass(frozen=True)
class MergeConfig:
    repo: str
    user_ids: frozenset[int]
    bases: tuple[str, ...]
    gh_bin: str
    ignored_checks: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls) -> MergeConfig | None:
        """Read the configuration; anything missing or malformed leaves ``/merge`` off."""
        repo = os.getenv("CCDB_MERGE_REPO", "").strip()
        raw_ids = os.getenv("CCDB_MERGE_USER_IDS", "").strip()
        if not repo and not raw_ids:
            return None
        if not _REPO_PATTERN.match(repo):
            logger.warning("/merge disabled: CCDB_MERGE_REPO is not owner/name")
            return None
        try:
            user_ids = frozenset(int(part) for part in raw_ids.split(",") if part.strip())
        except ValueError:
            logger.warning("/merge disabled: CCDB_MERGE_USER_IDS must be integers")
            return None
        if not user_ids:
            logger.warning("/merge disabled: CCDB_MERGE_USER_IDS is empty")
            return None
        # systemd's default PATH does not include ~/.local/bin, where gh often lives.
        gh_bin = (
            os.getenv("CCDB_MERGE_GH_BIN")
            or shutil.which("gh")
            or os.path.expanduser("~/.local/bin/gh")
        )
        bases = tuple(
            part.strip()
            for part in os.getenv("CCDB_MERGE_BASES", "main,master").split(",")
            if part.strip()
        )
        ignored_checks = frozenset(
            part.strip()
            for part in os.getenv("CCDB_MERGE_IGNORE_CHECKS", "").split(",")
            if part.strip()
        )
        return cls(
            repo=repo,
            user_ids=user_ids,
            bases=bases,
            gh_bin=gh_bin,
            ignored_checks=ignored_checks,
        )


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    url: str
    author: str
    base: str
    head: str
    head_sha: str
    state: str
    merged: bool
    draft: bool
    mergeable: bool | None
    mergeable_state: str
    additions: int
    deletions: int
    changed_files: int
    checks_passed: int = 0
    checks_failed: tuple[str, ...] = ()
    checks_pending: tuple[str, ...] = ()
    checks_ignored: tuple[str, ...] = ()

    @property
    def is_open(self) -> bool:
        return self.state == "open" and not self.merged


def find_blockers(pr: PullRequest, bases: tuple[str, ...]) -> list[str]:
    """Return why this PR must not be merged now; empty means the button may merge."""
    blockers: list[str] = []
    if pr.merged:
        return ["マージ済みです"]
    if pr.state != "open":
        return ["クローズ済みです"]
    if pr.draft:
        blockers.append("Draft です")
    if not any(fnmatch.fnmatchcase(pr.base, pattern) for pattern in bases):
        blockers.append(f"マージ先 `{pr.base}` は許可されていません")
    if pr.mergeable is False:
        blockers.append("コンフリクトがあります")
    elif pr.mergeable is None:
        blockers.append("GitHub がマージ可否を判定中です")
    elif pr.mergeable_state == "blocked":
        blockers.append("GitHub のルールで止められています")
    if pr.checks_failed:
        blockers.append("チェック失敗: " + ", ".join(pr.checks_failed[:5]))
    if pr.checks_pending:
        blockers.append("チェック実行中: " + ", ".join(pr.checks_pending[:5]))
    return blockers


def render(pr: PullRequest, blockers: list[str]) -> str:
    """Build the message body. Titles are untrusted; callers send with mentions disabled."""
    title = pr.title.replace("\n", " ")[:_MAX_TITLE_CHARS]
    if pr.checks_failed or pr.checks_pending:
        checks = f"成功 {pr.checks_passed} / 失敗 {len(pr.checks_failed)} / "
        checks += f"実行中 {len(pr.checks_pending)}"
    elif pr.checks_passed:
        checks = f"すべて成功 ({pr.checks_passed} 件)"
    elif pr.checks_ignored:
        checks = "判定対象のチェックなし"
    else:
        checks = "チェックなし"
    lines = [
        f"**PR #{pr.number}** {title}",
        f"<{pr.url}>",
        f"`{pr.base}` ← `{pr.head}` / 作成者 {pr.author}",
        f"+{pr.additions} -{pr.deletions} ({pr.changed_files} ファイル) / {checks}",
        f"head `{pr.head_sha[:7]}`",
    ]
    if pr.checks_ignored:
        lines.append("判定から除外 (未成功): " + ", ".join(pr.checks_ignored))
    if blockers:
        lines.append("⚠️ " + " / ".join(blockers))
    return "\n".join(lines)


class GitHubClient:
    """The few ``gh api`` calls ``/merge`` needs. ``gh`` holds the credential."""

    def __init__(self, config: MergeConfig) -> None:
        self._config = config

    async def _api(self, *args: str) -> Any:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._config.gh_bin,
                "api",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise GitHubError(f"gh を起動できません: {exc.strerror}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), _GH_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise GitHubError("GitHub の応答がタイムアウトしました") from exc
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace").strip().splitlines()
            raise GitHubError(detail[0][:300] if detail else f"gh exit {proc.returncode}")
        return json.loads(stdout) if stdout.strip() else None

    async def _checks(self, sha: str) -> dict[str, Any]:
        repo = self._config.repo
        ignored = self._config.ignored_checks
        passed = 0
        failed: list[str] = []
        pending: list[str] = []
        skipped: list[str] = []

        def not_passed(name: str, bucket: list[str]) -> None:
            (skipped if name in ignored else bucket).append(name)

        runs = await self._api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100")
        for run in runs.get("check_runs", []):
            if run.get("status") != "completed":
                not_passed(run.get("name", "?"), pending)
            elif run.get("conclusion") in _OK_CONCLUSIONS:
                passed += 1
            else:
                not_passed(run.get("name", "?"), failed)
        combined = await self._api(f"repos/{repo}/commits/{sha}/status")
        for status in combined.get("statuses", []):
            state = status.get("state")
            if state == "success":
                passed += 1
            elif state == "pending":
                not_passed(status.get("context", "?"), pending)
            else:
                not_passed(status.get("context", "?"), failed)
        return {
            "checks_passed": passed,
            "checks_failed": tuple(failed),
            "checks_pending": tuple(pending),
            "checks_ignored": tuple(skipped),
        }

    async def get_pull_request(self, number: int) -> PullRequest:
        data: dict[str, Any] = {}
        for attempt in range(_MERGEABLE_RETRIES):
            data = await self._api(f"repos/{self._config.repo}/pulls/{number}")
            if data.get("mergeable") is not None or data.get("state") != "open":
                break
            if attempt + 1 < _MERGEABLE_RETRIES:
                await asyncio.sleep(_MERGEABLE_RETRY_SECONDS)
        sha = data["head"]["sha"]
        checks = await self._checks(sha)
        return PullRequest(
            number=number,
            title=data.get("title") or "",
            url=data["html_url"],
            author=(data.get("user") or {}).get("login", "?"),
            base=data["base"]["ref"],
            head=data["head"]["ref"],
            head_sha=sha,
            state=data["state"],
            merged=bool(data.get("merged")),
            draft=bool(data.get("draft")),
            mergeable=data.get("mergeable"),
            mergeable_state=data.get("mergeable_state") or "",
            additions=int(data.get("additions") or 0),
            deletions=int(data.get("deletions") or 0),
            changed_files=int(data.get("changed_files") or 0),
            **checks,
        )

    async def merge(self, number: int, sha: str) -> None:
        """Merge exactly ``sha``; GitHub answers 409 if the branch has moved."""
        await self._api(
            "-X",
            "PUT",
            f"repos/{self._config.repo}/pulls/{number}/merge",
            "-f",
            f"sha={sha}",
            "-f",
            "merge_method=merge",
        )

    async def comment(self, number: int, body: str) -> None:
        await self._api(
            "-X",
            "POST",
            f"repos/{self._config.repo}/issues/{number}/comments",
            "-f",
            f"body={body}",
        )


class MergeButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"ccdb-merge:(?P<pr>[0-9]+):(?P<sha>[0-9a-f]{40})",
):
    """A button that survives a bot restart: its target lives in the custom_id."""

    def __init__(self, number: int, sha: str) -> None:
        super().__init__(
            discord.ui.Button(
                label=f"PR #{number} をマージ",
                style=discord.ButtonStyle.green,
                custom_id=f"ccdb-merge:{number}:{sha}",
            )
        )
        self.number = number
        self.sha = sha

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> MergeButton:
        return cls(int(match["pr"]), match["sha"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("MergeCommandCog")  # type: ignore[attr-defined]
        if cog is None:
            await interaction.response.send_message("/merge は無効です", ephemeral=True)
            return
        await cog.handle_press(interaction, self.number, self.sha)


def _view_for(pr: PullRequest) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if pr.is_open:
        view.add_item(MergeButton(pr.number, pr.head_sha))
    return view


class MergeCommandCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        config: MergeConfig,
        client: GitHubClient | None = None,
    ) -> None:
        self.bot = bot
        self.config = config
        self.client = client or GitHubClient(config)
        self._in_progress: set[int] = set()

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(MergeButton)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(MergeButton)

    def _is_allowed(self, user_id: int) -> bool:
        return user_id in self.config.user_ids

    @app_commands.command(name="merge", description="Show a pull request with a merge button")
    @app_commands.describe(pr="Pull request number")
    async def merge_command(
        self, interaction: discord.Interaction, pr: app_commands.Range[int, 1]
    ) -> None:
        if not self._is_allowed(interaction.user.id):
            await interaction.response.send_message(
                "このコマンドを使う権限がありません", ephemeral=True
            )
            return
        await interaction.response.defer()
        try:
            pull = await self.client.get_pull_request(pr)
        except GitHubError as exc:
            await interaction.followup.send(f"PR #{pr} を読めませんでした: {exc}")
            return
        blockers = find_blockers(pull, self.config.bases)
        await interaction.followup.send(
            render(pull, blockers),
            view=_view_for(pull),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def post_request(self, channel: Any, number: int) -> list[str]:
        """Post the button for a session's merge request. This never merges.

        Returns the current blockers so the caller can tell the session why a
        press would be refused. Raises :class:`GitHubError` if the PR is unreadable.
        """
        pull = await self.client.get_pull_request(number)
        blockers = find_blockers(pull, self.config.bases)
        await channel.send(
            render(pull, blockers),
            view=_view_for(pull),
            allowed_mentions=discord.AllowedMentions.none(),
            silent=False,
        )
        return blockers

    async def handle_press(self, interaction: discord.Interaction, number: int, sha: str) -> None:
        user = interaction.user
        if not self._is_allowed(user.id):
            logger.warning("Merge button for PR #%s refused for user %s", number, user.id)
            await interaction.response.send_message(
                "このボタンを押す権限がありません", ephemeral=True
            )
            return
        if number in self._in_progress:
            await interaction.response.send_message("マージ処理中です", ephemeral=True)
            return
        self._in_progress.add(number)
        try:
            await interaction.response.defer()
            await self._merge(interaction, number, sha)
        finally:
            self._in_progress.discard(number)

    async def _merge(self, interaction: discord.Interaction, number: int, sha: str) -> None:
        async def tell(text: str) -> None:
            await interaction.followup.send(text, ephemeral=True)

        no_mentions = discord.AllowedMentions.none()
        try:
            pull = await self.client.get_pull_request(number)
        except GitHubError as exc:
            await tell(f"PR #{number} を読めませんでした: {exc}")
            return

        blockers = find_blockers(pull, self.config.bases)
        await interaction.edit_original_response(
            content=render(pull, blockers), view=_view_for(pull), allowed_mentions=no_mentions
        )
        if pull.head_sha != sha:
            await tell(
                f"表示後に push がありました (`{sha[:7]}` → `{pull.head_sha[:7]}`)。"
                "内容を確認してもう一度押してください"
            )
            return
        if blockers:
            await tell("マージしませんでした: " + " / ".join(blockers))
            return

        user = interaction.user
        try:
            await self.client.merge(number, sha)
        except GitHubError as exc:
            logger.warning("Merge of PR #%s failed: %s", number, exc)
            await tell(f"マージに失敗しました: {exc}")
            return
        logger.info("PR #%s merged at %s by Discord user %s", number, sha, user.id)

        done = render(pull, []) + f"\n✅ {user.display_name} がマージしました"
        await interaction.edit_original_response(
            content=done, view=discord.ui.View(timeout=None), allowed_mentions=no_mentions
        )
        # The merge is already done; a failed note must not read as a failed merge.
        safe_name = re.sub(r"[@`]", "", user.display_name)
        try:
            await self.client.comment(
                number,
                f"Discord の承認ボタンでマージしました。押した人: {safe_name} "
                f"(Discord ID {user.id}) / head `{sha}`",
            )
        except GitHubError as exc:
            logger.warning("Could not comment on merged PR #%s: %s", number, exc)
            await tell(f"マージは完了しましたが、PR へのコメントに失敗しました: {exc}")
