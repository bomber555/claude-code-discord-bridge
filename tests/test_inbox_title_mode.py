"""Tests for title-based inbox classification (THREAD_INBOX_MODE=title)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_discord.cogs import event_processor as ep


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "classify"), ("classify", "classify"), ("title", "title"), ("TITLE", "title")],
)
def test_inbox_mode(monkeypatch: pytest.MonkeyPatch, value: str | None, expected: str) -> None:
    if value is None:
        monkeypatch.delenv("THREAD_INBOX_MODE", raising=False)
    else:
        monkeypatch.setenv("THREAD_INBOX_MODE", value)
    assert ep._inbox_mode() == expected


@pytest.mark.parametrize(
    ("title", "markers", "expected"),
    [
        ("#18 PR#19 マージ待ち | 通知を@silentに", None, "waiting"),
        ("#808 PO判断待ち 弾4 | 資料読み取り", None, "waiting"),
        ("#808 調査中 弾4 | 資料読み取り", None, "done"),
        ("", None, "done"),
        (None, None, "done"),
        ("#1 needs-review | x", "needs-review, 待ち", "waiting"),
        ("#1 マージ待ち | x", "needs-review", "done"),
    ],
)
def test_classify_by_title(
    monkeypatch: pytest.MonkeyPatch, title: str | None, markers: str | None, expected: str
) -> None:
    if markers is None:
        monkeypatch.delenv("THREAD_INBOX_TITLE_MARKERS", raising=False)
    else:
        monkeypatch.setenv("THREAD_INBOX_TITLE_MARKERS", markers)
    assert ep._classify_by_title(title) == expected


def _repo() -> MagicMock:
    from claude_discord.database.inbox_repo import ThreadInboxRepository

    repo = MagicMock(spec=ThreadInboxRepository)
    repo.upsert = AsyncMock()
    repo.remove = AsyncMock()
    return repo


async def test_waiting_title_upserts_without_calling_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("THREAD_INBOX_TITLE_MARKERS", raising=False)
    classify = AsyncMock()
    monkeypatch.setattr("claude_discord.discord_ui.inbox_classifier.classify", classify)
    repo = _repo()

    await ep._title_update_inbox(
        thread_id=1,
        title="#18 実装承認待ち | x",
        last_message_url="https://discord.com/x",
        inbox_repo=repo,
        dashboard=None,
    )

    repo.upsert.assert_awaited_once_with(
        thread_id=1, status="waiting", confidence="high", last_message_url="https://discord.com/x"
    )
    repo.remove.assert_not_awaited()
    classify.assert_not_awaited()


async def test_non_waiting_title_removes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("THREAD_INBOX_TITLE_MARKERS", raising=False)
    repo = _repo()

    await ep._title_update_inbox(
        thread_id=2, title="#18 調査中 | x", last_message_url=None, inbox_repo=repo, dashboard=None
    )

    repo.remove.assert_awaited_once_with(2)
    repo.upsert.assert_not_awaited()
