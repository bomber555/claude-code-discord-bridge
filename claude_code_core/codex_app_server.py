"""Codex app-server transport with native same-turn steering.

``codex exec`` remains available in :mod:`codex_runner` for compatibility and
for the local backend. The regular cloud Codex backend uses this subclass so
an active Discord turn can call the official ``turn/steer`` JSON-RPC method.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
from collections.abc import AsyncGenerator
from typing import Any

from .codex_runner import (
    CodexRunner,
    _is_missing_rollout_error,
    _resolve_codex_sandbox_override,
)
from .types import ImageData, MessageType, StreamEvent, ToolCategory, ToolUseEvent

logger = logging.getLogger(__name__)

_SESSION_ID = re.compile(r"^[a-f0-9-]+$")
_INHERIT = object()


def _item_from_message(message: dict[str, Any]) -> dict[str, Any]:
    params = message.get("params")
    if not isinstance(params, dict):
        return {}
    item = params.get("item")
    return item if isinstance(item, dict) else {}


def parse_app_server_message(message: dict[str, Any]) -> StreamEvent | None:
    """Translate one app-server notification to ccdb's backend-neutral event."""
    method = message.get("method")
    params = message.get("params")
    params = params if isinstance(params, dict) else {}

    if method == "turn/started":
        return StreamEvent(raw=message, message_type=MessageType.SYSTEM)

    if method == "turn/completed":
        turn = params.get("turn")
        turn = turn if isinstance(turn, dict) else {}
        error_data = turn.get("error")
        error: str | None = None
        if isinstance(error_data, dict):
            value = error_data.get("message")
            error = value if isinstance(value, str) else None
        usage = turn.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        return StreamEvent(
            raw=message,
            message_type=MessageType.RESULT if error else MessageType.SYSTEM,
            is_complete=True,
            error=error,
            input_tokens=usage.get("inputTokens"),
            output_tokens=usage.get("outputTokens"),
            cache_read_tokens=usage.get("cachedInputTokens"),
        )

    if method not in {"item/started", "item/completed"}:
        return None

    item = _item_from_message(message)
    item_type = item.get("type")
    raw_item_id = item.get("id")
    item_id = raw_item_id if isinstance(raw_item_id, str) else ""

    if method == "item/started" and item_type == "commandExecution":
        command = item.get("command")
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        return StreamEvent(
            raw=message,
            message_type=MessageType.ASSISTANT,
            tool_use=ToolUseEvent(
                tool_id=item_id,
                tool_name="Bash",
                tool_input={"command": command if isinstance(command, str) else ""},
                category=ToolCategory.COMMAND,
            ),
        )

    if method == "item/completed" and item_type == "agentMessage":
        text = item.get("text")
        return StreamEvent(
            raw=message,
            message_type=MessageType.ASSISTANT,
            text=text if isinstance(text, str) else "",
        )

    if method == "item/completed" and item_type == "commandExecution":
        output = item.get("aggregatedOutput", item.get("output", ""))
        return StreamEvent(
            raw=message,
            message_type=MessageType.USER,
            tool_result_id=item_id,
            tool_result_content=output if isinstance(output, str) else "",
        )

    if method == "item/completed" and item_type == "fileChange":
        return StreamEvent(
            raw=message,
            message_type=MessageType.ASSISTANT,
            tool_use=ToolUseEvent(
                tool_id=item_id,
                tool_name="Edit",
                tool_input={"description": str(item.get("status", ""))},
                category=ToolCategory.EDIT,
            ),
        )
    return None


