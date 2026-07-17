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

## Guarded image provenance and deployment

No build launcher deploys anything. The enforced flow is:

```text
independent signed source
  -> builder writes quarantine only
  -> source-free attestor verifies the exact linux/arm64 digest
  -> source-free promoter copies quarantine -> verified-candidates
  -> guarded release authorization revalidates candidate + signatures
  -> source-free promoter copies verified-candidates -> release
  -> Terraform accepts release-repository @sha256 only with fresh signed evidence
```

The attestor requires exact OCI labels, full commit, contract hash, installed
binary hashes, Trivy actual-image CRITICAL=0/HIGH=0/secret=0, one exact SPDX
SBOM referrer, one exact in-toto provenance referrer, and cryptographic
signatures over the image and both referrers. ECR referrer reads use
`max-results=50` and reject any remaining pagination token. An OCI index is not
a release subject.

The production application source allowlist is:

| Evidence | Canonical value |
|---|---|
| app HTML S3 VersionId | `I1qOb7Kwl.pMg71wqFxbHnbbTqMWjQcY` |
| app HTML SHA-256 | `46f0079783cde24b066c7823b7d6672bad12b33debf933a4d7a7ff04b7a3b067` |
| Vault manifest SHA-256 | `15663a838b1bd648443949244c02e66ccfd6cb7b684390baeb1a86efcdd6d4a2` |
| build_inputs SHA-256 | `1ca6f0213155d8d4dbef4220f641dbb38310fe79473f6c013ef4e54dfa6a87e2` |

The old `ec1b…`, `7a13…`, and `716ac…` values are rollback/test evidence only;
they must not be restored as the production canonical source.

### Activation and merge order

1. Merge this CodeBuild/provenance/IAM/ECR change first. Keep all three
   contracts at `release.ready=false`.
2. Merge Boyle's Docker/core/media implementation separately. Do not copy its
   Dockerfile or lock/package changes into this commit. The MCP Dockerfile must
   implement every machine-readable contract build argument, meaningful use,
   OCI label, and receipt entry. Builder and runtime images must use exact
   arm64 child digests; Wolfi package versions and installed binary hashes must
   be exact. Mutable tags are never evidence.
3. Run the full contract and actual-image tests. Only after the actual
   Chainguard/Wolfi candidate is CRITICAL=0/HIGH=0 and all binary/app/model/
   commit evidence matches may a reviewed follow-up set `release.ready=true`.
4. Keep Aristotle-owned task definitions unchanged until a fresh active or
   rollback receipt exists for the exact release digest.

This ordering is fail closed: the publisher, builders, attestor, release
launcher, and Terraform hard precondition all reject `release.ready=false`.

OpenClaw uses a dedicated full-40-character-`dev`-SHA project/role and never an
MCP repository or legacy/shared image-only path. Its build role cannot write or
sign S3 evidence. The publisher writes KMS-signed, versioned,
COMPLIANCE-locked source statements before the build; the build role verifies
them. The deployed `9cde4c…` object is an OCI image index and ECR basic scan
returns `UnsupportedImageType`, so it is ineligible. The promoted subject must
be a single ECR-scan-capable `linux/arm64` manifest, or carry the stronger
signed Trivy actual-image C/H/secret=0 evidence enforced here.

### One-time Terraform migration while release.ready is false

These are future, separately authorized AWS steps. They were not run as part
of this remediation.

1. Rotate/delete root keys first. Any AWS root access keys are an
   external account-level blocker because IAM cannot constrain the root principal. Keep
   only the normal break-glass root login.
2. Start from a clean reviewed `origin/dev`, back up state, and inspect existing
   ownership before import:

   ```bash
   cd infra/terraform
   terraform init
   terraform state pull > /secure/local/path/teamagent-before-provenance.tfstate
   terraform state list
   ```

   Do not print or commit state. Import only addresses absent from state.
   Existing release repositories and the existing main builder must be adopted,
   never recreated:

   ```bash
   terraform import aws_cloudwatch_log_group.codebuild_image /aws/codebuild/teamagent-dev-image-builder
   terraform import aws_cloudwatch_log_group.codebuild_aiia_image_legacy /aws/codebuild/aiia-image-builder
   terraform import aws_ecr_repository.mcp teamagent-mcp
   terraform import aws_ecr_repository.openclaw teamagent-openclaw
   terraform import aws_ecr_repository.openclaw_media teamagent-openclaw-media
   terraform import 'aws_ecr_repository.tiktok_acquire[0]' teamagent-dev-tiktok-acquire
   terraform import aws_codebuild_project.image teamagent-dev-image-builder
   terraform import aws_iam_role.codebuild teamagent-dev-codebuild-image
   terraform import aws_iam_role_policy.codebuild teamagent-dev-codebuild-image:teamagent-dev-codebuild-image
   ```

