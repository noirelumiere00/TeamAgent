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

# 3. 初期化と確認（production image を含まない変更のみ）
terraform init
terraform plan

# 4. 適用（production image deploy は後述の guarded saved-plan flow のみ）
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
| app HTML S3 VersionId | `FTXbcN70D0DCN90TI_hRK1IdQK_HhLee` |
| app HTML SHA-256 | `03f8e8cc0adbc397cc636e30fcc8baaffeb1c53502cf74baf1031399cceb391c` |
| Vault manifest SHA-256 | `aa451e744d26e9dc13c170b019307b0eb10d3645267960fbff41c4038e9b909e` |
| build_inputs SHA-256 | `6697acf311f0c9a96b41426e81ae05ad221482a6e6f69799281ad3532c2e78bf` |

The old `46f007…` / `I1qOb7…` set, along with `ec1b…`, `7a13…`, and
`716ac…`, is rollback/test evidence only; none may be restored as the
production canonical source.

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
4. Merge the direct release-gate dependency on every ECS task definition while
   preserving the current production image/application anchors. Do not change
   a production image until a fresh active or rollback receipt exists for its
   exact release digest.

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

1. Do not rotate, delete, or reduce the current root/admin/access-key or
   long-lived administrator permissions as part of this release work. The
   management-terminal/MFA control is the accepted administrative boundary.
   Administrators can technically bypass this automation, so the guarded
   release path is an audited operating control, not an IAM claim that root or
   administrators are unable to perform direct AWS changes.
2. Start from a clean reviewed `origin/dev`, back up state, and inspect existing
   ownership before import:

   ```bash
   cd infra/terraform
   terraform init
   terraform state pull > /secure/local/path/teamagent-before-provenance.tfstate
   terraform state list
   ```

   Do not print or commit state. Import only addresses absent from state.
   Imports belong to the separately reviewed Terraform migration worker, which
   must checkpoint each successful address and support resume after partial
   success. The image-release planner below rejects every plan containing an
   import and is not an import-resume mechanism.
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
   bootstrap only, build a saved targeted plan from the audited address file:

   ```bash
   target_args=()
   while IFS= read -r address; do target_args+=("-target=$address"); done \
     < <(sed '/^#/d;/^$/d' codebuild_provenance_bootstrap_targets.txt)
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
5. Quarantine repositories expire rejected candidates after 2 days.
   Verified-candidate lifecycle rules can match only explicit noncanonical
   `rejected-*` tags; canonical `verified-*` subjects and their untagged
   recursive referrers cannot match. Production release repositories have no
   ECR lifecycle policy. A deployed immutable candidate therefore remains
   available for fresh rollback re-attestation after 30 days.
   Candidate/source receipts use 3650-day COMPLIANCE retention;
   active/rollback authorization receipts remain short-lived. Broadening a
   verified-candidate lifecycle selector or adding a release-repository policy
   is a release-safety change and requires a fail-closed preview against every
   digest returned by the recursive signed-release graph validator.

### Independently authorized embedded-contract update stage

The broad bootstrap target file above is a one-time migration mechanism and
must not be reused for routine contract changes. After bootstrap, a new
`release.ready` value or any other changed embedded release-contract byte is
installed through the separate control-plane caller and role:

```bash
bash infra/terraform/update_image_release_controls.sh plan \
  /secure/local/path/release-controls.tfplan
terraform show /secure/local/path/release-controls.tfplan
cat /secure/local/path/release-controls.tfplan.control-update.json
bash infra/terraform/update_image_release_controls.sh apply \
  /secure/local/path/release-controls.tfplan