class CodexAppServerRunner(CodexRunner):
    """Run Codex through its official stdio JSON-RPC app-server."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._app_thread_id: str | None = None
        self._active_turn_id: str | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_request_id = 1
        self._write_lock = asyncio.Lock()

    def clone(
        self,
        model: str | None = None,
        working_dir: str | None | object = _INHERIT,
        thread_id: int | None = None,
        effort: str | None | object = _INHERIT,
        append_system_prompt: str | None = None,
        **_kwargs: object,
    ) -> CodexAppServerRunner:
        """Clone configuration without sharing app-server protocol state."""
        return CodexAppServerRunner(
            command=self.command,
            model=model if model is not None else self.model,
            permission_mode=self.permission_mode,
            working_dir=(self.working_dir if working_dir is _INHERIT else working_dir),
            timeout_seconds=self.timeout_seconds,
            dangerously_skip_permissions=self.dangerously_skip_permissions,
            allowed_tools=self.allowed_tools,
            api_port=self.api_port,
            api_secret=self.api_secret,
            thread_id=thread_id if thread_id is not None else self.thread_id,
            append_system_prompt=(
                append_system_prompt
                if append_system_prompt is not None
                else self.append_system_prompt
            ),
            images=self.images,
            effort=self.effort if effort is _INHERIT else effort,
        )

    def _build_app_server_args(self) -> list[str]:
        parts = shlex.split(self.command) if self.command else ["codex"]
        return [*(parts or ["codex"]), "app-server"]

    @staticmethod
    def _user_input(
        prompt: str,
        images: list[ImageData] | None = None,
    ) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        for image in images or []:
            items.append(
                {
                    "type": "image",
                    "url": f"data:{image.media_type};base64,{image.data}",
                }
            )
        if prompt:
            items.append({"type": "text", "text": prompt})
        return items

    async def _write(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise RuntimeError("Codex app-server is not running")
        line = (json.dumps(message, ensure_ascii=False) + "\n").encode()
        async with self._write_lock:
            process.stdin.write(line)
            await process.stdin.drain()

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._write(message)

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_request_id
        self._next_request_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            response = await asyncio.wait_for(future, timeout=self.timeout_seconds or None)
        finally:
            self._pending.pop(request_id, None)
        error = response.get("error")
        if error is not None:
            raise RuntimeError(f"{method} failed: {error}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"{method} returned no result")
        return result

    async def _reader(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(message, dict):
                continue
            response_id = message.get("id")
            if isinstance(response_id, int) and "method" not in message:
                future = self._pending.get(response_id)
                if future is not None and not future.done():
                    future.set_result(message)
                continue
            if isinstance(response_id, int) and isinstance(message.get("method"), str):
                await self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": response_id,
                        "error": {"code": -32601, "message": "Client request unsupported"},
                    }
                )
                continue
            if isinstance(message.get("method"), str):
                await self._notifications.put(message)

        error = RuntimeError("Codex app-server stdout closed")
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)

    def _thread_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "approvalPolicy": "never",
            "cwd": os.path.abspath(self.working_dir or os.getcwd()),
        }
        if self.model:
            params["model"] = self.model
        if self.append_system_prompt:
            params["developerInstructions"] = self.append_system_prompt
        sandbox = (
            "danger-full-access"
            if self.dangerously_skip_permissions
            else _resolve_codex_sandbox_override()
        )
        if sandbox:
            params["sandbox"] = sandbox
        return params

    async def run(
        self,
        prompt: str,
        session_id: str | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Start/resume a Codex thread, then stream one app-server turn."""
        if session_id and not _SESSION_ID.fullmatch(session_id):
            raise ValueError(f"Invalid session_id format: {session_id!r}")
        cwd = self.working_dir or os.getcwd()
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._build_app_server_args(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=self._build_env(),
                limit=10 * 1024 * 1024,
            )
            self._reader_task = asyncio.create_task(self._reader())
            await self._request(
                "initialize",
                {"clientInfo": {"name": "ccdb", "version": "1.0.0"}},
            )
            await self._notify("initialized")

            thread_method = "thread/resume" if session_id else "thread/start"
            thread_params = self._thread_params()
            if session_id:
                thread_params["threadId"] = session_id
            try:
                thread_result = await self._request(thread_method, thread_params)
            except RuntimeError as exc:
                if not session_id or not _is_missing_rollout_error(str(exc)):
                    raise
                logger.warning(
                    "Codex resume history for session %s is missing; starting a new thread",
                    session_id,
                )
                thread_params.pop("threadId", None)
                thread_method = "thread/start"
                thread_result = await self._request(thread_method, thread_params)
            thread = thread_result.get("thread")
            if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
                raise RuntimeError(f"{thread_method} returned no thread id")
            self._app_thread_id = thread["id"]
            yield StreamEvent(
                raw=thread_result,
                message_type=MessageType.SYSTEM,
                session_id=self._app_thread_id,
            )

            turn_params: dict[str, Any] = {
                "threadId": self._app_thread_id,
                "input": self._user_input(prompt, self.images),
            }
            if self.model:
                turn_params["model"] = self.model
            if self.effort:
                turn_params["effort"] = self.effort
            turn_result = await self._request("turn/start", turn_params)
            turn = turn_result.get("turn")
            if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                raise RuntimeError("turn/start returned no turn id")
            self._active_turn_id = turn["id"]

            while True:
                message = await asyncio.wait_for(
                    self._notifications.get(), timeout=self.timeout_seconds or None
                )
                event = parse_app_server_message(message)
                if event is None:
                    continue
                yield event
                if event.tool_use is not None and event.tool_use.tool_name == "Edit":
                    yield StreamEvent(
                        raw=event.raw,
                        message_type=MessageType.USER,
                        tool_result_id=event.tool_use.tool_id,
                        tool_result_content="",
                    )
                if event.is_complete:
                    return
        except TimeoutError:
            yield StreamEvent(
                raw={},
                message_type=MessageType.RESULT,
                is_complete=True,
                error=f"Timed out after {self.timeout_seconds} seconds",
            )
        except Exception as exc:
            logger.warning("Codex app-server run failed", exc_info=True)
            yield StreamEvent(
                raw={},
                message_type=MessageType.RESULT,
                is_complete=True,
                error=str(exc),
            )
        finally:
            self._active_turn_id = None
            self._app_thread_id = None
            await self._cleanup_app_server()

    async def steer(
        self,
        prompt: str,
        images: list[ImageData] | None = None,
    ) -> bool:
        """Append input to the in-flight turn using ``turn/steer``."""
        if self._app_thread_id is None or self._active_turn_id is None:
            return False
        params = {
            "threadId": self._app_thread_id,
            "expectedTurnId": self._active_turn_id,
            "input": self._user_input(prompt, images),
        }
        try:
            await self._request("turn/steer", params)
            return True
        except Exception:
            logger.info("Codex turn ended or rejected steer", exc_info=True)
            return False

    async def interrupt(self) -> None:
        """Interrupt the active turn without killing app-server first."""
        if self._app_thread_id is None or self._active_turn_id is None:
            await self.kill()
            return
        try:
            await self._request(
                "turn/interrupt",
                {"threadId": self._app_thread_id, "turnId": self._active_turn_id},
            )
        except Exception:
            logger.warning("Codex turn/interrupt failed; terminating app-server", exc_info=True)
            await self.kill()

    async def _cleanup_app_server(self) -> None:
        await self.kill()
        task = self._reader_task
        self._reader_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._pending.clear()
        self._notifications = asyncio.Queue()
