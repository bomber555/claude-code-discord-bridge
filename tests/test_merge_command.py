"""Tests for ``/merge``.

What matters here is who can cause a merge and what gets merged: only a listed
user's press, only the SHA that was on screen, and never a PR with a blocker.
The GitHub client runs against a real subprocess (a fake ``gh``) so the argv that
reaches GitHub is asserted rather than assumed.
"""

from __future__ import annotations

import json
import stat
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer

from claude_discord.cogs.merge_command import (
    GitHubClient,
    GitHubError,
    MergeButton,
    MergeCommandCog,
    MergeConfig,
    PullRequest,
    find_blockers,
    render,
)
from claude_discord.ext.api_server import ApiServer

SHA = "a" * 40
OTHER_SHA = "b" * 40
OWNER = 111
BASES = ("master", "release/*")
CONFIG = MergeConfig(repo="o/r", user_ids=frozenset({OWNER}), bases=BASES, gh_bin="gh")
PR = PullRequest(
    number=7,
    title="Fix",
    url="https://github.com/o/r/pull/7",
    author="kls9-bot",
    base="master",
    head="feature",
    head_sha=SHA,
    state="open",
    merged=False,
    draft=False,
    mergeable=True,
    mergeable_state="clean",
    additions=1,
    deletions=2,
    changed_files=3,
    checks_passed=1,
)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"merged": True, "state": "closed"}, "マージ済み"),
        ({"state": "closed"}, "クローズ済み"),
        ({"draft": True}, "Draft"),
        ({"base": "develop"}, "許可されていません"),
        ({"mergeable": False}, "コンフリクト"),
        ({"mergeable": None}, "判定中"),
        ({"mergeable_state": "blocked"}, "ルール"),
        ({"checks_failed": ("smoke",)}, "チェック失敗: smoke"),
        ({"checks_pending": ("smoke",)}, "チェック実行中: smoke"),
    ],
)
def test_blockers(change: dict, expected: str) -> None:
    blockers = find_blockers(replace(PR, **change), BASES)
    assert any(expected in text for text in blockers)


@pytest.mark.parametrize("base", ["master", "release/v4.1.0-260514"])
def test_allowed_bases_have_no_blockers(base: str) -> None:
    assert find_blockers(replace(PR, base=base), BASES) == []


def test_ignored_failure_does_not_block_but_stays_visible() -> None:
    pr = replace(PR, checks_passed=0, checks_ignored=("smoke",))
    assert find_blockers(pr, BASES) == []
    assert "smoke" in render(pr, [])


def test_render_says_when_there_are_no_checks() -> None:
    assert "チェックなし" in render(replace(PR, checks_passed=0), [])


