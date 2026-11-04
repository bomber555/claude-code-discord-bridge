# bomber555 fork の独自機能

upstream (`ebibibi/ebi-agent-chat-relay`) に無く、この fork だけが持つ機能。upstream を取り込むときはここに挙げたものを残す。

| 機能 | 使い方 / 設定 |
|---|---|
| Public スレッド一覧 | `GET /api/channels/{channel_id}/threads` (下記) |
| スレッド名の変更 | `POST /api/threads/{id}/title` (人が改名したら 409、`override: true` で解除) |
| ボタンで PR をマージ | `/merge <pr>` / `POST /api/threads/{id}/merge-request`。`CCDB_MERGE_REPO` `CCDB_MERGE_USER_IDS` `CCDB_MERGE_BASES` `CCDB_MERGE_IGNORE_CHECKS` `CCDB_MERGE_GH_BIN` |
| hook の ask を Discord で承認 | `--permission-prompt-tool stdio` の `can_use_tool` を Allow/Deny ボタンへ中継。`CCDB_APPROVER_USER_IDS` `CCDB_PERMISSION_TIMEOUT_SECONDS`。確認は `scripts/check_permission_relay.py` |
| 通知を絞る | `CCDB_SILENT_BY_DEFAULT` (既定 `true`。完了・エラー・@mention だけ通知) |
| モデル呼び出し無しの受信箱 | `THREAD_INBOX_MODE=title` `THREAD_INBOX_TITLE_MARKERS` |
| Session Status の時刻 | `CCDB_DISPLAY_TZ` |
| 使用量の表示 | Claude / Codex の残量を日本語の行で表示。`CCDB_CODEX_STATUS_ACCOUNT=1` でアカウント名も表示 (`CCDB_STATUS_LANG` は Codex の取得失敗行にだけ効く) |
| 他の Bot 宛を無視 | 他の Bot だけをメンションした投稿には反応しない |
| リポジトリ判定付きの worktree 指示 | Git の変更だけ worktree を要求する |

## Public スレッド一覧 API

`GET /api/channels/{channel_id}/threads` は localhost 専用の読み取り API。

| パラメータ | 値 | 既定 |
|---|---|---|
| `state` | `active` / `archived` / `all` | `all` |
| `limit` | 1〜100 | 50 |
| `cursor` | 前のページの `next_cursor` | なし |

```json
{
  "guild_id": "111",
  "channel_id": "222",
  "threads": [
    {
      "id": "333",
      "name": "スレッド名",
      "parent_id": "222",
      "archived": false,
      "url": "https://discord.com/channels/111/333"
    }
  ],
  "next_cursor": "archived:2026-11-01T00:00:00+00:00"
}
```

- 順序はアクティブ (新しい順) → アーカイブ済み (アーカイブが新しい順)。
- `next_cursor` が `null` なら最後のページ。`null` でなければ次のページが空のこともある。
- ページの間にアーカイブされたスレッドは 2 回返ることがあるので、呼び出し側で `id` で重複を除く。
- ID は文字列 (Discord の ID は JSON の数値では桁が落ちるため)。
- 本文は `GET /api/threads/{id}/messages` で読む。

| ステータス | 意味 |
|---|---|
| 400 | パラメータ不正、またはスレッドを持てないチャンネル |
| 403 | 設定中のサーバー外のチャンネル、または Bot に閲覧・履歴閲覧権限がない |
| 404 | チャンネルが見つからない |
| 502 | Discord API のエラー |
