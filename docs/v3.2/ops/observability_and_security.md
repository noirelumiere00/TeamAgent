# 観測 & セキュリティ運用メモ（Sprint 2 / 2.6・2.7）

最終更新: 2026-05-23

このドキュメントは `infra/terraform/cloudwatch.tf` と `infra/terraform/security.tf` で
構築される観測 / セキュリティ基盤の運用手順をまとめたものです。

---

## 1. 観測基盤（cloudwatch.tf）

### 1.1 構築されるもの

| リソース | 用途 |
|---|---|
| SNS Topic `teamagent-<env>-alarms` | アラーム通知先（メール → 後で Slack Chatbot） |
| メトリクスフィルタ `BedrockCostUSD` | 構造化ログの `cost_usd` を時系列メトリクス化 |
| メトリクスフィルタ `SkillLatencyMs` | 構造化ログの `latency_ms` を時系列メトリクス化 |
| メトリクスフィルタ `ErrorCount` | `level=error` or 既知エラーイベントをカウント |
| アラーム `daily-bedrock-cost-high` | 24h 合算コスト > 5 USD（既定） |
| アラーム `p95-latency-high` | 15 分窓の p95 latency > 15s |
| アラーム `error-spike` | 5 分窓のエラー >= 3 件 |

メトリクス名前空間: `TeamAgent/<env>`

### 1.2 apply 手順

```bash
cd infra/terraform
# tfvars に通知メールを追加
cat >> terraform.tfvars <<'EOF'
alarm_email_endpoints = ["s-komata@vectorinc.co.jp"]
EOF

terraform plan -target=aws_sns_topic.alarms \
               -target=aws_cloudwatch_log_metric_filter.cost_usd \
               -target=aws_cloudwatch_log_metric_filter.latency_ms \
               -target=aws_cloudwatch_log_metric_filter.error_count \
               -target=aws_cloudwatch_metric_alarm.daily_cost_high \
               -target=aws_cloudwatch_metric_alarm.p95_latency_high \
               -target=aws_cloudwatch_metric_alarm.error_spike

terraform apply
# 受信箱で SNS subscription confirmation メールを承認
```

### 1.3 メトリクスが取れる前提条件

CloudWatch メトリクスフィルタは **JSON 構造化ログのフィールドを参照** する。
そのため `runtime/slack_bot.py` から発出されるログには **必ず以下が含まれる必要**：

```json
{ "event": "search_skill_done", "cost_usd": 0.0123, "latency_ms": 8421, "request_id": "..." }
```

SearchSkill / BedrockClient は CLAUDE.md 6-bis に従ってこれを満たす実装になっている。
新規 Skill を追加する際は同じパターンを踏襲する。

### 1.4 Slack 通知への移行（次ステップ）

現状はメール通知のみ。Slack 化は以下のいずれか：

1. **AWS Chatbot**（推奨）: SNS Topic を Slack ワークスペースの `#alerts` に転送
   - https://console.aws.amazon.com/chatbot/ で設定（手動）
2. **Lambda + slack_sdk**: SNS → Lambda → `chat.postMessage`
   - 既存の `SlackClient` を流用可能

### 1.5 Sentry 連携（Day 3 追加）

`src/teamagent/observability/sentry.py` で Sentry SDK を統合済。

**運用フロー**:
1. Sentry プロジェクト作成（Project 名: `teamagent-dev` / Platform: Python）
2. DSN を Secrets Manager に保管:
   ```bash
   aws secretsmanager create-secret \
       --name teamagent/dev/sentry_dsn \
       --secret-string 'https://xxx@yyy.ingest.sentry.io/zzz' \
       --region ap-northeast-1
   ```
3. Bot 起動時に `SENTRY_DSN` を環境変数に展開（`scripts/load_secrets.sh` が自動取得）
4. Bot 起動ログに `slack_bot_start sentry_enabled=true` が出れば成功

**設計のポイント**:
- DSN 未設定なら `init_sentry()` は no-op で False を返す（dev / テスト安全）
- `before_send` で xoxb- / sk-ant- / AKIA* / メール / 電話 / 2000 文字超を再帰スクラブ
- `LoggingIntegration(event_level=None)` で例外二重送信を防止
- `AsyncioIntegration()` は async 文脈内で init（Socket Mode loop 取りこぼし対策）
- `@app.error` + `loop.set_exception_handler` で Bolt 内外の例外を二重キャッチ
- `traces_sample_rate=0.05` / `profiles_sample_rate=0.0`（Sentry 無料枠想定）