class TestConfig:
    def _env(self, monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
        for name in ("REPO", "USER_IDS", "BASES", "GH_BIN", "IGNORE_CHECKS"):
            monkeypatch.delenv(f"CCDB_MERGE_{name}", raising=False)
        for name, value in values.items():
            monkeypatch.setenv(f"CCDB_MERGE_{name}", value)

    def test_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._env(monkeypatch)
        assert MergeConfig.from_env() is None

    @pytest.mark.parametrize(
        "values",
        [
            {"REPO": "o/r"},
            {"USER_IDS": "1"},
            {"REPO": "o/r", "USER_IDS": "abc"},
            {"REPO": "o/r", "USER_IDS": " , "},
            {"REPO": "not a repo", "USER_IDS": "1"},
        ],
    )
    def test_incomplete_config_stays_off(
        self, monkeypatch: pytest.MonkeyPatch, values: dict[str, str]
    ) -> None:
        self._env(monkeypatch, **values)
        assert MergeConfig.from_env() is None

    def test_full_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._env(
            monkeypatch, REPO="o/r", USER_IDS="1, 2", BASES="master, release/*", GH_BIN="/x/gh"
        )
        config = MergeConfig.from_env()
        assert config == MergeConfig(
            repo="o/r", user_ids=frozenset({1, 2}), bases=BASES, gh_bin="/x/gh"
        )


def make_interaction(user_id: int = OWNER) -> MagicMock:
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.user.display_name = "@po`"
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


def make_cog(pr: PullRequest = PR) -> tuple[MergeCommandCog, MagicMock]:
    client = MagicMock()
    client.get_pull_request = AsyncMock(return_value=pr)
    client.merge = AsyncMock()
    client.comment = AsyncMock()
    return MergeCommandCog(MagicMock(), CONFIG, client), client


class TestPress:
    async def test_listed_user_merges_the_sha_on_screen(self) -> None:
        cog, client = make_cog()
        interaction = make_interaction()
        await cog.handle_press(interaction, 7, SHA)
        client.merge.assert_awaited_once_with(7, SHA)
        final = interaction.edit_original_response.await_args.kwargs
        assert "✅" in final["content"]
        assert not final["view"].children

    async def test_comment_records_who_pressed_without_pinging(self) -> None:
        cog, client = make_cog()
        await cog.handle_press(make_interaction(), 7, SHA)
        body = client.comment.await_args.args[1]
        assert str(OWNER) in body and SHA in body
        assert "@" not in body

    async def test_other_user_cannot_merge(self) -> None:
        cog, client = make_cog()
        interaction = make_interaction(user_id=222)
        await cog.handle_press(interaction, 7, SHA)
        client.merge.assert_not_awaited()
        client.get_pull_request.assert_not_awaited()
        assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True

    async def test_push_after_display_refuses_and_offers_new_button(self) -> None:
        cog, client = make_cog(replace(PR, head_sha=OTHER_SHA))
        interaction = make_interaction()
        await cog.handle_press(interaction, 7, SHA)
        client.merge.assert_not_awaited()
        button = interaction.edit_original_response.await_args.kwargs["view"].children[0]
        assert button.custom_id == f"ccdb-merge:7:{OTHER_SHA}"
        assert "push" in interaction.followup.send.await_args.args[0]

    async def test_blocker_found_at_press_time_refuses(self) -> None:
        cog, client = make_cog(replace(PR, checks_failed=("smoke",)))
        interaction = make_interaction()
        await cog.handle_press(interaction, 7, SHA)
        client.merge.assert_not_awaited()
        assert "smoke" in interaction.followup.send.await_args.args[0]

    async def test_merge_failure_is_not_reported_as_success(self) -> None:
        cog, client = make_cog()
        client.merge.side_effect = GitHubError("HTTP 405")
        interaction = make_interaction()
        await cog.handle_press(interaction, 7, SHA)
        assert "HTTP 405" in interaction.followup.send.await_args.args[0]
        assert "✅" not in interaction.edit_original_response.await_args.kwargs["content"]
        client.comment.assert_not_awaited()

    async def test_comment_failure_still_reports_the_merge(self) -> None:
        cog, client = make_cog()
        client.comment.side_effect = GitHubError("HTTP 500")
        interaction = make_interaction()
        await cog.handle_press(interaction, 7, SHA)
        assert "✅" in interaction.edit_original_response.await_args.kwargs["content"]
        assert "マージは完了" in interaction.followup.send.await_args.args[0]

    async def test_second_press_during_merge_is_refused(self) -> None:
        cog, client = make_cog()
        cog._in_progress.add(7)
        interaction = make_interaction()
        await cog.handle_press(interaction, 7, SHA)
        client.get_pull_request.assert_not_awaited()

    async def test_lock_is_released_after_failure(self) -> None:
        cog, client = make_cog()
        client.get_pull_request.side_effect = GitHubError("boom")
        await cog.handle_press(make_interaction(), 7, SHA)
        assert 7 not in cog._in_progress


class TestCommand:
    async def test_other_user_is_refused(self) -> None:
        cog, client = make_cog()
        interaction = make_interaction(user_id=222)
        await cog.merge_command.callback(cog, interaction, 7)
        client.get_pull_request.assert_not_awaited()

    async def test_shows_button_with_mentions_disabled(self) -> None:
        cog, client = make_cog(replace(PR, title="@everyone"))
        interaction = make_interaction()
        await cog.merge_command.callback(cog, interaction, 7)
        kwargs = interaction.followup.send.await_args.kwargs
        assert kwargs["view"].children[0].custom_id == f"ccdb-merge:7:{SHA}"
        assert kwargs["allowed_mentions"].everyone is False
        client.merge.assert_not_awaited()

    async def test_merged_pr_has_no_button(self) -> None:
        cog, _ = make_cog(replace(PR, merged=True, state="closed"))
        interaction = make_interaction()
        await cog.merge_command.callback(cog, interaction, 7)
        assert not interaction.followup.send.await_args.kwargs["view"].children


def test_button_custom_id_round_trips() -> None:
    button = MergeButton(7, SHA)
    match = MergeButton.__discord_ui_compiled_template__.fullmatch(button.item.custom_id)
    assert match is not None
    assert (match["pr"], match["sha"]) == ("7", SHA)


class TestRequestEndpoint:
    """A session may ask for the button; it must have no way to cause the merge."""

    async def _client(self, cog: MergeCommandCog | None) -> tuple[TestClient, MagicMock]:
        channel = MagicMock()
        channel.send = AsyncMock()
        bot = MagicMock()
        bot.cogs = {"MergeCommandCog": cog} if cog else {}
        bot.get_channel.return_value = channel
        server = ApiServer(repo=MagicMock(), bot=bot, default_channel_id=1)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        return client, channel

    async def test_posts_the_button_and_does_not_merge(self) -> None:
        cog, gh = make_cog()
        client, channel = await self._client(cog)
        try:
            response = await client.post("/api/threads/5/merge-request", json={"pr": 7})
            assert response.status == 201
            assert (await response.json())["blockers"] == []
        finally:
            await client.close()
        assert channel.send.await_args.kwargs["view"].children[0].custom_id.startswith("ccdb-merge")
        gh.merge.assert_not_awaited()

    async def test_reports_blockers(self) -> None:
        cog, _ = make_cog(replace(PR, draft=True))
        client, _ = await self._client(cog)
        try:
            response = await client.post("/api/threads/5/merge-request", json={"pr": 7})
            assert "Draft" in (await response.json())["blockers"][0]
        finally:
            await client.close()

    @pytest.mark.parametrize("body", [{}, {"pr": "7"}, {"pr": 0}, {"pr": True}, []])
    async def test_rejects_bad_body(self, body: object) -> None:
        cog, _ = make_cog()
        client, channel = await self._client(cog)
        try:
            response = await client.post("/api/threads/5/merge-request", json=body)
            assert response.status == 400
        finally:
            await client.close()
        channel.send.assert_not_awaited()

    async def test_unconfigured_is_503(self) -> None:
        client, _ = await self._client(None)
        try:
            response = await client.post("/api/threads/5/merge-request", json={"pr": 7})
            assert response.status == 503
        finally:
            await client.close()

    async def test_unreadable_pr_is_502_and_posts_nothing(self) -> None:
        cog, gh = make_cog()
        gh.get_pull_request.side_effect = GitHubError("HTTP 404")
        client, channel = await self._client(cog)
        try:
            response = await client.post("/api/threads/5/merge-request", json={"pr": 7})
            assert response.status == 502
        finally:
            await client.close()
        channel.send.assert_not_awaited()


FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
responses = json.load(open(os.environ["FAKE_GH_RESPONSES"]))
for key, value in responses.items():
    if any(key in arg for arg in args):
        if isinstance(value, str):
            print(value, file=sys.stderr)
            sys.exit(1)
        print(json.dumps(value))
        sys.exit(0)
print("{}")
"""


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    gh = tmp_path / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "log"
    log.write_text("")
    responses = tmp_path / "responses.json"
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    monkeypatch.setenv("FAKE_GH_RESPONSES", str(responses))

    def setup(payload: dict) -> tuple[GitHubClient, Path]:
        responses.write_text(json.dumps(payload))
        return GitHubClient(replace(CONFIG, gh_bin=str(gh))), log

    return setup


PR_JSON = {
    "title": "T",
    "html_url": "https://x/7",
    "user": {"login": "a"},
    "base": {"ref": "master"},
    "head": {"ref": "f", "sha": SHA},
    "state": "open",
    "merged": False,
    "draft": False,
    "mergeable": True,
    "mergeable_state": "clean",
    "additions": 1,
    "deletions": 1,
    "changed_files": 1,
}


class TestGitHubClient:
    async def test_merge_pins_the_sha(self, fake_gh) -> None:  # noqa: ANN001
        client, log = fake_gh({})
        await client.merge(7, SHA)
        args = json.loads(log.read_text().splitlines()[0])
        assert args == [
            "api",
            "-X",
            "PUT",
            "repos/o/r/pulls/7/merge",
            "-f",
            f"sha={SHA}",
            "-f",
            "merge_method=merge",
        ]

    async def test_error_is_raised_with_gh_message(self, fake_gh) -> None:  # noqa: ANN001
        client, _ = fake_gh({"merge": "gh: Head branch was modified (HTTP 409)"})
        with pytest.raises(GitHubError, match="HTTP 409"):
            await client.merge(7, SHA)

    async def test_missing_gh_is_a_github_error(self) -> None:
        client = GitHubClient(replace(CONFIG, gh_bin="/nonexistent/gh"))
        with pytest.raises(GitHubError):
            await client.merge(7, SHA)

    async def test_reads_checks_from_both_apis(self, fake_gh) -> None:  # noqa: ANN001
        client, _ = fake_gh(
            {
                "check-runs": {
                    "check_runs": [
                        {"name": "ok", "status": "completed", "conclusion": "success"},
                        {"name": "skip", "status": "completed", "conclusion": "skipped"},
                        {"name": "bad", "status": "completed", "conclusion": "failure"},
                        {"name": "run", "status": "in_progress", "conclusion": None},
                    ]
                },
                "/status": {
                    "statuses": [
                        {"context": "s-ok", "state": "success"},
                        {"context": "s-bad", "state": "error"},
                        {"context": "s-wait", "state": "pending"},
                    ]
                },
                "pulls/7": PR_JSON,
            }
        )
        pr = await client.get_pull_request(7)
        assert pr.checks_passed == 3
        assert pr.checks_failed == ("bad", "s-bad")
        assert pr.checks_pending == ("run", "s-wait")
        assert pr.head_sha == SHA and pr.base == "master"

    async def test_only_the_named_check_is_ignored(self, fake_gh) -> None:  # noqa: ANN001
        client, _ = fake_gh(
            {
                "check-runs": {
                    "check_runs": [
                        {"name": "smoke", "status": "completed", "conclusion": "failure"},
                        {"name": "unit", "status": "completed", "conclusion": "failure"},
                    ]
                },
                "/status": {"statuses": []},
                "pulls/7": PR_JSON,
            }
        )
        client._config = replace(client._config, ignored_checks=frozenset({"smoke"}))
        pr = await client.get_pull_request(7)
        assert pr.checks_ignored == ("smoke",)
        assert pr.checks_failed == ("unit",)
        assert any("unit" in text for text in find_blockers(pr, BASES))


def test_button_label_is_discord_sized() -> None:
    button = MergeButton(10**9, SHA)
    assert isinstance(button.item, discord.ui.Button)
    assert len(button.item.label or "") <= 80
    assert len(button.item.custom_id or "") <= 100
