# ローカル開発で本番 RDS を使う手順（SSM tunnel 経由）

最終更新: 2026-05-26

## 背景

本番 RDS の Security Group は **踏み台 EC2 (`i-04fd1f367b454f641`) からの 5432 のみ許可**しており、
ローカル Mac から直接接続できない（設計通り）。

開発機から本番 RDS を叩いて E2E 動作確認する時は、SSM Port Forwarding でトンネルを張る必要がある。

```
Mac (localhost:15432)
       │
       │ SSM Session (HTTPS over AWS API)
       ▼
踏み台 EC2 (i-04fd1f367b454f641)
       │ port forward
       ▼
RDS (teamagent-dev.<endpoint>:5432)
```

---

## 前提

- `aws configure` で TeamAgent 用プロファイルが設定済み
- `session-manager-plugin` がインストール済み（`which session-manager-plugin` で確認）
- 本番 RDS endpoint を `aws rds describe-db-instances` で取得済み

```bash
# 本番 RDS endpoint を取得
aws rds describe-db-instances \
    --db-instance-identifier teamagent-dev \
    --query 'DBInstances[0].Endpoint.Address' \
    --output text \
    --region ap-northeast-1
# → teamagent-dev.c164uq6g8u35.ap-northeast-1.rds.amazonaws.com
```

---

## ステップ 1: SSM トンネルを起動（別 Terminal、放置）

```bash
aws ssm start-session \
    --target i-04fd1f367b454f641 \
    --document-name AWS-StartPortForwardingSessionToRemoteHost \
    --parameters '{"host":["teamagent-dev.c164uq6g8u35.ap-northeast-1.rds.amazonaws.com"],"portNumber":["5432"],"localPortNumber":["15432"]}' \
    --region ap-northeast-1
```

成功すると：
```
Starting session with SessionId: root-...
Port 15432 opened for sessionId ...
Waiting for connections...
```

> **このタブは閉じない**。閉じるとトンネルが切れる。

> `localPortNumber=15432` を選んでる理由：ローカル docker-compose の pgvector (5432) との衝突回避。
> 別 port が使いたければ `.env.local` の `RDS_PORT=` を合わせて書き換える。

---

## ステップ 2: Bot を起動（別 Terminal）

```bash
cd ~/Documents/TeamAgent

# 初回のみ：テンプレを .env.local にコピー
cp .env.local.template .env.local

# 環境変数読み込み → Secrets Manager から動的取得 → Bot 起動
source .venv/bin/activate
set -a; source .env.local; set +a
source scripts/load_secrets.sh
python -m teamagent.runtime.slack_bot
```

`load_secrets.sh` の出力で：
```
[load_secrets] MODE: local (SSM tunnel 経由想定 / RDS_HOST=localhost:15432)
[load_secrets] OK: DATABASE_URL を組み立て（host=localhost, ssl=require）
[load_secrets] OK: Slack tokens loaded
[load_secrets] OK: SENTRY_DSN loaded
```
が出れば OK。

Bot 起動ログで `slack_bot_start sentry_enabled=True` + `⚡️ Bolt app is running!` が出れば完成。

---

## 動作確認

Slack で：
```
@TeamAgent_Dev_Ver.2 INPEX案件の提案内容を教えて
```

SSM tunnel タブに `Connection accepted for session [...]` が出れば、Bot → tunnel → RDS が繋がった証拠。

---

## トラブルシューティング

| 症状 | 原因 / 対処 |
|---|---|
| `ERROR: RDS_HOST にプレースホルダ '__RDS_ENDPOINT__' が残っています` | `.env.production` の編集忘れ。`.env.local` を使うなら `RDS_HOST=localhost` が入っているはずなので、`.env.local.template` をコピーし直す |
| `psycopg.OperationalError: failed to resolve host 'teamagent-dev.'` | bash の `<>` がリダイレクト解釈で壊れた。`.env.local.template` 経由なら起きないはず |
| `connection failed: ... 172.31.39.131` | DATABASE_URL が VPC private IP に向いている = tunnel に向いてない。`.env.local` の `RDS_HOST=localhost` を確認、Bot 再起動 |
| `psycopg.OperationalError: SSL SYSCALL error: EOF detected` | SSL ハンドシェイク失敗 → `.env.local` の `RDS_SSL_MODE=disable` に下げる |
| `aws ssm start-session: SessionManagerPlugin is not found` | `~/.local/bin/session-manager-plugin` を PATH に追加 |
| `psycopg.errors.UndefinedTable: "proposals_chunks_contextual"` | 本番に Contextual テーブル無 → `export USE_CONTEXTUAL=false` で再起動 |

---

## 後始末

開発が終わったら：
- SSM tunnel タブを Ctrl+C で停止（不要なセッション課金を避ける）
- Bot Terminal も Ctrl+C で停止

---

## 本番 EC2 / Lambda デプロイ時の差分

本番デプロイ後は `.env.production` を使う：

```bash
cp .env.production.template .env.production
# RDS_HOST=__RDS_ENDPOINT__ を実値に手動置換
# 取得：aws rds describe-db-instances ... --query 'DBInstances[0].Endpoint.Address'
```

EC2 / Lambda は VPC 内で動くので tunnel 不要、`RDS_HOST` に実 endpoint を直接書く。

---

## 関連ファイル

- `.env.production.template` — 本番 EC2/Lambda 用テンプレ（RDS endpoint プレースホルダ）
- `.env.local.template` — ローカル Mac 用テンプレ（RDS_HOST=localhost、tunnel 前提）
- `scripts/load_secrets.sh` — Secrets Manager から動的取得 + プレースホルダ検知 + mode ログ
- `docs/v3.2/ops/observability_and_security.md` — Sentry / CloudWatch / セキュリティ
- `docs/v3.2/ops/secrets_rotation_policy.md` — Secrets ローテーション設計
