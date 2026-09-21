"""Codex app-server transport used for same-turn steering."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from claude_code_core.backend import create_backend
from claude_code_core.codex_app_server import CodexAppServerRunner, parse_app_server_message
from claude_code_core.codex_runner import CodexRunner
from claude_code_core.types import MessageType


class _AppServerStdout:
    def __init__(self) -> None:
        self.lines: asyncio.Queue[bytes] = asyncio.Queue()

    async def readline(self) -> bytes:
        return await self.lines.get()


class _AppServerStdin:
    def __init__(
        self,
        stdout: _AppServerStdout,
        *,
        complete_on_steer: bool = False,
        resume_missing: bool = False,
    ) -> None:
        self.stdout = stdout
        self.complete_on_steer = complete_on_steer
        self.resume_missing = resume_missing
        self.messages: list[dict] = []

    def _feed(self, message: dict) -> None:
        self.stdout.lines.put_nowait((json.dumps(message) + "\n").encode())

    def _finish_turn(self) -> None:
        self._feed(
            {
                "method": "item/completed",
                "params": {"item": {"id": "item-1", "type": "agentMessage", "text": "done"}},
            }
        )
        self._feed(
            {
                "method": "turn/completed",
                "params": {"threadId": "abc-123", "turn": {"id": "turn-1"}},
            }
        )

    def write(self, data: bytes) -> None:
        message = json.loads(data)
        self.messages.append(message)
        request_id = message.get("id")
        method = message.get("method")
        if request_id is None:
            return
        if method == "initialize":
            result = {"userAgent": "fake"}
        elif method == "thread/start":
            result = {"thread": {"id": "abc-123"}}
        elif method == "thread/resume" and self.resume_missing:
            self._feed(
                {
                    "id": request_id,
                    "error": {
                        "code": -32600,
                        "message": "no rollout found for thread id dead-beef",
                    },
                }
            )
            return
        elif method == "turn/start":
            result = {"turn": {"id": "turn-1"}}
        elif method == "turn/steer":
            result = {"turnId": "turn-1"}
        else:
            result = {}
        self._feed({"id": request_id, "result": result})
        if method == "turn/start":
            self._feed(
                {
                    "method": "turn/started",
                    "params": {"threadId": "abc-123", "turn": {"id": "turn-1"}},
                }
            )
            if not self.complete_on_steer:
                self._finish_turn()
        elif method == "turn/steer" and self.complete_on_steer:
            self._finish_turn()

    async def drain(self) -> None:
        return None


class _AppServerProcess:
    def __init__(
        self,
        *,
        complete_on_steer: bool = False,
        resume_missing: bool = False,
    ) -> None:
        self.stdout = _AppServerStdout()
        self.stdin = _AppServerStdin(
            self.stdout,
            complete_on_steer=complete_on_steer,
            resume_missing=resume_missing,
        )
        self.stderr = AsyncMock()
        self.returncode: int | None = None
        self.pid = 123

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode


def test_codex_backend_uses_app_server_runner() -> None:
    runner = create_backend(backend="codex", model="gpt-5.6-sol")

    assert isinstance(runner, CodexAppServerRunner)
    assert isinstance(runner, CodexRunner)


def test_app_server_args_do_not_contain_user_input() -> None:
    runner = CodexAppServerRunner(command="codex", model="gpt-5.6-sol")

    args = runner._build_app_server_args()

    assert args == ["codex", "app-server"]


@pytest.mark.asyncio
async def test_steer_targets_the_active_turn() -> None:
    runner = CodexAppServerRunner(command="codex")
    runner._app_thread_id = "thread-1"
    runner._active_turn_id = "turn-1"
    runner._request = AsyncMock(return_value={"turnId": "turn-1"})  # type: ignore[method-assign]

    accepted = await runner.steer("追加指示")

    assert accepted is True
    runner._request.assert_awaited_once_with(
        "turn/steer",
        {
            "threadId": "thread-1",
            "expectedTurnId": "turn-1",
            "input": [{"type": "text", "text": "追加指示"}],
        },
    )


@pytest.mark.asyncio
async def test_steer_returns_false_when_no_turn_is_active() -> None:
    runner = CodexAppServerRunner(command="codex")

    assert await runner.steer("late") is False


def test_completed_agent_message_maps_to_stream_event() -> None:
    event = parse_app_server_message(
        {
            "method": "item/completed",
            "params": {"item": {"id": "item-1", "type": "agentMessage", "text": "done"}},
        }
    )

    assert event is not None
    assert event.message_type is MessageType.ASSISTANT
    assert event.text == "done"


@pytest.mark.asyncio
async def test_run_uses_app_server_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _AppServerProcess()
    spawned: list[tuple[object, ...]] = []

    async def fake_spawn(*args: object, **kwargs: object) -> _AppServerProcess:
        spawned.append(args)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    runner = CodexAppServerRunner(command="codex", working_dir="/tmp")

    events = [event async for event in runner.run("hello")]

    assert spawned == [("codex", "app-server")]
    methods = [message.get("method") for message in process.stdin.messages]
    assert methods[:4] == ["initialize", "initialized", "thread/start", "turn/start"]
    assert events[0].session_id == "abc-123"
    assert any(event.text == "done" for event in events)
    assert events[-1].is_complete is True


@pytest.mark.asyncio
async def test_live_run_accepts_turn_steer(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _AppServerProcess(complete_on_steer=True)

    async def fake_spawn(*args: object, **kwargs: object) -> _AppServerProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    runner = CodexAppServerRunner(command="codex", working_dir="/tmp")
    run_task = asyncio.create_task(_collect(runner.run("initial")))
    for _ in range(100):
        if runner._active_turn_id is not None:
            break
        await asyncio.sleep(0)

    assert await runner.steer("追加") is True
    events = await run_task

    steer = next(
        message for message in process.stdin.messages if message.get("method") == "turn/steer"
    )
    assert steer["params"]["expectedTurnId"] == "turn-1"
    assert steer["params"]["input"] == [{"type": "text", "text": "追加"}]
    assert events[-1].is_complete is True


@pytest.mark.asyncio
async def test_missing_resume_rollout_starts_replacement_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _AppServerProcess(resume_missing=True)

    async def fake_spawn(*args: object, **kwargs: object) -> _AppServerProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    runner = CodexAppServerRunner(command="codex", working_dir="/tmp")

    events = [event async for event in runner.run("continue", session_id="dead-beef")]

    methods = [message.get("method") for message in process.stdin.messages]
    assert "thread/resume" in methods
    assert "thread/start" in methods
    assert events[0].session_id == "abc-123"
    assert events[-1].is_complete is True


async def _collect(stream) -> list:
    return [event async for event in stream]
