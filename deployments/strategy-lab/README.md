# Strategy Lab CCDB service

Dedicated service `ccdb-lab`, Discord channel `nk225-strategy-lab`
(`1547533034583101511`), bot `225strategy_lab` (`1547532074351595530`), runtime
`/home/bomber/ccdb-lab`, workspace `/home/bomber/nk225future/strategy_lab`,
local API `127.0.0.1:8088`. Uses verified release `cf8ffa437ebc4c827f9c6c1297d071ffde8beb86`.
The runtime has its own bot token and session database. This deployment does not
implement workspace filesystem isolation or research access restrictions.

## Discord prerequisites

1. Create an application named `225strategy_lab` in the
   [Developer Portal](https://discord.com/developers/applications).
2. Enable **Bot → Message Content Intent**.
3. Invite the new bot to server `1495053272375890001`, with OAuth2 scopes
   `bot` and `applications.commands`. Grant View Channels, Send Messages,
   Send Messages in Threads, Create Public Threads, Manage Threads,
   Manage Messages, Read Message History, Add Reactions, Embed Links, and Attach Files.
4. Create text channel `nk225-strategy-lab` and copy its channel ID. The existing operations
   bot cannot create channels (Discord returned HTTP 403 / Missing Permissions).
5. Save the raw new bot token locally without echoing it or putting it in chat:

   ```bash
   install -m 600 /dev/null /home/bomber/.ccdb-lab-token
   read -rsp 'New bot token: ' lab_bot_token
   printf '%s\n' "$lab_bot_token" > /home/bomber/.ccdb-lab-token
   unset lab_bot_token
   ```

## Install

Run as `bomber`:

```bash
bash /home/bomber/ccdb-lab/activate.sh
```

The prepared runtime includes this bundle's installer files. To choose Claude Code
/ Opus before preparing configuration, invoke `install.sh` with `--token-file`,
`--channel-id`, and `--backend claude` instead. Codex uses its existing CLI model
default. Permission mode follows the existing CCDB services. The installer validates
bot identity, Message Content Intent, channel access, and API port availability,
then asks for sudo authentication in the terminal. An identical private runtime
configuration may be reused; different configuration is never overwritten.
It only enables the new service; no existing service is restarted.

Confirm `systemctl is-active ccdb-lab.service`, API health, and Discord login in
`journalctl -u ccdb-lab.service`. Finally send a simple greeting in `nk225-strategy-lab`
to verify a complete response. API health alone does not prove Discord or the
agent backend is responding. After success, remove the temporary token file;
the runtime `.env` is mode 600.

## Verified deployment (2026-09-10)

Bot token, Message Content Intent, guild membership and access to the user-specified
channel verified. Runtime configuration uses Codex and the existing CLI default
model. Service is enabled and active; API health is `ok`. Discord login and the
configured channel were confirmed at 18:10:14 JST, with 26 slash commands synced.

The first startup failed because the new runtime lacked its `data` directory.
Creating `/home/bomber/ccdb-lab/data` with mode 700 allowed the systemd retry to
succeed. `prepare.py` now creates this directory for both new and reused runtime
configuration.

## Windows / WSL automatic startup

Lab follows the existing services' startup chain:

1. Windows task `Strix3-BootRecovery` is enabled, with an interactive logon trigger
   and a one-minute delay for the existing Windows account `bombe`.
2. Its existing `C:\strix3\nikkei225-trade-analysis\scripts\ops\daily-startup.ps1`
   invokes WSL. The default distribution is `Ubuntu-24.04`.
3. `/etc/wsl.conf` enables systemd. `ccdb-lab.service` is enabled under
   `multi-user.target`, just like `ccdb-factory` and `ccdb-operation`.

This is startup after Windows login, not unattended startup before login. No
Windows task changes were needed. Configuration and live service health were
checked without rebooting Windows or WSL. A full bot/agent conversational response
has not been tested by this deployment session.