**ローテーション**: DSN は Sprint 14 の Secrets ローテーション Lambda の対象に含める。

---

## 2. セキュリティ基盤（security.tf）

### 2.1 構築されるもの

| リソース | 用途 |
|---|---|
| KMS CMK `alias/teamagent-<env>-logs` | CloudTrail / Bedrock invocation logs の共通暗号化 |
| S3 `teamagent-<env>-cloudtrail-<accountId>` | CloudTrail 配信先（Public Access Block + KMS） |
| `aws_cloudtrail.main` | multi-region + log file validation 有効 |
| `aws_accessanalyzer_analyzer.account` | IAM Access Analyzer（アカウント単位） |
| S3 `teamagent-<env>-bedrock-logs-<accountId>` | Bedrock invocation logs 配信先 |
| `aws_bedrock_model_invocation_logging_configuration.main` | Bedrock 呼び出しログ有効化 |

### 2.2 既知の注意点

- **Bedrock invocation logging は 1 アカウント × 1 リージョン × 1 設定**。
  既に手動で設定してある場合は tfvars で `enable_bedrock_invocation_logging = false` にして
  Terraform 管理外に置く。次回の Sprint で import を検討。
- **CloudTrail** も同様に「既存 trail があるかを `aws cloudtrail list-trails` で先に確認」。
  既存があれば `enable_cloudtrail = false`。
- **RDS の SSL 強制**：`infra/terraform/rds.tf` の parameter group に
  `rds.force_ssl = 1` を追加済。**parameter group 適用には DB 再起動が必要** な場合があるので
  apply 前にメンテ時間を確保する。

### 2.3 apply 手順（dev）

```bash
cd infra/terraform

# 既存 CloudTrail / Bedrock logs の有無を確認
aws cloudtrail list-trails --region ap-northeast-1
aws bedrock get-model-invocation-logging-configuration --region ap-northeast-1

# 既にあれば tfvars で false 指定
cat >> terraform.tfvars <<'EOF'
enable_cloudtrail = true   # ← 既存があれば false に
enable_iam_access_analyzer = true
enable_bedrock_invocation_logging = true
EOF

terraform plan
terraform apply
```

### 2.4 検証

```bash
# CloudTrail が configure 通り動いてるか
aws cloudtrail get-trail-status --name teamagent-dev-trail --region ap-northeast-1

# IAM Access Analyzer の findings
aws accessanalyzer list-findings \
    --analyzer-arn $(terraform output -raw access_analyzer_arn)

# Bedrock invocation logging
aws bedrock get-model-invocation-logging-configuration --region ap-northeast-1

# RDS SSL 強制が効いてるか（拒否されることを確認）
PGSSLMODE=disable psql -h <endpoint> -U teamagent -d teamagent -c 'SELECT 1'
# → ERROR: no pg_hba.conf entry for host ... no encryption が出れば OK
```

---

## 3. PII / シークレット漏洩スキャン

`scripts/pii_log_scan.py` で過去 N 時間の CloudWatch Logs を走査する。

```bash
# 直近 24 時間を default ロググループに対して
python scripts/pii_log_scan.py --hours 24

# 顧客名も grep（カンマ区切り。CI のシークレットから注入推奨）
python scripts/pii_log_scan.py --hours 168 --customers "INPEX,森ビル"

# 結果を JSON 保存（GitHub Actions の artifact 用）
python scripts/pii_log_scan.py --hours 24 --output reports/pii_$(date +%Y%m%d).json
```

検出パターン：
- Slack bot/app token, Anthropic key, AWS access key, Google API key
- メール / 電話番号 / 2000 文字以上の長文（PDF 全文混入の可能性）
- `--customers` で渡した固有名詞

**exit code**: 0 = clean、1 = 1 件以上検出、2 = scan エラー。
GitHub Actions の weekly job に組み込んで失敗時に Slack 通知する想定。

---

## 4. ローテーション期日トラッキング

実 secret の自動ローテーションは Sprint 14 で Lambda 化する予定（→ `secrets_rotation_policy.md`）。
それまでの暫定として `teamagent/<env>/_rotation_marker` というマーカー secret を作成し、
タグ `NextRotationDue=YYYY-MM-DD` で次回期日を管理する。

```bash
# 次回期日確認
aws secretsmanager describe-secret \
    --secret-id teamagent/dev/_rotation_marker \
    --query 'Tags' --output table

# ローテーション実施後にタグ更新
aws secretsmanager tag-resource \
    --secret-id teamagent/dev/_rotation_marker \
    --tags Key=NextRotationDue,Value=2026-11-22
```
