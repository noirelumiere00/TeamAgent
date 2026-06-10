# OpenClaw 前面 P1 パイロット — デプロイ Runbook（§I / M2-M5）

OpenClaw 外殻 ＋ TeamAgent-MCP 境界（会社共有モデル §G）を **ECS Fargate** に出し、専用Slackチャネル・
少数(2-3名)・**読取のみ**で実稼働させるまでの本人手順。`terraform apply` は **plain 禁止＝targeted/plan確認**。
全コードは authoring 済（dev/PR#118）。本書は **apply＝本人操作**の手順だけを示す。

> 関連: プラン `~/.claude/plans/mossy-snacking-locket.md` §A/§C/§D/§G/§H/§I。IaC=`infra/terraform/{fargate,ecr,vpc_endpoints,cloudwatch_fargate,outputs_fargate}.tf`、
> イメージ=`infra/docker/Dockerfile.{teamagent-mcp,openclaw}`、smoke=`scripts/smoke_mcp.py`。

## 0. 前提（gated）
- [ ] **ゲート①承認**：OpenClaw(Node コンテナ)を本番 AWS に持ち込む承認。
- [ ] **Bedrock モデル確認**：`aws bedrock list-inference-profiles --region ap-northeast-1` で
      Haiku4.5 の推論プロファイル ID を確定（`variables_fargate.tf:openclaw_model_id` / `openclaw.config.json5` の `★deploy時要確認` を実値へ）。
- [ ] リージョン=`ap-northeast-1`、account=`718959508629`、tfstate=既存 S3 backend（`main.tf:32-38`）。

## 1. Secrets 作成（値は本人が投入・コミット禁止）
Secrets Manager に 5 つ作成（名前は `variables_fargate.tf` の default に合わせる）:
```sh
R=ap-northeast-1
aws secretsmanager create-secret --region $R --name teamagent/dev/mcp/bearer            --secret-string "$(openssl rand -hex 32)"
aws secretsmanager create-secret --region $R --name teamagent/dev/database-url           --secret-string "postgresql://USER:PASS@HOST:5432/teamagent?sslmode=require"
aws secretsmanager create-secret --region $R --name teamagent/dev/openclaw/slack-bot-token --secret-string "xoxb-..."   # 手順4で取得
aws secretsmanager create-secret --region $R --name teamagent/dev/openclaw/slack-app-token --secret-string "xapp-..."   # 手順4で取得
aws secretsmanager create-secret --region $R --name teamagent/dev/openclaw/gateway-token   --secret-string "$(openssl rand -hex 32)"
```
- `teamagent/dev/mcp/bearer` と `gateway-token` は新規ランダム。`database-url` は既存 RDS（password は `teamagent/dev/*` の DB secret 参照可）。
- ⚠️ `fargate.tf` は **secret 実在を前提**（`data.aws_secretsmanager_secret`）＝この手順を terraform plan より先に。

## 2. イメージ build & ECR push（2つ）
```sh
# ECR repo だけ先に作る（targeted）
cd infra/terraform && terraform plan -target=aws_ecr_repository.mcp -target=aws_ecr_repository.openclaw
terraform apply -target=aws_ecr_repository.mcp -target=aws_ecr_repository.openclaw   # 本人確認のうえ
MCP_URL=$(terraform output -raw ecr_mcp_url); OC_URL=$(terraform output -raw ecr_openclaw_url); cd ../..
aws ecr get-login-password --region ap-northeast-1 | docker login --username AWS --password-stdin "${MCP_URL%/*}"

# arm64 でビルド（Fargate=ARM）。BuildKit 前提。
docker buildx build --platform linux/arm64 -f infra/docker/Dockerfile.teamagent-mcp -t "$MCP_URL:p1" --push .
docker buildx build --platform linux/arm64 -f infra/docker/Dockerfile.openclaw       -t "$OC_URL:p1"  --push .
# digest を控える（IMMUTABLE pin 推奨）
MCP_DIGEST=$(aws ecr describe-images --repository-name teamagent-mcp     --image-ids imageTag=p1 --query 'imageDetails[0].imageDigest' --output text)
OC_DIGEST=$(aws ecr describe-images  --repository-name teamagent-openclaw --image-ids imageTag=p1 --query 'imageDetails[0].imageDigest' --output text)
```

