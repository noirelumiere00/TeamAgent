# Secrets Manager ローテーションポリシー（TeamAgent v3.2）

**作成日**: 2026-05-22 Day 2
**Sprint 14 で自動化予定**：このドキュメントは手動運用ポリシー、Sprint 14 で Lambda automatic rotation に置き換える

---

## 1. 対象シークレット一覧

Wave2-⑦ 棚卸し（2026-06-15）で 9 secret に確定。Wave1〜2 で追加された 6 secret を本ポリシーに統合。

| # | Secret ID | 用途 | 周期 | 最終更新 | rotate オーナー | ECS 依存 |
|---|---|---|---|---|---|---|
| 1 | `teamagent/dev/db_password` | RDS 接続パスワード | **90 日** | 2026-05-21（apply 時） | DBA/小俣 | mcp / openclaw / worker EC2 |
| 2 | `teamagent/dev/slack/bot_token` | Slack Bot OAuth (xoxb-) | **180 日** | 2026-05-22（初期） | Slack workspace admin | mcp / openclaw / worker EC2 |
| 3 | `teamagent/dev/slack/app_token` | Slack App-Level Token (xapp-) | **180 日** | 2026-05-22 夜 | Slack workspace admin | worker EC2（Socket Mode）|
| 4 | `teamagent/dev/sentry_dsn` | Sentry DSN（任意） | **90 日**（実質無期限）| 2026-05-28 | DevOps | mcp / openclaw |
| 5 | `teamagent/dev/google_oauth` | Google OAuth JSON（client_id/secret/refresh_token）| **180 日** + 6mo 未使用で失効リスク | 2026-05-30 | Google Workspace admin（小俣） | worker EC2 |
| 6 | `teamagent/dev/vertex_sa` | GCP Vertex AI SA JSON（Gemini 動画分析） | **年次**（key rotation） | 2026-06-02 | GCP org admin | mcp / worker EC2 |
| 7 | `teamagent/dev/openclaw/gateway-token` | OpenClaw gateway operator token | **180 日** | 2026-06-12（go-live） | OpenClaw maintainer | openclaw |
| 8 | `teamagent/dev/mcp/bearer` | OpenClaw ↔ MCP bearer | **180 日** | 2026-06-12（go-live） | infra team | mcp + openclaw（同期必須）|
| 9 | `teamagent/prod/ops-slack-webhook` | ingest #ops 通知 webhook | **90 日**（webhook 再生成）| 未投入（Wave1-③ で追加・実値投入待ち）| Slack workspace admin | worker EC2 |

**未投入の Secret**: `teamagent/prod/ops-slack-webhook` は Wave1-③ で `OPS_SLACK_WEBHOOK_SECRET_NAME` env として配線済（systemd unit + load_secrets.sh）。
実値の Slack Incoming Webhook URL を AWS Secrets Manager に投入することで `ingest 失敗 → #ops 通知` が有効化される（未投入なら alerter は no-op で pipeline 続行）。

---

## 2. ローテーション手順

### 2.1 RDS パスワード（`teamagent/dev/db_password`）

```bash
# 1. 新パスワード生成
NEW_PW=$(openssl rand -base64 32 | tr -d "=+/" | cut -c1-32)

# 2. Secrets Manager 更新
aws secretsmanager update-secret \
  --secret-id teamagent/dev/db_password \
  --secret-string "$NEW_PW" \
  --region ap-northeast-1

# 3. 踏み台経由で RDS master password 変更
aws ssm start-session --target i-04fd1f367b454f641 --region ap-northeast-1
# 踏み台内で：
#   PGPASSWORD=$(aws secretsmanager get-secret-value --secret-id teamagent/dev/db_password \
#     --region ap-northeast-1 --query SecretString --output text)
#   psql -h teamagent-dev.c164uq6g8u35.ap-northeast-1.rds.amazonaws.com -U teamagent -d teamagent \
#     -c "ALTER USER teamagent WITH PASSWORD '<NEW_PW>';"

# 4. 動作確認
# ローカルから SSM port-forward 経由で接続テスト
```

### 2.2 Slack Bot Token（`teamagent/dev/slack/bot_token`）

