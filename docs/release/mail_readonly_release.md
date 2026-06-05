# メール機能 初回リリース手順（読み取り専用2機能）

対象: `mail_to_internal_context`（メール×社内ナレッジ横断）/ `mail_followup`（要返信トリアージ）。
いずれも **per-user OAuth・`gmail.readonly`・読み取り専用**（書き込み/送信なし）。各営業が
`/teamagent connect` で自分の Google を認可済みであることが前提。

> ⚠️ 公開＝Bot が 16 名の **個人受信箱（メタデータ）を読む** ことを意味する。プライバシー重大
> ゲートのため、本番反映は管理者の明示承認のもとで実施する。本機能はメール本文を LLM に渡さず、
> 件名はマスク・相手はドメイン/マスク、送信・削除はスコープ＋アダプタ denylist で物理的に不可。

## 0. 前提（マージ）
- 本 PR を `main` にマージ。CI（pytest / ruff format --check / ruff check）green を確認。
- 新規 import は pydantic / structlog のみ → `ci.yml` の手動依存列挙の変更は不要。

## 1. リリース前 pre-flight（**ここが落ちると全員サイレントに未連携/無反応**）
本番 Bot プロセスの環境で、以下を必ず確認:

| # | 確認項目 | 確認方法 | 落ちると |
|---|---|---|---|
| 1 | `OAUTH_KMS_KEY_ID` 設定 ＋ `DATABASE_URL`(RDS) 到達（SSM 踏み台/トンネル） | Bot 起動ログに `mail_token_store_rds_initialized` が出る（`mail_token_store_inmemory` が出たら NG） | TokenStore が InMemory（空）に落ち、**全員「未連携」** |
| 2 | `SLACK_BOT_TOKEN` に `users:read.email` スコープ | Slack App 設定 → OAuth & Permissions | user→email 解決不可で **全員 fail-closed** |
| 3 | `SLACK_WORKSPACE`（例 `vectorinc`）設定 | env | feature1 の Slack permalink が出ない（リンクが消える） |

## 2. 機能フラグ
- **Slack ホットパス（rule-based）**: 追加フラグ不要。マージ＋再起動で `intent.py`/`slack_bot.py` 経由で有効。
- **Bedrock 要約（feature1・任意）**: 既定 OFF。価値が確認できたら `USE_MAIL_LINK_SUMMARY=true`。
- **orchestrator(Agent SDK) ツール登録（dark・任意）**: `USE_MAIL_LINK_TOOL=true` / `USE_FOLLOWUP_TOOL=true`（既定 OFF。本番 Slack は rule-based 経路で届くため通常は不要）。

## 3. 段階ロールアウト
1. **自分(管理者)で smoke**: `/teamagent connect` 済みアカウントで Slack で実行:
   - 「（実在クライアント）のメール、社内で何か話してた?」→ メール件数＋社内リンクが返る
   - 「（実在クライアント）の要返信メール教えて」→ 放置日数つきリスト＋「返信済みかは未判定」注記
   - 未連携アカウントで実行 →「🔗 メール連携が必要です。`/teamagent connect`…」案内が出る（汎用エラーにならない）
2. **1名パイロット**（営業1名に connect → 上記2問）。
3. 問題なければ **全営業へ周知**（使い方フレーズ＝下記）。

## 4. 営業向け使い方（周知文）
- 社内の関連を見る: 「**○○社のメール、社内で何か話してた?**」「○○社のメール、関連する過去提案ある?」
- 要返信の棚卸し: 「**○○社の要返信メール教えて**」「3日以上 返信してない○○社のメールある?」
- ※ クライアント名は必須（受信箱全体の無差別走査はしない設計）。名前が無いと聞き返します。
- ※「メールの返信ドラフト作成」は次リリース（Google 追加認可が必要）。

## 5. 既知の制限（第2リリース以降）
- **返信ドラフト生成**: `create_draft` は `gmail.modify` 必須。現 connect は `gmail.readonly` のみ付与のため、
  スコープ拡張＋**全営業の再同意**＋Google 検証審査(Tier-2 Sensitive) が前提。アダプタ実装は完了済み。
- `mail_followup` の「未返信」断定: `users.threads.get` ラッパー未実装のため現状は「相手から最後に来た
  メール（返信済みかは未判定）」。スレッド精査ラッパー追加で精度向上予定。
- 社内ナレッジは定期取り込みスナップショット（ごく直近の会話は未反映の場合あり・注記を表示）。

## 6. ロールバック
- Slack 経路はコード分岐のため、問題時は revert（または該当 dispatch 分岐を無効化）して再起動。
  書き込み副作用が無い（readonly）ため、データ面のロールバックは不要。
