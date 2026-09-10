# Fork status footer

This fork tracks `ebibibi/ebi-agent-chat-relay` through `7fc303c` (package 4.0.26)
and carries the Claude/Codex account and quota patches previously ending at `8851639`.
Interactive Discord Codex turns now use one completion card with separate execution
backend, observed model, account and remaining quota. The observed model comes from
the latest `turn_context` in that session's local rollout, including CLI-default
and resumed turns. Missing metadata is shown as `未取得`; selection is never changed.
The metadata lookup reads at most the last 8 MiB, so a very long turn can show
`未取得` even when earlier metadata exists.

Session IDs and per-turn input/output tokens appear behind a `詳細` button.
Command cards also keep shell text and output behind `詳細`. These buttons respond
privately and are available for one hour or until the bot restarts. The running
control is removed at completion; if deletion fails, it changes to `セッション終了`.
Chat-only mode remains text-only, and other frontends keep their existing notices.

## Enable

- Codex quota: `/engine-status mode:on` (or `auto`, the default). Thread settings
  override the global setting; `off` suppresses quota retrieval, while the completion/model card remains.
- Codex account: set `CCDB_CODEX_STATUS_ACCOUNT=1` in the bot environment and restart.
  Account display is opt-in because the footer can include the account email.
- Claude usage: enabled by default; `CCDB_USAGE_FOOTER=0` disables usage lines.
  The account label comes from the CLI's local login information.
- Model: automatically included in the Codex completion card.
  `/model` selects the model; Codex suggestions follow the installed CLI catalog.

Codex quota rows explicitly show **remaining** percent, derived as 100 minus
`usedPercent`. Claude rows explicitly show **used** percent from `utilization`.
These are subscription quota windows, not an exact number of tokens left.
Codex labels use the returned window duration (including weekly data in the primary
slot). Unavailable windows are omitted, and Spark's separate quota is never
substituted for normal Codex. Internal plan codes such as `prolite` are omitted.
Only the active backend's account and quota are shown. CLI/network failures do
not prevent the assistant's answer from being delivered.

## Initial upstream integration verification

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

## Display cleanup verification

Regression coverage checks one completion card, default-model metadata lookup,
account opt-in, weekly-primary limits, details-only token/command display, missing
quota, and removal of stale running controls. The live esmart session's observed
model was read successfully without issuing a model request or changing settings.
Service deployment is separate from repository checks.