1. [api.slack.com/apps](https://api.slack.com/apps) → TeamAgent Ver.2
2. 「OAuth & Permissions」→ 最下部「Revoke All OAuth Tokens」
3. 「Install App」→ 「Reinstall to Workspace」→ 新 xoxb- 取得
4. ```bash
   aws secretsmanager update-secret \
     --secret-id teamagent/dev/slack/bot_token \
     --secret-string "xoxb-..." \
     --region ap-northeast-1
   ```
5. Bot 再起動 + 疎通確認

### 2.3 Slack App Token（`teamagent/dev/slack/app_token`）

1. [api.slack.com/apps](https://api.slack.com/apps) → TeamAgent Ver.2 → 「Socket Mode」
2. 既存トークンを Revoke
3. 「Generate Token and Scopes」→ `connections:write` を追加して Generate
4. ```bash
   aws secretsmanager update-secret \
     --secret-id teamagent/dev/slack/app_token \
     --secret-string "xapp-..." \
     --region ap-northeast-1
   ```

### 2.4 Sentry DSN（`teamagent/dev/sentry_dsn`）

Sentry DSN は通常無期限だが、外部に露出した可能性がある場合は再生成。

1. Sentry プロジェクト → Settings → Client Keys (DSN) → **Generate New Key**
2. ```bash
   aws secretsmanager update-secret --secret-id teamagent/dev/sentry_dsn \
     --secret-string "https://...@o....ingest.sentry.io/..." --region ap-northeast-1
   ```
3. ECS service を再デプロイ（taskdef revision を上げて update-service）。
4. 旧 DSN は **24h 待ってから削除**（in-flight イベント取りこぼし防止）。

### 2.5 Google OAuth（`teamagent/dev/google_oauth`）

⚠️ `refresh_token` は 6 ヶ月未使用で失効する。**dev/staging でも定期実行**しているか確認すること。

1. Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 Client ID
2. クライアントシークレットを Reset（client_id は据え置き可）。
3. `scripts/get_google_refresh_token.py` を実行してユーザー同意フローで新 refresh_token を取得。
4. ```bash
   aws secretsmanager update-secret --secret-id teamagent/dev/google_oauth \
     --secret-string '{"client_id":"...","client_secret":"...","refresh_token":"..."}' \
     --region ap-northeast-1
   ```
5. worker EC2 で `scripts/load_secrets.sh` を re-source して env 再展開、Bot 再起動。
6. **dry-run**: `python scripts/ingest_sources.py --sources gdrive --dry-run` で疎通確認。

### 2.6 Vertex AI SA（`teamagent/dev/vertex_sa`）

GCP の Service Account JSON。年次 key rotation 推奨。

1. GCP Console → IAM → Service Accounts → 対象 SA → Keys → **ADD KEY (Create new key, JSON)**
2. ```bash
   aws secretsmanager update-secret --secret-id teamagent/dev/vertex_sa \
     --secret-string file://service-account-new.json --region ap-northeast-1
   ```
3. ECS / worker EC2 の `load_secrets.sh` が `/opt/teamagent/secrets/vertex-sa.json` に展開（0600）。
4. 旧 key は GCP Console で **DELETE**（24h 観察してから）。

### 2.7 OpenClaw Gateway Token（`teamagent/dev/openclaw/gateway-token`）

OpenClaw の操作者権限相当（loopback bind の gateway を叩く）。漏れたら即時 revoke。

1. ```bash
   NEW=$(openssl rand -base64 32 | tr -d "=+/" | cut -c1-40)
   aws secretsmanager update-secret --secret-id teamagent/dev/openclaw/gateway-token \
     --secret-string "$NEW" --region ap-northeast-1
   ```
2. ECS service `teamagent-dev-openclaw` を `update-service --force-new-deployment` で再起動。

### 2.8 MCP Bearer（`teamagent/dev/mcp/bearer`）

OpenClaw → MCP の internal 認証。**両 ECS service を同時に再起動**しないと bearer 不一致で 401。

1. ```bash
   NEW=$(openssl rand -base64 48 | tr -d "=+/" | cut -c1-64)
   aws secretsmanager update-secret --secret-id teamagent/dev/mcp/bearer \
     --secret-string "$NEW" --region ap-northeast-1
   ```
2. ECS の **両 service** を順番に update-service（mcp → openclaw）。MCP が先に新 bearer を受け入れる状態にしてから OpenClaw を切り替える。

### 2.9 ingest #ops Webhook（`teamagent/prod/ops-slack-webhook`）

Wave1-③ で配線。**初回は Secret 自体の作成**から（rotation ではなく投入）。

1. Slack → Apps → 新規 Incoming Webhooks 連携を作成 → 投稿先を `#ops` に設定 → webhook URL コピー
2. ```bash
   aws secretsmanager create-secret --region ap-northeast-1 \
     --name teamagent/prod/ops-slack-webhook \
     --secret-string "https://hooks.slack.com/services/..."
   ```
3. worker EC2 で `sudo systemctl restart teamagent-ingest.timer` し、次回 ingest 失敗時に #ops に通知が出るか確認（or 手動で `INGEST_DRY_RUN=1 sudo systemctl start teamagent-ingest.service`）。

Rotation 時は手順 2 を `update-secret` に変える。

---

## 3. ローテーション失敗時の対応

| 失敗ケース | 対応 |
|---|---|
| RDS への新パスワード ALTER 失敗 | 旧パスワードに戻して Secrets Manager rollback（`restore-secret`） |
| Slack Reinstall で Bot が消えた | 既存 Bot を全チャネルから一旦外し、再 invite |
| Bot 接続不能 | Socket Mode の自動再接続待ち（最大 5 分） |

---

## 4. 監視（Sprint 14 で自動化）

| 監視項目 | 方法（Sprint 14） | 暫定 |
|---|---|---|
| 最終更新日からの経過日数 | CloudWatch Lambda + Metric | カレンダー通知（手動） |
| ローテーション失敗 | CloudWatch Alarm + SNS | 手動確認 |
| シークレット読み取り権限のないユーザーアクセス | CloudTrail + AWS GuardDuty | 月次レビュー |

---

## 5. Sprint 14 で実装する自動ローテーション

**設計**：
- AWS Secrets Manager `automatic rotation` 機能
- Rotation Lambda：
  - RDS：master_user_password を ALTER USER で更新
  - Slack：手動 reinstall が必要なため、自動化対象外。代わりに「90日経過したら Slack 通知で reminder」
- Lambda は VPC 内、KMS-encrypted、Dead Letter Queue 付き

実装するファイル：
- `infra/terraform/secrets_rotation.tf`
- `services/secrets_rotation_lambda/`（Python）

---

## 6. インシデント対応

シークレット漏洩疑い時の即時対応：

1. **即時 Revoke**:
   - Slack：「OAuth & Permissions」→「Revoke All OAuth Tokens」
   - RDS：踏み台から `ALTER USER teamagent WITH PASSWORD 'TEMP_NEW_PW';`
2. **新シークレット生成** → Secrets Manager 更新
3. **影響範囲調査**：
   - CloudTrail で異常な API 呼び出しを検索
   - Slack audit log で異常な投稿/取得を検索
4. **インシデントレポート**：`docs/v3.2/ops/incidents/YYYYMMDD_secret_leak.md` に記録
5. **ユーザー周知**：影響範囲に応じて

---

## 7. パイロット前 Gate チェックリスト（Wave2-⑦）

P1 パイロット（営業 2-3 名・1 週間）に入る前に、以下を完了する。

- [ ] **棚卸し完了**: §1 の 9 secret の最終更新日と rotate オーナーが全て埋まっている
- [ ] **未投入 Secret の処置**: `teamagent/prod/ops-slack-webhook` を Slack 管理者が Incoming Webhook を新規発行し、§2.9 の手順で投入
- [ ] **長期未使用 refresh_token の事前 refresh**: `teamagent/dev/google_oauth` の refresh_token が「過去 6 ヶ月以内に使用された」ことを `journalctl -u teamagent-bot` か `usage_events where skill='ingest'` で確認
- [ ] **dry-run rotation**: §2.1 (RDS) を本番外時間帯に 1 回練習（旧パスワードに戻すロールバックも実施）
- [ ] **疎通テスト**: rotation 直後に `scripts/preflight_golive.sh`（既存）相当のチェック実行
- [ ] **オンコール体制**: rotation 中の障害対応者（小俣 + 補助 1 名）を確定

---

## 8. 棚卸し時点の git 露出状況（Wave2-⑦・2026-06-15 confirm）

`gitleaks` 設定（`.gitleaks.toml`）に従い、`xoxb-` / `xapp-` / AKIA / `-----BEGIN PRIVATE KEY` 等のパターンを grep。
**実値の露出は確認されない**（template / runbook 内のサンプル文字列のみ）。
コミット履歴の rotate 前トークンが残っていないことは `docs/security/security_audit_2026-06-12.md` で確認済。

---

## 更新履歴

| 日付 | バージョン | 更新内容 |
|---|---|---|
| 2026-05-22 | v1.0 | 初版（Day 2 完了時点・3 secrets） |
| 2026-06-15 | v1.1 | Wave2-⑦: 6 secrets 追記（Sentry / Google OAuth / Vertex SA / OpenClaw GW / MCP Bearer / OPS webhook）・パイロット前 gate チェックリスト追加・rotate オーナー明文化 |
