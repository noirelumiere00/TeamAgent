# Secrets Manager ローテーションポリシー（TeamAgent v3.2）

**作成日**: 2026-05-22 Day 2
**Sprint 14 で自動化予定**：このドキュメントは手動運用ポリシー、Sprint 14 で Lambda automatic rotation に置き換える

---

## 1. 対象シークレット一覧

| Secret ID | 用途 | 推奨ローテーション周期 | 最終更新 |
|---|---|---|---|
| `teamagent/dev/db_password` | RDS 接続パスワード | **90 日** | 2026-05-21（apply 時） |
| `teamagent/dev/slack/bot_token` | Slack Bot User OAuth Token (xoxb-) | **180 日** | 2026-05-22（初期） |
| `teamagent/dev/slack/app_token` | Slack App-Level Token (xapp-) | **180 日** | 2026-05-22 夜 |

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

## 更新履歴

| 日付 | バージョン | 更新内容 |
|---|---|---|
| 2026-05-22 | v1.0 | 初版（Day 2 完了時点） |
