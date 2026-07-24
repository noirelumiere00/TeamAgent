# メール機能 リリース手順（営業向け・per-user OAuth）

営業が Google を認可（連携）すると、Slack から自分の受信箱に対して 4 つのメール機能が使える。
全機能 **per-user OAuth・本人の受信箱のみ・結果は本人だけに ephemeral 配信**（共有チャンネルに
内容を出さない）。

> ⚠️ **連携の起動方法（重要）**: この Slack アプリには **スラッシュコマンドが登録されていない**
> （`/teamagent_connect` は「有効なコマンドではありません」になる）。そのため連携は **Bot に
> 「連携」と話しかける**方式を使う:
> - 「**@TeamAgent 連携**」（チャンネル）または **DM で「連携」**「メール連携」「Google連携」
> - → 本人専用の認可リンクが **本人にだけ** 返る（`detect_skill` の connect 経路 / `_connect_message`）。
> スラッシュコマンドを使いたい場合は別途 api.slack.com でコマンド登録＋再インストールが必要。

| 機能 | Skill | トリガー例 | スコープ |
|---|---|---|---|
| 朝のTodo（要返信トリアージ） | `mail_followup` | 「○○社の要返信メール教えて」 | gmail.modify※(読み) |
| メール要約 | `mail_summary` | 「○○社のメール要約して」 | gmail.modify※(読み) |
| メール×社内ナレッジ横断 | `mail_to_internal_context` | 「○○社のメール、社内で何か話してた?」 | gmail.modify※(読み) |
| 返信ドラフト作成 | `mail_reply` | 「○○社のメールに返信作って」「○○社のメール作成して」 | gmail.modify（下書き作成） |

※ connect は **`gmail.modify` 1 本**を取得（読み＋下書き作成）。送信/削除は GmailClient の
adapter denylist（`users.messages.send`/`users.drafts.send`/delete/trash 等）で**物理封鎖**し、
`drafts.create`（下書き）だけ許可＝「AI は要約・提案・下書きまで、送信は人間」をコードで強制。

> ⚠️ **公開＝Bot が営業の個人受信箱を読む**。プライバシー重大ゲートのため本番反映は管理者承認で実施。
> 返信ドラフトは Gmail の「下書き」に入るだけで送信はしない（本人が確認して送信）。
> OAuth 同意画面は **Internal（社内のみ・審査不要）** なので gmail.modify でも Google 審査は不要。

## 0. 前提（マージ）
- 本 PR を `main` にマージ。CI（ruff / mypy / pytest / bandit / gitleaks）green を確認。
- 新規 import は pydantic / structlog のみ → `ci.yml` 変更不要。

## 1. ⚠️ 再同意（gmail.modify への移行・最重要）
- connect の取得スコープを `gmail.readonly` → **`gmail.modify`** に変更済み
  （`adapters/google_oauth_flow.py` `WORKSPACE_SCOPES`）。
- **既に readonly で connect 済みの人は、返信ドラフト(`mail_reply`)を使うには「連携」をやり直す**
  必要がある（@TeamAgent に「連携」/ DM。refresh token はスコープ束縛のため）。読み取り3機能は旧トークンでも動く。
- 「数名だけ」に出すなら、その数名に再 connect してもらえばよい。
- 同意画面には Gmail の「メールの読み取り・作成・送信・削除」権限が表示される（Google の文言。
  実際の送信/削除は denylist で封鎖）。営業へはその旨を周知する。

## 2. リリース前 pre-flight（落ちると全員サイレントに無反応）
| 確認 | 方法 | 落ちると |
|---|---|---|
| `OAUTH_KMS_KEY_ID` + `DATABASE_URL`(RDS) 到達 | Bot 起動ログ `mail_token_store_rds_initialized`（`_inmemory` なら NG） | 全員「未連携」 |
| `SLACK_BOT_TOKEN` に `users:read.email` | Slack App OAuth 設定 | user→email 解決不可で全員 fail-closed |
| `SLACK_BOT_TOKEN` に `chat:write`（ephemeral 送信） | 同上 | 結果を本人に返せない |
| `SLACK_WORKSPACE` 設定 | env | mail_to_internal_context の Slack permalink が出ない |

## 3. 機能フラグ
- **Slack ホットパス（rule-based）**: 追加フラグ不要。マージ＋Bot 再起動で 4 機能とも有効。
  結果は `_PRIVATE_SKILLS`（slack_bot.py）により**本人へ ephemeral 配信**。
- Bedrock 要約 in mail_to_internal_context（任意）: `USE_MAIL_LINK_SUMMARY=true`（既定 OFF）。
- orchestrator(Agent SDK) ツール登録（dark・任意）: `USE_MAIL_LINK_TOOL` / `USE_FOLLOWUP_TOOL` /
  `USE_MAIL_SUMMARY_TOOL` / `USE_MAIL_REPLY_TOOL`（既定 OFF。本番 Slack は rule-based 経路で届く）。

## 4. 段階ロールアウト
1. 管理者で smoke（「@TeamAgent 連携」or DM「連携」で認可 → 下記を各機能で確認）:
   - 「○○社の要返信メール教えて」→ 放置日数つきリスト（本人にだけ表示）
   - 「○○社のメール要約して」→ 横断要約
   - 「○○社のメール、社内で何か話してた?」→ 社内Slack/提案リンク
   - 「○○社のメールに返信作って」→ **Gmail 下書きに保存**＋Slackに下書き全文（本人にだけ）。
     Gmail の「下書き」に実際に入っていること、**送信されていない**ことを確認。
   - 未連携アカウントで実行 →「🔗 メール連携が必要です…」案内（汎用エラーにならない）。
2. 1 名パイロット → 数名 → 周知。

## 5. 営業向け使い方（周知文）
- 朝のTodo: 「○○社の要返信メール教えて」「3日以上 返信してない○○社のメールある?」
- 要約: 「○○社のメール要約して」
- 社内横断: 「○○社のメール、社内で何か話してた?」
- 返信作成: 「○○社のメールに返信作って」「○○社さんのメール、返信案ちょうだい」
  → AI が**下書き**を作成。Gmail で確認して**自分で送信**してください（AI は送信しません）。
- ※ クライアント名は必須（受信箱全体の無差別走査はしない）。名前が無いと聞き返します。
- ※ 結果は**あなたにだけ**表示されます（チャンネルには出ません）。

## 6. 既知の制限
- `mail_followup` は候補を `thread_id` で重複排除した後、`users.threads.get(format=metadata)`
  の末尾を精査し、自分の返信（SENT または本人 From）が最後のスレッドを除外する。本文は取得しない。
- 社内ナレッジは定期取り込みスナップショット（直近の会話は未反映の場合あり・注記表示）。
- 返信ドラフトは最新の受信1通を対象（特定メール指定 `target_message_id` は将来対応）。

## 7. ロールバック
- 全機能コード分岐＋既定フラグなので、問題時は revert / 該当 dispatch 分岐を無効化して再起動。
- データ面: 送信・削除は不可。`mail_reply` は Gmail に**下書き**を作るのみ（本人が削除可能）。