3. A normal full plan is expected to fail while live image variables are set
   and `release.ready=false`. Do not bypass the gate by clearing live image
   variables or by changing `release.ready`. For the one-time infrastructure
   bootstrap only, build a saved targeted plan from the audited address file.
   Exclude lifecycle policies on the first pass:

   ```bash
   target_args=()
   while IFS= read -r address; do target_args+=("-target=$address"); done \
     < <(sed '/^#/d;/^$/d;/^aws_ecr_lifecycle_policy\\./d' codebuild_provenance_bootstrap_targets.txt)
   terraform plan "${target_args[@]}" -out=provenance-bootstrap.tfplan
   terraform show provenance-bootstrap.tfplan
   terraform apply provenance-bootstrap.tfplan
   ```

   The saved plan must contain no ECS task definition, ECS service,
   EventBridge rule/target, schedule, production image variable change, delete,
   or replacement. Apply only that reviewed saved plan. The target file
   intentionally excludes `terraform_data.production_image_release_gate`;
   this is a one-time migration exception for provenance infrastructure, not a
   deployment exception.

4. The two GitHub CodeConnections are created in `PENDING`. Complete the GitHub
   App handshake for `teamagent-dev-openclaw-codebuild` and
   `teamagent-dev-tiktok-codebuild`, then verify both are `AVAILABLE`.
   The MCP publisher reuses the TeamAgent/OpenClaw connection. Do not start a
   build before this manual handshake.
5. Preview lifecycle deletion before applying lifecycle resources. Obtain all
   current `active-*` and `rollback-*` release digests without logging image
   contents, run ECR lifecycle preview for each release repository, then run:

   ```bash
   python3 ../codebuild/release_evidence.py verify-lifecycle-preview \
     --preview /secure/local/path/release-lifecycle-preview.json \
     --protected-digest sha256:<active-or-rollback-digest>
   ```

   The verifier rejects truncated previews and any protected digest selected
   for expiry. Then create and review a second saved plan containing only the
   `aws_ecr_lifecycle_policy.*` addresses from the target file. Quarantine
   expires after 2 days, verified candidates after 30 days, and release
   repositories expire only untagged artifacts after 365 days; active/rollback
   tags live only in release repositories:

   ```bash
   lifecycle_args=()
   while IFS= read -r address; do lifecycle_args+=("-target=$address"); done \
     < <(sed -n '/^aws_ecr_lifecycle_policy\\./p' codebuild_provenance_bootstrap_targets.txt)
   terraform plan "${lifecycle_args[@]}" -out=provenance-lifecycle.tfplan
   terraform show provenance-lifecycle.tfplan
   terraform apply provenance-lifecycle.tfplan
   ```

### Build, release authorization, and signed-digest deploy

After contracts are ready and both connections are available:

1. Run exactly one build-only launcher from clean remote HEAD:
   `build_teamagent_image.sh`, `build_openclaw_image.sh`, or
   `build_tiktok_image.sh`. Each assumes its dedicated role once, pins that
   session, and ends at a verified-candidate digest plus immutable receipt.
2. Use `authorize_image_release.sh` with the exact candidate receipt key and
   both S3 VersionIds before the signed 30-day candidate window expires. It
   rechecks the candidate manifest/referrers and all cosign signatures, issues
   a fresh short-lived active/rollback receipt, and invokes the source-free
   promoter. It does not run Terraform.
3. Set the applicable production image variable to the fixed release
   repository `@sha256:<digest>` and set `image_release_evidence` to the exact
   receipt/signature keys and VersionIds. A full `terraform plan` invokes the
   hard precondition, re-downloads those exact COMPLIANCE-locked versions,
   verifies the KMS signature, freshness, contract hash, channel, repository,
   digest, single-manifest release presence, and exact SBOM/provenance/signature
   referrers for every receipt subject. It fails on any tag/string, partial
   bundle promotion, pagination token, or mismatch.
4. Review the full plan and apply that saved plan as a separate deployment
   authorization. None of the build/release launchers updates ECS,
   EventBridge, task definitions, services, or schedules.

Do not use `-target` for a task definition, service, schedule, or other runtime
resource. Terraform resource targeting can omit the standalone
`terraform_data.production_image_release_gate` from the selected graph. Until
the Aristotle-owned production task definitions directly depend on that gate,
or an enforced Terraform policy rejects every runtime-targeted plan, any such
targeted plan/apply is a deployment NO-GO. The one-time target list above is
limited to provenance infrastructure and is not authorization to target a
runtime resource.

CodeBuild log groups, including the legacy `aiia-image-builder` group, use
30-day retention. The legacy resource is retention-only and does not recreate
its retired project. Shell tracing stays disabled; ECR tokens go only to
fixed-registry `--password-stdin` logins, and temporary AWS credentials,
signature bytes, and secret values are never printed.

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
