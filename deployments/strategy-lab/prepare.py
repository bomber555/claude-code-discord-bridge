"""Validate a dedicated Discord bot/channel and prepare its private runtime."""

import argparse
import json
import os
from pathlib import Path
import socket
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--channel-id", type=int, required=True)
    parser.add_argument("--backend", choices=("codex", "claude"), default="codex")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    runtime = Path("/home/bomber/ccdb-lab")
    workspace = Path("/home/bomber/nk225future/strategy_lab")
    if not workspace.is_dir():
        raise SystemExit("Workspace is missing")
    if (runtime / ".env").exists():
        raise SystemExit("Runtime already configured; refusing to overwrite")
    if args.token_file.stat().st_mode & 0o077:
        raise SystemExit("Token file must be private: chmod 600 TOKEN_FILE")
    token = args.token_file.read_text().strip()
    if not token or any(c.isspace() for c in token):
        raise SystemExit("Token file must contain only the raw bot token")

    def get(path):
        request = urllib.request.Request(
            "https://discord.com/api/v10" + path,
            headers={"Authorization": "Bot " + token, "User-Agent": "CCDB Lab setup"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise SystemExit(f"Discord validation failed: HTTP {error.code}") from None

    bot = get("/users/@me")
    app = get("/oauth2/applications/@me")
    channel = get(f"/channels/{args.channel_id}")
    if not bot.get("bot"):
        raise SystemExit("A bot token is required")
    if channel.get("guild_id") != "1495053272375890001" or channel.get("type") != 0:
        raise SystemExit("Expected a text channel in the existing CCDB server")
    if channel.get("name") != "nk225-lab":
        raise SystemExit("Expected the dedicated nk225-lab channel")
    if not app.get("flags", 0) & ((1 << 18) | (1 << 19)):
        raise SystemExit("Enable Message Content Intent in the Developer Portal")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 8088))
    print(f"Validated bot {bot['username']} ({bot['id']}), channel {channel['id']}")
    if args.check_only:
        return
    os.umask(0o077)
    runtime.mkdir(mode=0o700, exist_ok=True)
    values = {
        "DISCORD_BOT_TOKEN": token,
        "DISCORD_CHANNEL_ID": str(args.channel_id),
        "DISCORD_OWNER_ID": "951322660170776666",
        "CCDB_BACKEND": args.backend,
        "CCDB_CLAUDE_COMMAND": "/home/bomber/.local/bin/claude",
        "CCDB_CODEX_COMMAND": "/home/bomber/.local/bin/codex",
        "CCDB_WORKING_DIR": str(workspace),
        "CCDB_PERMISSION_MODE": "acceptEdits",
        "CCDB_DANGEROUSLY_SKIP_PERMISSIONS": "true",
        "CCDB_MONITOR_ALL_CHANNELS": "false",
        "MAX_CONCURRENT_SESSIONS": "3",
        "SESSION_TIMEOUT_SECONDS": "300",
        "API_HOST": "127.0.0.1",
        "API_PORT": "8088",
    }
    if args.backend == "claude":
        values["CCDB_MODEL"] = "opus"
    with (runtime / ".env").open("x") as output:
        output.write("".join(f"{key}={value}\n" for key, value in values.items()))
    print(f"Prepared {runtime}/.env (private); backend={args.backend}")


if __name__ == "__main__":
    main()
