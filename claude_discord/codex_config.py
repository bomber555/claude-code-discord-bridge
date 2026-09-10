"""Resolve Codex model settings through read-only app-server metadata calls.

Never creates a thread, starts a turn, or sends a prompt to a model. The CLI
resolves its own configuration layers; ccdb does not duplicate TOML precedence.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
from dataclasses import dataclass

from claude_code_core.child_env import STRIPPED_ENV_KEYS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CodexModelSelection:
    model: str
    source: str


def _model_name(value: object) -> str | None:
    if isinstance(value, str) and value.strip() and len(value) <= 120:
        return value.strip()
    return None


async def read_codex_model(
    command: str = "codex", *, cwd: str | None = None, timeout: float = 15.0
) -> CodexModelSelection | None:
    """Read effective config, falling back to the CLI-declared default model.

    Missing or broken configuration remains unknown: only a successful read
    with an unset model permits falling back to ``model/list``'s ``isDefault``.
    """
    proc: asyncio.subprocess.Process | None = None

    async def exchange() -> CodexModelSelection | None:
        nonlocal proc
        proc = await asyncio.create_subprocess_exec(
            *(shlex.split(command) or ["codex"]),
            "app-server",
            cwd=cwd,
            env={k: v for k, v in os.environ.items() if k not in STRIPPED_ENV_KEYS},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=4 * 1024 * 1024,
        )
        if proc.stdin is None or proc.stdout is None:
            raise EOFError("Codex metadata pipes unavailable")
        request_id = 0

        async def rpc(method: str, params: dict) -> dict:
            nonlocal request_id
            if proc is None or proc.stdin is None or proc.stdout is None:
                raise EOFError("Codex metadata pipes unavailable")
            request_id += 1
            proc.stdin.write(
                (json.dumps({"id": request_id, "method": method, "params": params}) + "\n").encode()
            )
            await proc.stdin.drain()
            while line := await proc.stdout.readline():
                try:
                    response = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(response, dict) or response.get("id") != request_id:
                    continue
                result = response.get("result")
                if not isinstance(result, dict):
                    raise ValueError("Codex metadata response unavailable")
                return result
            raise EOFError("Codex metadata stream ended")

        await rpc("initialize", {"clientInfo": {"name": "ccdb-model-config", "version": "1.0.0"}})
        proc.stdin.write(b'{"method":"initialized"}\n')
        await proc.stdin.drain()
        result = await rpc("config/read", {"includeLayers": False, "cwd": cwd})
        config = result.get("config")
        if not isinstance(config, dict):
            return None
        model = _model_name(config.get("model"))
        if model:
            return CodexModelSelection(model, "Codex CLI設定")
        if config.get("model") is not None:
            return None  # malformed values are not an unset model

        cursor: str | None = None
        for _ in range(10):
            result = await rpc(
                "model/list", {"limit": 100, "includeHidden": False, "cursor": cursor}
            )
            entries = result.get("data")
            if not isinstance(entries, list):
                return None
            defaults = [
                name
                for entry in entries
                if isinstance(entry, dict) and entry.get("isDefault") is True
                if (name := _model_name(entry.get("model"))) is not None
            ]
            if defaults:
                return (
                    CodexModelSelection(defaults[0], "Codex CLI既定値")
                    if len(defaults) == 1
                    else None
                )
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                return None
            cursor = next_cursor
        return None

    try:
        return await asyncio.wait_for(exchange(), timeout=timeout)
    except (OSError, ValueError, EOFError, TimeoutError):
        # Do not log the configuration response: it can contain credentials.
        logger.debug("Codex model configuration could not be resolved")
        return None
    finally:
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
