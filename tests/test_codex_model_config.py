"""Read-only model resolution must never start an inference turn."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from claude_discord.codex_config import read_codex_model


def fake_server(monkeypatch, responses):
    proc = MagicMock()
    proc.stdin.drain = AsyncMock()
    lines = [
        json.dumps({"id": i, "result": r}).encode() + b"\n" for i, r in enumerate(responses, 1)
    ]
    proc.stdout.readline = AsyncMock(side_effect=[*lines, b""])
    proc.wait = AsyncMock(return_value=0)
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    return proc, spawn


async def test_reads_effective_project_config_without_model_inference(monkeypatch):
    proc, spawn = fake_server(monkeypatch, [{}, {"config": {"model": "project-model"}}])
    result = await read_codex_model("codex", cwd="/tmp/project")
    assert result.model == "project-model"
    assert result.source == "Codex CLI設定"
    requests = [json.loads(c.args[0]) for c in proc.stdin.write.call_args_list]
    assert [x["method"] for x in requests] == ["initialize", "initialized", "config/read"]
    assert requests[-1]["params"]["cwd"] == "/tmp/project"
    proc.terminate.assert_called_once()


async def test_unset_config_uses_cli_declared_default_not_catalog_order(monkeypatch):
    proc, _ = fake_server(
        monkeypatch,
        [
            {},
            {"config": {"model": None}},
            {
                "data": [
                    {"model": "first-but-not-default", "isDefault": False},
                    {"model": "gpt-6-astra", "isDefault": True},
                ],
                "nextCursor": None,
            },
        ],
    )
    result = await read_codex_model("codex")
    assert result.model == "gpt-6-astra"
    assert result.source == "Codex CLI既定値"
    methods = [json.loads(c.args[0])["method"] for c in proc.stdin.write.call_args_list]
    assert set(methods) <= {"initialize", "initialized", "config/read", "model/list"}


async def test_no_declared_default_stays_unknown(monkeypatch):
    fake_server(monkeypatch, [{}, {"config": {}}, {"data": [{"model": "guess"}]}])
    assert await read_codex_model("codex") is None


async def test_config_failure_does_not_guess_from_recommended_models(monkeypatch):
    fake_server(monkeypatch, [{}, None])
    assert await read_codex_model("codex") is None


async def test_missing_cli_is_nonfatal(monkeypatch):
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError))
    assert await read_codex_model("not-installed") is None


async def test_timeout_terminates_and_reaps_server(monkeypatch):
    import asyncio

    proc, _ = fake_server(monkeypatch, [])

    async def never_respond():
        await asyncio.sleep(60)

    proc.stdout.readline = never_respond
    assert await read_codex_model("codex", timeout=0.01) is None
    proc.terminate.assert_called_once()
    proc.wait.assert_awaited()


async def test_metadata_process_strips_bot_secrets_and_keeps_codex_home(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-secret")
    monkeypatch.setenv("CODEX_HOME", "/tmp/isolated-codex")
    _, spawn = fake_server(monkeypatch, [{}, {"config": {"model": "configured"}}])
    await read_codex_model("codex")
    env = spawn.await_args.kwargs["env"]
    assert "DISCORD_BOT_TOKEN" not in env
    assert env["CODEX_HOME"] == "/tmp/isolated-codex"


async def test_default_can_be_on_a_later_metadata_page(monkeypatch):
    proc, _ = fake_server(
        monkeypatch,
        [
            {},
            {"config": {}},
            {"data": [], "nextCursor": "next-page"},
            {"data": [{"model": "default-model", "isDefault": True}], "nextCursor": None},
        ],
    )
    result = await read_codex_model("codex")
    assert result.model == "default-model"
    assert (
        json.loads(proc.stdin.write.call_args_list[-1].args[0])["params"]["cursor"] == "next-page"
    )


async def test_missing_subprocess_pipe_is_nonfatal(monkeypatch):
    proc, _ = fake_server(monkeypatch, [])
    proc.stdin = None
    assert await read_codex_model("codex") is None
