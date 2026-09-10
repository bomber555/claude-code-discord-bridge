# Fork status footer

This fork tracks `ebibibi/ebi-agent-chat-relay` through `7fc303c` (package 4.0.26)
and carries the Claude/Codex account and quota patches previously ending at `8851639`.
The existing per-turn footer now also shows the selected model on resumed turns.
An unset model is labelled `CLI default`; the bridge does not infer the resolved model.

## Enable

- Codex quota: `/engine-status mode:on` (or `auto`, the default). Thread settings
  override the global setting; `off` suppresses the Codex footer.
- Codex account: set `CCDB_CODEX_STATUS_ACCOUNT=1` in the bot environment and restart.
  Account display is opt-in because the footer can include the account email.
- Claude usage: enabled by default; `CCDB_USAGE_FOOTER=0` disables usage lines.
  The account label comes from the CLI's local login information.
- Model: automatically included whenever the active backend footer is posted.
  `/model` selects the model; Codex suggestions follow the installed CLI catalog.

Codex quota rows explicitly show **remaining** percent, derived as 100 minus
`usedPercent`. Claude rows explicitly show **used** percent from `utilization`.
These are subscription quota windows, not an exact number of tokens left.
Only the active backend's account and quota are shown. CLI/network failures do
not prevent the assistant's answer from being delivered.

## Verification

Local Python 3.12: 2,907 tests passed; branch coverage for `claude_discord` 81.98%.
Lint, format, type checks and public imports passed. The security scan adds no
findings relative to upstream. Model footer and fragmented HTTP body regressions
were reproduced before their fixes.

Live read-only checks fetched the Codex account, remaining quota and CLI model
catalog. A minimal Claude CLI request refreshed its expired login token, after
which account quota retrieval also succeeded. No Discord service was restarted
as part of these checks. Mock Discord sends test the rendering path; an actual
post-upgrade Discord reply remains a deployment check.

The fork uses GitHub-hosted CI runners because it has no self-hosted runner.
The full suite also exposed an upstream Teams receiver short-read bug: HTTP
body fragments are now accumulated through EOF within the existing size limit.
