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

## Guarded image build and deployment

Image build and deployment are deliberately separate:

1. Run `infra/deploy/build_teamagent_image.sh` from a clean local `dev` branch
   whose HEAD is the freshly fetched `origin/dev`. The launcher assumes
   `teamagent-dev-codebuild-launcher`, uploads only the fixed versioned source
   key, and returns a digest from the release repository after quarantine
   provenance and scan gates pass.
2. Review a Terraform plan that sets `mcp_image` (or
   `tiktok_acquire_image`) to that exact **release repository digest**. A task
   definition must never reference a repository whose name ends in
   `-quarantine`.
3. Apply the reviewed plan as a separate, explicitly authorized deployment.
   The build launcher itself never calls ECS or EventBridge.

The inline deny on the `AIIAdev` IAM user blocks direct `StartBuild`; operators
must assume the dedicated launcher role. Any AWS root access keys remain an
external account-level blocker because IAM policies cannot restrict the root
principal. Rotate/delete root keys and retain only the normal break-glass root
login before treating the launcher boundary as complete.

### P1 activation and merge order

The provenance/IAM/quarantine remediation must merge before either image
implementation. Both checked-in contracts intentionally have `release.ready =
false`, so this ordering cannot publish a partial cross-branch implementation.

For the main MCP image:

1. Merge this provenance change without the Boyle-owned Dockerfile, Python lock
   files, TikTok package files, or core/media image implementation.
2. Merge Boyle's Wolfi/Chainguard arm64 Dockerfile. It must implement every
   build argument, meaningful use, OCI label, and canonical receipt declared by
   `teamagent_runtime_contract.json`; all external images must be child-digest
   pinned, never mutable tags.
3. In a reviewed follow-up, complete the allowlist with measured builder/runtime
   child digests, exact Wolfi package versions, installed binary SHA-256 values,
   app HTML identity, model revision, and commit binding. Set `release.ready`
   only after the actual candidate passes the zero-exception C/H=0 scan. The
   checked-in Dockerfile contract test becomes mandatory as soon as the flag is
   enabled.

For OpenClaw:

1. Merge this isolated project/role, signed-source, quarantine, scan, referrer,
   and evidence boundary while `openclaw_bundle_contract.json` remains blocked.
2. Merge Boyle's core/media builder, evidence verifier, and recursive
   digest-preserving promoter at the three fixed contract interfaces. Each
   arm64 child needs signed provenance and SBOM referrers and must pass C/H=0.
3. Set the OpenClaw contract ready only after those interfaces and signatures
   are independently verified. Complete the GitHub CodeConnections handshake
   and any Terraform apply later as separately authorized infrastructure work.

OpenClaw uses CodeConnections with one full `dev` commit SHA; it does not use a
shared or legacy S3 source ZIP. S3 stores only KMS-signed, versioned,
COMPLIANCE-locked source manifests and release evidence under content-addressed
keys. The currently deployed `9cde4c...` OpenClaw digest is an OCI image index
and ECR basic scan reports `UnsupportedImageType`; it is therefore ineligible
for this path. New release subjects must be a single OCI image manifest whose
config is exactly `linux/arm64`, whose revision is the full source commit, and
whose verified quarantine digest passes ECR C/H=0 before promotion.

Neither safe launcher updates ECS, EventBridge, task definitions, services, or
schedules. Deployment remains a separate guarded and explicitly authorized
operation.

### Existing CodeBuild log groups

CodeBuild log groups are Terraform-managed with 30-day retention. Before the
first separately authorized apply of this change, import the two groups found
by the audit so Terraform changes retention in place instead of attempting to
create existing names:

```bash
terraform import aws_cloudwatch_log_group.codebuild_image /aws/codebuild/teamagent-dev-image-builder
terraform import aws_cloudwatch_log_group.codebuild_aiia_image_legacy /aws/codebuild/aiia-image-builder
```

The second resource manages retention only and does not recreate the retired
AI-IA CodeBuild project. Buildspecs and launchers keep shell tracing disabled,
pipe ECR credentials only to fixed-registry `docker login --password-stdin`,
and never print temporary AWS credentials or KMS signature material.

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