## 3. Terraform apply（targeted・順序厳守）
`infra/terraform/terraform.tfvars`（git管理外）に:
```hcl
mcp_image              = "<MCP_URL>@<MCP_DIGEST>"
openclaw_image         = "<OC_URL>@<OC_DIGEST>"
shared_company_domains = "vectorinc.co.jp"        # §G 会社共有ドメイン
openclaw_model_id      = "jp.anthropic.claude-haiku-4-5"   # 手順0で確定した値
enable_vpc_endpoints   = true
alarm_email_endpoints  = ["you@vectorinc.co.jp"]
```
段階 apply（plan を毎回確認）:
```sh
cd infra/terraform
terraform plan   # 全差分レビュー（特に IAM Deny / SG / secrets data source 解決）
# 役割→ネットワーク→クラスタ/ログ→Cloud Map→task def→service の順
terraform apply -target=aws_iam_role.ecs_execution -target=aws_iam_role.mcp_task -target=aws_iam_role.openclaw_task
terraform apply -target=aws_security_group.openclaw -target=aws_security_group.mcp -target=aws_security_group_rule.db_from_mcp
terraform apply -target=aws_ecs_cluster.main -target=aws_cloudwatch_log_group.mcp -target=aws_cloudwatch_log_group.openclaw -target=aws_service_discovery_service.mcp
terraform apply -target=aws_ecs_service.mcp           # MCP 先（OpenClaw が依存）
terraform apply -target=aws_ecs_service.openclaw
terraform apply -target=aws_cloudwatch_dashboard.fargate   # 観測（任意で alarms も）
```
- 検証: **OpenClaw タスクロールで `secretsmanager:GetSecretValue` が拒否**されることを IAM Policy Simulator で確認。

## 4. 新 Slack アプリ（OpenClaw 専用・Socket Mode）
- Slack で **新規アプリ**を作成（既存 Bot とは別＝Socket Mode 二重接続回避）。**専用チャネル**を1つ用意。
- スコープ: `app_mentions:read`/`chat:write`/`channels:history`(+DM 要件) など。**Socket Mode 有効**で `connections:write`。
- 取得した `xoxb-`/`xapp-` を手順1の secret（`slack-bot-token`/`slack-app-token`）に `put-secret-value` で投入。
- service を再デプロイ（`aws ecs update-service --force-new-deployment`）してトークンを反映。

## 5. DB マイグレーション（SSM トンネル・要承認）
SSM トンネルで RDS へ接続し、未適用分を流す（**会社共有モデルの前提**）:
```sh
# 既存踏み台/worker 経由 SSM port forward → psql で
psql "$DATABASE_URL" -f infra/migrations/0010_rls_email_case_insensitive.sql
# 0011 は <COMPANY_DOMAIN> を実値へ置換してから
sed 's/<COMPANY_DOMAIN>/vectorinc.co.jp/g' infra/migrations/0011_backfill_company_acl_groups.sql | psql "$DATABASE_URL"
```
- **RLS 実走検証（M1/P0）**: 2 ユーザ相当で「会社ドメイン doc は見える / 会社外は0 / `user_role=admin` 詐称は無効」を確認（`scripts/smoke_mcp.py --full` か手動 SQL）。

## 6. 起動確認 & smoke
```sh
# タスクが RUNNING / healthz green を確認
aws ecs describe-services --cluster $(terraform -chdir=infra/terraform output -raw ecs_cluster_name) \
  --services $(terraform -chdir=infra/terraform output -raw ecs_service_mcp) --query 'services[0].deployments'
# MCP へ（SSM トンネルで 8787 を localhost へ転送して）smoke
TEAMAGENT_MCP_BEARER=<bearer値> TEAMAGENT_SHARED_COMPANY_DOMAINS=vectorinc.co.jp \
  uv run python scripts/smoke_mcp.py --base-url http://127.0.0.1:8787 --full
# 期待: healthz=200 / bearer無=401 / tools=会社ナレッジ4のみ / (--full) search=会社ドメインdocのみ
```
- Slack 専用チャネルで実ユーザが「検索/カルテ/提案」を投げ、**他人に返答が混ざらない**（`dmScope:per-channel-peer`）ことを 2 人同時で確認。

## 7. ロールバック（~1分・可逆）
OpenClaw は**専用 Slack アプリ/チャネル**・現行 Bot は**既存チャネル**で物理分離。ロールバック＝**OpenClaw を止めるだけ**（現行 Bot は無停止）:
```sh
aws ecs update-service --cluster <cluster> --service <ecs_service_openclaw> --desired-count 0
```
- `USE_OPENCLAW_FRONTEND` の**コード実装は不要**（物理分離のため運用ロールバックで足る）。MCP バックエンドは残してよい（現行 Bot からは使わない／将来の入口）。

## 8. パイロット運用ゲート（→P2 判断）
- 専用ch・2-3名・読取のみで**1週間**。`teamagent-dev-openclaw-pilot` ダッシュボードで監視。
- 合格: **同時4で p95≤15s／エラー<1%／RLS越権0（会社外/admin不可）／コスト許容／無事故**＋ OpenClaw 単一GWの同時実行上限・月運用工数を記録。

## コスト目安（パイロット）
- Fargate 2 task（小）＋ ECR ＋ CloudWatch ＋ VPC endpoints(任意 ~$7/月×6) ＋ Bedrock(Haiku外側+cache)。詳細は §9。
