# Terraform — TeamAgent v3.0 AWS インフラ

## ファイル構成

| ファイル | 中身 |
|---|---|
| `main.tf` | プロバイダ・バックエンド定義 |
| `variables.tf` | 入力変数の定義 |
| `rds.tf` | PostgreSQL 16 + pgvector / Secrets / Subnet / SG |
| `lambda_iam.tf` | Lambda 実行ロール + S3 バケット + CloudWatch Logs |
| `outputs.tf` | 出力値 |
| `terraform.tfvars.example` | 変数値テンプレート |

## 使い方

```bash
cd infra/terraform

# 1. AWS 認証
aws configure  # or export AWS_PROFILE=...

# 2. 変数ファイル準備
cp terraform.tfvars.example terraform.tfvars
vi terraform.tfvars

# 3. 初期化と確認
terraform init
terraform plan

# 4. 適用
terraform apply
```

## 構築されるリソース

- RDS PostgreSQL 16 (`db.t4g.medium` 〜 `db.r7g.large`)
- DB パラメータグループ（pgvector 用）
- Secrets Manager（DB パスワード）
- S3 バケット（生ファイル保存）
- Lambda 実行 IAM Role（Bedrock / Secrets / S3 アクセス権）
- CloudWatch Logs Group

## Lambda 本体について

Lambda 関数本体は **コード zip / コンテナイメージが必要**なので、コード実装が進んでから有効化する想定で、現状はコメントアウトしています（`lambda_iam.tf` 末尾）。

## pgvector の有効化

RDS 作成後、初回接続時に SQL を流す必要があります：

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

Alembic マイグレーション初回で実行する設計。

## 注意

- 現状の SG はデフォルト VPC 全範囲を許可しています。本番は Lambda SG → DB SG に絞り込みが必要。
- 本番運用時は backend "s3" を有効化して、tfstate を S3 + DynamoDB ロックで管理してください。
- 削除保護は `environment = "prod"` のとき自動で有効化されます。
