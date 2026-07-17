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

### 初期化

```bash
cd infra/terraform

# 1. AWS 認証
aws configure  # or export AWS_PROFILE=...

# 2. 変数ファイル準備
cp terraform.tfvars.example terraform.tfvars
vi terraform.tfvars

# 3. 初期化
terraform init
```

### 稼働中runtimeの変更・state同期

MCP / connect-web / ingest / morning-digest / canary / TikTok worker / x-buzz worker と
両workerのLambda dispatcherは、CLI直デプロイ後に
Terraform state・`terraform.tfvars` がliveより古くなることがある。runtimeを含む
plain `terraform plan` / `terraform apply` は禁止する。次のvalidatorはTeamAgent dev
（AWS account `718959508629`、東京リージョン、固定S3 backend）専用で、検証済みの
saved planを作るところまでを担当する。**apply機能はない。**

```bash
# 1. live由来のnon-secret値を表示する（AWS read-only）
bash ../deploy/terraform_runtime_guard.sh snapshot

# 2. 表示値をgitignored terraform.tfvarsへ反映する。ファイルは所有者限定にする
vi terraform.tfvars
chmod 600 terraform.tfvars

# 3. 保存先も所有者限定かつ空にする（既存plan/receiptへの上書きは禁止）
ARTIFACT_DIR="$(mktemp -d /tmp/teamagent-runtime.XXXXXX)"
chmod 700 "$ARTIFACT_DIR"

# 4a. state/configを現在のlive imageへ同期する候補plan
bash ../deploy/terraform_runtime_guard.sh plan \
  --var-file "$PWD/terraform.tfvars" \
  --out "$ARTIFACT_DIR/runtime.tfplan" \
  --runtime-sync

# 4b. または、同じECR repositoryに実在する別の完全digestを明示したrollout候補plan
bash ../deploy/terraform_runtime_guard.sh plan \
  --var-file "$PWD/terraform.tfvars" \
  --out "$ARTIFACT_DIR/runtime.tfplan" \
  --runtime-rollout-image \
  '718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mcp@sha256:<64hex>'

# 5. 人間が差分を確認
terraform show "$ARTIFACT_DIR/runtime.tfplan"

# 6. read-only再検証。plan/receipt/var-file改ざんとlive変更を検出する
bash ../deploy/terraform_runtime_guard.sh verify \
  --plan "$ARTIFACT_DIR/runtime.tfplan"

# 7. 検証器はここで終了する。適用は行わない。中止時はdirectoryごと削除する
rm -rf "$ARTIFACT_DIR"
```

guardは次をfail-closedで検査する。

- 主要5＋TikTok/x-buzz task definitionの期待container名が一意で、syncならlive digest、
  rolloutなら主要5＋x-buzzが同一account/region/repositoryにECR実在する別digestになること
- environment/secretsを順序無視でliveと完全一致させ、追加・変更・削除・重複を拒否すること
- task roles/cpu/memory/runtime platform/network/command/health/log/ports/volumes等を
  canonical比較し、image以外のcritical構成を保持すること
- ECS serviceのdesired count/network/LB/deployment/role/tags等、EventBridge targetの
  role/cluster/network/retry/input等、ruleのstate/schedule/description等をliveと比較し、
  task definition参照以外の差分を拒否すること
- TikTok/x-buzz dispatcherのrole/runtime/code hash/static env等をliveと比較し、所定task
  definition revision参照以外の変更を拒否すること。SQS event source mappingはqueue、
  function、有効状態、batch/retry/filter/concurrency/tagsを含め完全不変にすること
- ECS serviceが単一PRIMARYかつrollout `COMPLETED`であること
- resource/action/schema/check/deferred action/action invocation/unknown値とresource driftを
  allowlistで検証し、対象task definitionのcreate-before-destroy以外のdelete/createと
  runtime外変更を拒否すること
- 0700 private stagingでplanを生成し、SHA→`terraform show`→検証→SHA再照合後に
  0600でatomic publishすること。receiptはplan/var-file path・各SHA・live fingerprint・
  desired image・runtime guardに束縛すること
- verify中もprivate copyを使い、前後のlive snapshotと全SHAを再照合すること

`runtime_guard_live` はscriptがliveから一時注入する値であり、tfvarsへ書かない。
Terraform resource precondition、provider `allowed_account_ids`、dev/region/project validationも
誤操作を止める補助線だが、`runtime_guard_live`は自己申告値でありIAM境界ではない。

#### 脅威モデルと限界

このvalidatorの目的は、善意の運用者による古いtfvars/state、対象指定ミス、plan取り違え、
通常のファイル差替えを検出すること。管理者・同一OSユーザー・リポジトリやscript自体を
変更できる攻撃者への強制力、AWS IAMによるdeploy権限制御、デプロイ承認を提供しない。

全デプロイ経路が共通のdeployment lockをverify開始からapply終了まで保持していないため、
verify後に別経路がliveを変更するTOCTOUは閉じられない。このためapply subcommandを意図的に
削除した。既存の直接Terraform/CLI/CodeBuild deployは本validatorの観点ではunsafe/deprecatedで、
receiptを「適用してよい」という承認に使ってはならない。共有lock導入までは、validatorが
通ったことと安全にapplyできることを同義にしない。

また、既存の主要5 runtime＋TikTok/x-buzz worker＋dispatcherを同期/rolloutする用途だけを扱う。
resourceの初回作成、機能disable、destroy、bootstrap/create-onlyはfail-closedで対象外。
これらは別の設計・レビュー・共有lockが必要。

source/tfvarsとliveのenv/secretsが1件でも違えば、差分を黙認せずplanを残さない。承認済みの
ECS/EventBridge deploy経路でliveを意図どおりに先に整合させるか、不要なdesired設定を明示的に
source/tfvarsから除く変更を別レビューし、その後 `snapshot` → tfvars更新 → planをやり直す。
保存plan/receiptはstate由来の機微情報を含み得るためGitへ追加しない。

### runtime以外

runtimeを含まない変更もplain applyは避け、対象を限定した保存planをレビューしてから
適用する。runtime resourceが依存に入る場合は上記guardを使う。

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