```

The planner targets only the five contract-consuming CodeBuild projects. Its
validator rejects create/delete/replace/import actions, runtime resources,
unknown values, and every project-field mutation except the embedded buildspec
and the main builder's two contract-hash environment values. It also requires
every consumer to contain the exact current contract hash or bytes, and binds
the saved plan, clean `origin/dev` control commit, contract hashes, changed
contracts, and changed addresses in the companion authorization file.

The dedicated caller can assume only the release-control updater role. That
role can read/update the five fixed CodeBuild projects and read/write the one
fixed Terraform state object under its existing lock; it is explicitly denied
build starts and IAM, ECR, ECS, EventBridge, Scheduler, Lambda, and evidence
mutation. This closes the contract activation cycle: trusted projects can
receive a reviewed new contract before a candidate or release receipt under
that contract exists, without granting a production deployment bypass. A
partial update is fail-closed because mismatched builders/attestors reject the
contract hash; create and review a fresh control plan to resume.

### Build, release authorization, and signed-digest deploy

After contracts are ready, both connections are available, and the Terraform
worker has provisioned the trusted
`teamagent-dev-terraform-automation/teamagent-terraform-worker` session:

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
   receipt/signature keys and VersionIds. Do not set
   `image_deployment_intent_id`; the planner creates it.
4. Store the plan outside the worktree and create it only with the guarded
   planner:

   ```bash
   bash infra/terraform/plan_image_release.sh \
     /secure/local/path/image-release.tfplan
   terraform show /secure/local/path/image-release.tfplan
   ```

   The planner rejects every inherited `TF_*` variable (including
   `TF_CLI_ARGS*`, `TF_WORKSPACE`, and `TF_DATA_DIR`), `-target`, disabled
   locking/refresh, destroy/refresh-only/import plans, caller-supplied intent
   IDs, a dirty/non-`dev` worktree, and anything other than exact fresh
   `origin/dev`. It requires `complete=true`, `errored=false`, and
   `applyable=true`, then binds the exact S3 backend key, default workspace,
   state lineage/serial, state-address ownership hash, plan-address ownership
   hash, opaque plan hash, images, contracts, immutable receipt/signature
   VersionIds, and per-receipt one-use claim IDs into the `PREPARED` intent.
   When the separately owned HMAC generation ledger is integrated, pass only
   its non-secret `{table_arn, generation, high_water_t0, stage}` snapshot in
   `image_release_shared_generation_ledger`. The exact snapshot hash is bound
   into the gate result, saved plan, `PREPARED` intent, `APPLYING` transition,
   and `CONSUMED` transition. Any changed generation, T0, stage, or table
   identity requires a fresh plan and intent.
5. After review, apply exactly that saved plan:

   ```bash
   bash infra/terraform/apply_image_release_plan.sh \
     /secure/local/path/image-release.tfplan
   ```

   The apply launcher atomically acquires the shared, leased DynamoDB
   automation lock and changes the exact intent from `PREPARED` to `APPLYING`
   with a unique apply-attempt ID. This transition burns the intent for every
   other attempt. While holding the lock, it recaptures and compares the exact backend,
   workspace, state lineage/serial, and address ownership, then keeps the lease
   alive through Terraform's own backend-locked apply. Immediately before any
   image-bearing ECS task definition, Terraform
   re-downloads the exact COMPLIANCE-locked evidence versions, rechecks
   retention and receipt expiry, verifies KMS signatures, release presence,
   single-manifest type, and the complete recursive subject/SBOM/provenance/
   signature/referrer graph. A second DynamoDB transaction then changes the
   exact same-attempt intent from `APPLYING` to `CONSUMED` and conditionally creates every exact
   receipt claim. The same plan, intent, or receipt cannot authorize another
   deployment. Every discovered ECS task definition has a direct dependency
   on this apply-time action; the guarded automation path cannot omit it with
   `-target`.

The authorization expires after one hour even if the signed receipt expires
sooner; both deadlines are rechecked when `APPLYING` starts and immediately
before `CONSUMED`. Never retry a plan after the apply attempt starts, including
after a preflight failure or nonzero Terraform exit. Reconcile
Terraform and runtime state first; the ledger records `RECONCILE_REQUIRED`.
Only confirming the same already-started attempt after an ambiguous DynamoDB
response, or repeating its outcome-recording call, is narrowly idempotent.
A retry, roll-forward, or rollback requires a fresh active/rollback
receipt, a new intent, and a new full saved plan. The ledger uses point-in-time
recovery, deletion protection, a customer-managed key, and 90-day audit TTL;
TTL is cleanup, never authorization. None of the build/release launchers
updates ECS, EventBridge, task definitions, services, or schedules.

The shared lock is cooperative with the trusted Terraform worker. It closes the
verify-to-apply race among conforming automation flows, but it does not prevent
an administrator from bypassing the scripts or changing AWS directly. Existing
administrator IAM, root/admin keys, and long-lived management permissions are
intentionally unchanged.

The HMAC worker stack owns its durable generation/high-water/stage table and
the live task/time preflight. This provenance stack neither creates that table
nor reads secrets from it; it only binds the non-secret snapshot supplied to
the guarded Terraform plan. Until the later HMAC rebase supplies and live-checks
that snapshot, an empty optional binding is not evidence that the HMAC
preflight ran. The release gate must not be described as enforcing the
separately owned live preflight on its own.

The one-time provenance bootstrap target list above remains limited to
non-runtime infrastructure and is not authorization to target a runtime
resource.

CodeBuild log groups, including the legacy `aiia-image-builder` group, are
normal operational logs and use 30-day retention. AI input/output logs use the
separately owned 60-day policy; this CodeBuild provenance change does not alter
that storage or the audit branch's S3 deletion lifecycle. The legacy resource
is retention-only and does not recreate
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
