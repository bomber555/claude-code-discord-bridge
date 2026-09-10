# Strategy Lab CCDB service

Dedicated service `ccdb-lab`, Discord channel `nk225-lab`, runtime
`/home/bomber/ccdb-lab`, workspace `/home/bomber/nk225future/strategy_lab`,
local API `127.0.0.1:8088`. Uses verified release `cf8ffa437ebc4c827f9c6c1297d071ffde8beb86`.
The runtime has its own bot token and session database. This deployment does not
implement workspace filesystem isolation or research access restrictions.

## Discord prerequisites

1. Create an application named `nk225st_lab` in the
   [Developer Portal](https://discord.com/developers/applications).
2. Enable **Bot → Message Content Intent**.
3. Invite the new bot to server `1495053272375890001`, with OAuth2 scopes
   `bot` and `applications.commands`. Grant View Channels, Send Messages,
   Send Messages in Threads, Create Public Threads, Manage Threads,
   Manage Messages, Read Message History, Add Reactions, Embed Links, and Attach Files.
4. Create text channel `nk225-lab` and copy its channel ID. The existing operations
   bot cannot create channels (Discord returned HTTP 403 / Missing Permissions).
5. Save the raw new bot token locally without echoing it or putting it in chat:

   ```bash
   install -m 600 /dev/null /home/bomber/.ccdb-lab-token
   read -rsp 'New bot token: ' lab_bot_token
   printf '%s\n' "$lab_bot_token" > /home/bomber/.ccdb-lab-token
   unset lab_bot_token
   ```

## Install

Run as `bomber`, from this bundle (substitute the new channel ID):

```bash
bash /home/bomber/systematic_trading/operations/wt-1547531225571393637/deployments/strategy-lab/install.sh \
  --token-file /home/bomber/.ccdb-lab-token --channel-id CHANNEL_ID --backend codex
```

Use `--backend claude` for Claude Code / Opus. Codex uses its existing CLI model
default. Permission mode follows the existing CCDB services. The installer validates
bot identity, Message Content Intent, channel access, and API port availability,
then asks for sudo authentication in the terminal. Existing configuration is never
overwritten. It only enables the new service; no existing service is restarted.

Confirm `systemctl is-active ccdb-lab.service`, API health, and Discord login in
`journalctl -u ccdb-lab.service`. Finally send a simple greeting in `nk225-lab`
to verify a complete response. API health alone does not prove Discord or the
agent backend is responding. After success, remove the temporary token file;
the runtime `.env` is mode 600.

## Preparation status (2026-09-10)

Configuration prepared. Deployment and live checks await a new bot token, channel
ID, and terminal sudo authentication. No new service has been started.
