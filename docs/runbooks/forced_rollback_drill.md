# forced-rollback drill Runbook

## 1. 判定

このRunbookは、初回リリース後の `new` から保存済みTerraform planで `old` へ
戻し、別の保存済みplanで `new` へ復帰するStage C drillを扱う。対象はAWS
account `718959508629`、region `ap-northeast-1`、environment `dev`、
pipeline `mcp` である。

> **判定: 実走可能（前提条件付き）。ただし現在の実走開始判定はNO-GO。**
>
> trust、両legのDM QA、PASSED aggregateのschema・S3保存・KMS署名・exact
> VersionId再取得は実装済みである。一方、`finalize` の単一callerがlive
> snapshotとdrill署名を両立できないため、現HEADのままでは `PASSED` まで
> 完走できない。下表のNO-GOが修正・配備・独立確認されるまでは、本番の
> `prepare` 以降を開始しない。

### GO / NO-GOチェック

| 項目 | 判定 | 実読結果 |
|---|---|---|
| runtime automation trust | GO | `AIIAdev`、MFA、固定session name `teamagent-terraform-worker` の `sts:AssumeRole`。`sts:SetSourceIdentity` 条件なし (`infra/terraform/runtime_evidence.tf:116`) |
| subcommand間のcaller切替 | **NO-GO** | `AIIAdev` からruntime automation roleへ入る経路は復旧した。しかし `finalize` は同一processでguard snapshot (`infra/deploy/forced_rollback_drill.sh:3206`) とdrill鍵による署名を含むartifact/aggregate persistence (`infra/deploy/forced_rollback_drill.sh:3256`, `:3268`) を行う。runtime automation boundaryはdrill鍵への `kms:Sign` を明示Deny (`infra/terraform/runtime_evidence.tf:1230`) し、鍵policyはdrill evidence roleだけを許可 (`infra/terraform/forced_rollback_drill.tf:63`) する。一方、そのdrill roleのidentity policyはS3/KMS証拠操作だけで、ECS等のsnapshot読取権限がない (`infra/terraform/forced_rollback_drill.tf:310`)。有効な単一callerがない |
| task definition不変legのDM QA | GO | controllerは両legの `apply` にdeadlineを必ず渡し、guardはtask definitionの変更有無にかかわらずDM QAを実行する。失敗は24、timeoutは124 (`infra/deploy/terraform_runtime_guard.sh:12740`) |
| PASSED aggregate | GO | builder/storeがvalidatorのexact schema用artifactを作り、source artifactとaggregateをS3 Object Lockへ保存、KMS署名、返却VersionId指定で再取得、bytes一致、KMS Verifyを行う (`infra/deploy/forced_rollback_drill.sh:2548`, `:2563`, `:2590`) |
| FAILED aggregate | **NO-GO（回復証拠のみ）** | controllerは `RECOVERY_REQUIRED` を `finalize` の入力として受け付ける (`infra/deploy/forced_rollback_drill.sh:3167`) が、その経路ではterminal snapshotを作らない。一方builderはterminal snapshotと両legのcomplete artifactを必須とする (`infra/codebuild/forced_rollback_drill_aggregate_builder.py:155`, `:181`)。失敗drillをcontrollerだけで `FINALIZED` にできない |

PASSED経路をGOにする修正条件は、`finalize` のsnapshotと署名を、権限分離を
維持した公開entrypointで実行できること、かつ実AWSへ配備済みであること。
FAILED経路も正式に使うなら、`RECOVERY_REQUIRED` 用aggregate契約をcontroller、
builder、validatorで一致させること。IAM拡張、内部helperの直実行、既存artifactの
手編集で迂回してはならない。

## 2. 本番配備済みの前提

2026-07-29の本番確認では、drill基盤、承認基盤
`teamagent-dev-approval-publisher`、trust修復、次のRSA署名鍵2本を含む対象全件は
配備済みで、実AWSの両鍵は `KeyState=Enabled` である。

- `alias/teamagent-dev-mcp-approval`
- `alias/teamagent-dev-forced-rollback-drill-signing`

通常のdrill前に基盤applyは行わない。再配備が必要な場合もfull planは正規経路
ではない。`infra/terraform/worker.tf:201` のpreconditionはHMAC rollout有効を
要求するが、本番の `mail_action_hmac_rollout_phase` は既定の `"blocked"` で
あるためfull planは失敗する。配備担当がreviewした固定targetだけを
`-target` でplan/applyするのが現行の正規手順であり、drill中に対象を追加したり
plain `terraform apply` へ切り替えたりしない。

KMS key作成直後のread-backが `kms:GetKeyRotationStatus` のAccessDeniedで失敗
した場合、鍵作成失敗ではなく管理callerのread権限不足を疑う。この権限は
`infra/terraform/forced_rollback_drill.tf:45` と
`infra/terraform/runtime_evidence.tf:906` で修正済みである。鍵を作り直さない。

## 3. 時間制約

> **期限1: 初回リリース検証時刻から30分以内にrollback applyを開始する。**
>
> `prepare` やplanの完了ではない。`apply-leg --leg rollback-to-previous` が
> exact承認を消費し、guard apply直前のdeadline判定を通る必要がある。

> **期限2: rollback apply開始から20分未満でrestore applyを完了する。**
>
> controllerはrollback承認消費時から1,200秒のold-dwell timerを開始する。
> restoreのplan、apply、DM QA、最終guard処理をすべて含む。deadline超過は
> `RECOVERY_REQUIRED` であり、同じdrillを再開できない。

各applyのDM QAは最大300秒を使い、回復用30秒をdeadline前に予約する。最初から
30分ぎりぎりで開始しない。rollback承認前にrestore担当、incident担当、作業記録、
連絡経路を待機させる。

## 4. 認証とcaller

MFA sessionは1時間で失効する。**以下の各フェーズ冒頭で毎回**
`source ~/sts.sh` を実行し、古い認証情報を使い回さない。

```bash
source ~/sts.sh
aws sts get-caller-identity --query '[Account,Arn]' --output text
```

期待する起点は
`718959508629  arn:aws:iam::718959508629:user/AIIAdev`。異なる場合は停止する。
runtime automation roleへ入るフェーズでは、fresh MFA sessionの直後に次を実行
する。`--source-identity` は付けない。

```bash
read -r AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN < <(
  aws sts assume-role \
    --region ap-northeast-1 \
    --role-arn arn:aws:iam::718959508629:role/teamagent-dev-terraform-runtime-automation \
    --role-session-name teamagent-terraform-worker \
    --duration-seconds 3600 \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
    --output text
)
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
unset AWS_PROFILE AWS_DEFAULT_PROFILE
aws sts get-caller-identity --query '[Account,Arn]' --output text
```

期待ARNは
`arn:aws:sts::718959508629:assumed-role/teamagent-dev-terraform-runtime-automation/teamagent-terraform-worker`。

| controller subcommand | 実行時caller |
|---|---|
| `prepare` | controller自身はexact callerを検査しない。S3 input取得を含む準備はfresh `AIIAdev` MFA sessionで行う |
| `preflight` | exact runtime automation session |
| `plan-leg` | `AIIAdev`。内部のrelease authorizerが固定release launcherへassumeし、終了後のguard planは親shellの `AIIAdev` credentialsを使う |
| `apply-leg` | exact runtime automation session |
| `finalize` | 現在は有効な単一callerなし。NO-GO解消後の公開手順に従う |

`plan-leg` はcleanなlocal `dev`、allowlist済みorigin、`HEAD == origin/dev` を
要求し、自身で `git fetch` する。dirty worktree、別branch、offline状態では
fail-closedする。

## 5. 承認発行と入力の準備

### 5.1 承認publisherは手動起動

承認publisher用ランチャーは存在しない。CodeBuild projectのdefault
`source_version` は意図的に使えない40桁ゼロSHAである。起動時に
`--source-version refs/heads/dev` を必ず指定する。省略するとsource解決に失敗
するのが正しいfail-closed動作である。

起動callerはMFA付き `AIIAdev` から固定session name
`teamagent-approval-caller` でassumeしたapproval caller role。override可能な
環境変数は次の3個だけである。

- `APPROVAL_DECISION`: `APPROVED: ` で始まるreview済み判断
- `EXPECTED_COMMIT`: remote `dev` と一致する40桁lowercase commit
- `FORCED_ROLLBACK_EVIDENCE_JSON`: schema version 1のreview済み
  `PASSED` または有効期限内 `PROVISIONAL_INITIAL_RELEASE` gate

fresh MFA sessionで次を行う。`$APPROVAL_ENV_FILE` はowner-only directory内の
未使用pathとする。

```bash
source ~/sts.sh
read -r AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN < <(
  aws sts assume-role \
    --region ap-northeast-1 \
    --role-arn arn:aws:iam::718959508629:role/teamagent-dev-approval-caller \
    --role-session-name teamagent-approval-caller \
    --duration-seconds 3600 \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
    --output text
)
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
unset AWS_PROFILE AWS_DEFAULT_PROFILE
```

```bash
jq -n \
  --arg decision "$APPROVAL_DECISION" \
  --arg commit "$EXPECTED_COMMIT" \
  --arg gate "$FORCED_ROLLBACK_EVIDENCE_JSON" '[
    {name:"APPROVAL_DECISION",value:$decision,type:"PLAINTEXT"},
    {name:"EXPECTED_COMMIT",value:$commit,type:"PLAINTEXT"},
    {name:"FORCED_ROLLBACK_EVIDENCE_JSON",value:$gate,type:"PLAINTEXT"}
  ]' > "$APPROVAL_ENV_FILE"
chmod 600 "$APPROVAL_ENV_FILE"

BUILD_ID="$(
  aws codebuild start-build \
    --region ap-northeast-1 \
    --project-name teamagent-dev-approval-publisher \
    --source-version refs/heads/dev \
    --environment-variables-override "file://$APPROVAL_ENV_FILE" \
    --query 'build.id' \
    --output text
)"
aws codebuild batch-get-builds \
  --region ap-northeast-1 \
  --ids "$BUILD_ID" \
  --query 'builds[0].[buildStatus,resolvedSourceVersion]' \
  --output text
```

`SUCCEEDED` とexact commitを確認し、build logが出力するapproval payloadと
signatureのbucket、key、VersionId、SHA-256を契約へ転記する。approvalは発行から
1時間で失効する。値を推測したり、失敗buildのlocatorを使ったりしない。

### 5.2 drill契約

`infra/deploy/forced_rollback_drills/EXAMPLE.json` をowner-only locationへcopyし、
全placeholderを実AWSの値へ置換する。exact key以外、duplicate key、symlink、
group/other書込み可能fileは拒否される。

- `ready=true`、`blocked_reason=""`、新しいUUIDv4 `drill_id`、実行commitを設定。
- limitsは `max_start_delay_seconds=1800`、
  `max_old_dwell_seconds=1200`。pipelineは `mcp`。
- `targets.old/new` はreview済みのexact image、subject、resource、activation、
  task definition ARN、別々のpreflight/runtime migration IDを記録。
- old/new candidateのreceipt key、payload VersionId、signature VersionIdは
  同じ組を使わない。
- `targets.*.approval` は手動publisherの実payload/signature locatorを記録。
- consumer manifestはcanonical local pathと実SHA-256、guard receiptsは
  alarm delivery、alarm migration、log readiness、versioningの実path/SHA。
  `media_cutover` は `null`。
- `evidence` はtemplateのexact shapeを保つ。pre-finalizeでは
  `artifact_manifest=[]`、`signature={}`、`immutable_object={}` が正しい。
  `kms_key_arn` だけ実AWSのdrill signing key ARNへ置換する。artifact locatorと
  integrity実測値は `finalize` が生成するため、契約へ手入力しない。
- initial release locatorはpayload/signatureのexact VersionId、SHA-256、
  size、content type、SSE-KMS、Object Lock、retain-until、KMS Verify、
  exact-version再取得結果を実測で記録。

old/newは同じsubject/resource identity集合で、各changed subject digestは異なる。
task definitionは同じfamilyかつold revisionよりnew revisionが大きい。
`openclaw` imageはold/new同一、`x_buzz` imageは `mcp` と同一である。

### 5.3 local pathと初回apply receipt

```bash
export DRILL_CONTRACT="/secure/forced-rollback/contract.json"
export INITIAL_APPLY_RECEIPT="/secure/forced-rollback/initial-release.apply.json"
export TFVARS="/secure/forced-rollback/terraform.tfvars.json"
export DRILL_DIR="/secure/forced-rollback/drills/<drill-id>"
```

fresh `AIIAdev` MFA sessionで、契約のexact VersionIdを未使用pathへ取得する。

```bash
source ~/sts.sh
aws s3api get-object \
  --region ap-northeast-1 \
  --bucket "$(jq -r '.control.initial_release_apply_locator.bucket' "$DRILL_CONTRACT")" \
  --key "$(jq -r '.control.initial_release_apply_locator.key' "$DRILL_CONTRACT")" \
  --version-id "$(jq -r '.control.initial_release_apply_locator.version_id' "$DRILL_CONTRACT")" \
  "$INITIAL_APPLY_RECEIPT"
```

`DRILL_DIR` は未存在、その親だけがowner-onlyで存在すること。別Terraform、
ECS更新、release promotionが走っていないことを確認する。

## 6. 実行順序

現在は§1のNO-GO解消まで開始しない。解消後も次の公開controllerだけを順番どおり
使う。guardはAWS全inventoryを全page実査するため、**10〜20分出力がないことが
ある。無音でも待ち、Ctrl-Cしない。** 別shellから同じplanを実行しない。

### Phase 1: prepare

```bash
source ~/sts.sh
bash infra/deploy/forced_rollback_drill.sh prepare \
  --contract "$DRILL_CONTRACT" \
  --initial-apply-receipt "$INITIAL_APPLY_RECEIPT" \
  --var-file "$TFVARS" \
  --out-dir "$DRILL_DIR"
jq -r '.state' "$DRILL_DIR/state.json"
```

期待stateは `PREPARED`。このcommandはAWS runtimeを変更しない。

### Phase 2: old/new preflight

fresh MFA sessionからruntime automation roleへ入り、両targetをexact順で実行する。

```bash
source ~/sts.sh
# §4のruntime automation assume手順を実行
bash infra/deploy/forced_rollback_drill.sh preflight \
  --drill-dir "$DRILL_DIR" \
  --targets old,new
jq -r '.state' "$DRILL_DIR/state.json"
```

期待stateは `PREFLIGHTED`。一時Fargate taskとOpenClaw用一時EFS/IAMを作成して
後始末する。無音時も中断しない。

### Phase 3: rollback plan

fresh `AIIAdev` MFA sessionへ戻す。runtime automation credentialsを残さない。

```bash
source ~/sts.sh
bash infra/deploy/forced_rollback_drill.sh plan-leg \
  --drill-dir "$DRILL_DIR" \
  --leg rollback-to-previous
jq -r \
  '.state, .legs.rollback_to_previous.plan.sha256' \
  "$DRILL_DIR/state.json"
```

期待stateは `LEG1_PLANNED`。表示されたplan SHAを作業記録へ転記する。

### Phase 4: rollback apply / OK-1

初回リリース検証から30分以内であることを再確認する。fresh MFA sessionから
runtime automation roleへ入り、次を実行する。

```bash
source ~/sts.sh
# §4のruntime automation assume手順を実行
bash infra/deploy/forced_rollback_drill.sh apply-leg \
  --drill-dir "$DRILL_DIR" \
  --leg rollback-to-previous
```

表示された次の1行をdrill ID、action、plan SHAと照合し、完全一致した場合だけ
そのまま入力する。`OK-1` や `yes` だけでは承認にならない。

```text
APPROVE <drill-id> rollback <plan_sha256>
```

期待stateは `LEG1_APPLIED`。承認消費時から20分timerが始まる。DM QAは
OpenClaw task definition不変でも無条件実行される。失敗exit 24、timeout
exit 124はいずれも `RECOVERY_REQUIRED`。

### Phase 5: restore plan

直ちにfresh `AIIAdev` MFA sessionへ戻して実行する。

```bash
source ~/sts.sh
bash infra/deploy/forced_rollback_drill.sh plan-leg \
  --drill-dir "$DRILL_DIR" \
  --leg restore-active
jq -r \
  '.state, .legs.restore_active.plan.sha256, .old_dwell.deadline_epoch' \
  "$DRILL_DIR/state.json"
```

期待stateは `LEG2_PLANNED`。restore plan SHAがrollback plan SHAと異なることを
確認する。

### Phase 6: restore apply / OK-2

deadline前にfresh MFA sessionからruntime automation roleへ入り実行する。

```bash
source ~/sts.sh
# §4のruntime automation assume手順を実行
bash infra/deploy/forced_rollback_drill.sh apply-leg \
  --drill-dir "$DRILL_DIR" \
  --leg restore-active
```

表示値を照合し、完全一致した次の1行だけを入力する。

```text
APPROVE <drill-id> restore <plan_sha256>
```

期待stateは `LEG2_APPLIED`。rollback開始からrestore完了までが1,200秒未満で
なければならない。

### Phase 7: finalize

公開されたcaller分離修正が配備され、§1が全GOになった場合だけ実行する。
実際の引数は次だけで、caller切替用の隠しoptionは存在しない。

```bash
source ~/sts.sh
# 修正後に承認されたcaller手順を実行
bash infra/deploy/forced_rollback_drill.sh finalize \
  --drill-dir "$DRILL_DIR"
```

成功条件は出力 `PASSED aggregate_sha256=<64桁>`、state `FINALIZED`、
`final_status=PASSED`。finalizeはfresh terminal snapshotをinitial newと照合し、
各source artifactをCOMPLIANCE retentionで保存・署名・exact VersionId再取得後、
aggregate本体も同様に保存・署名・再検証する。

## 7. backend local cacheの復旧

guardが次で停止した場合、remote stateではなく
`infra/terraform/.terraform/terraform.tfstate` のlocal backend metadata破損を
疑う。

```text
Failed to load state: invalid syntax: no format version number
```

同時Terraformがないことを確認し、対象fileを削除せず退避して
`init -reconfigure` する。fresh MFA sessionと、そのphaseが要求するcallerで行う。

```bash
cd infra/terraform
mv .terraform/terraform.tfstate \
  ".terraform/terraform.tfstate.invalid.$(date +%Y%m%d%H%M%S)"
terraform init -reconfigure
cd ../..
```

復旧後もcontroller stateが `RECOVERY_REQUIRED` なら同じcommandを再実行しない。
backend復旧はcontrollerのstate遷移やone-use planを巻き戻さない。

## 8. 中断・失敗時

最初にstate、failure、deadlineを保存する。

```bash
jq '{state,failures,old_dwell,legs,final_status}' "$DRILL_DIR/state.json"
```

fresh MFA sessionからruntime automation roleへ入り、read-only snapshotを取得する。
これも全inventory実査で10〜20分無音になり得るためCtrl-Cしない。

```bash
source ~/sts.sh
# §4のruntime automation assume手順を実行
bash infra/deploy/terraform_runtime_guard.sh snapshot
```

| state | できること | してはいけないこと |
|---|---|---|
| `PREPARED` | initial new exact一致をsnapshotで確認し中止 | preflight artifactの手作成 |
| `PREFLIGHTED` | initial newを確認し中止、または期限内に正規rollback planへ進む | 別drillへのreceipt流用 |
| `LEG1_PLANNED` | 未applyならinitial newを確認し中止、または期限内にOK-1 | plan再生成、別commandでapply |
| `LEG1_APPLIED` | exact oldかつ20分deadline内なら正規restore planへ直行 | oldで待機、manual ECS更新 |
| `LEG2_PLANNED` | exact oldかつdeadline内なら正規OK-2 | rollback planの再apply |
| `LEG2_APPLIED` | initial new exact一致を確認。§1全GO後だけfinalize | aggregate手編集 |
| `RECOVERY_REQUIRED` | evidence保全、read-only snapshot、incident引継ぎ | `plan-leg` / `apply-leg` / retry / resume / force |
| `FINALIZED` | aggregateとAWS locatorを監査 | stateやartifactの変更 |

controllerに `recover`、`resume`、`retry`、`force` subcommandはない。
`RECOVERY_REQUIRED` からplan/applyへ戻る遷移もない。guard apply途中の失敗では
cleanupがbaseline復元を試みるが、成功を推測せず、task revision、desired count、
rule state、dispatcher、Terraform lineage/serialを独立照合する。

現HEADでは `RECOVERY_REQUIRED` の `finalize` も§1記載のbuilder契約不一致で完成
しない。実行してartifactを部分保存させず、state、receipts、CloudTrail、snapshot
をincident担当へ渡す。修正後もfailureが1件でもあるdrillはPASSEDへ昇格させない。
原因修正後は新しいdrill ID、契約、out-dir、authorization、plan、approval、
apply attempt IDで最初から行う。

`.controller.lock` を手で削除しない。
`drill is locked or an interrupted command requires reconciliation` は別processまたは
中断commandの照合が必要という意味である。

## 9. 証拠監査

正規保存先はbucket `teamagent-dev-openclaw-rollout-evidence`、prefix
`forced-rollback-drills/`、Object Lock `COMPLIANCE`、retention 3,650日以上、
signing algorithm `RSASSA_PSS_SHA_256`。

local結果を確認する。

```bash
jq '{state,final_status,aggregate_sha256,failures}' "$DRILL_DIR/state.json"
```

AWSはdelete markerではなくexact VersionIdで監査する。

```bash
aws s3api list-object-versions \
  --region ap-northeast-1 \
  --bucket teamagent-dev-openclaw-rollout-evidence \
  --prefix "forced-rollback-drills/<drill-id>/" \
  --output table
```

契約またはaggregateのlocatorにあるexact key/VersionIdを取得し、SHA-256、size、
content type、SSE-KMS ARN、Object Lock mode/retain-until、signature key、
signer key/algorithm、KMS Verify、同じVersionIdの再取得bytes一致を照合する。
`verified=true`、`bytes_match=true`、VersionId、SHAを手入力で補わない。

## 10. 禁止事項

- guardが無音でもCtrl-Cしない。
- `aws ecs update-service`、生の `terraform apply`、保存planの手動apply、
  Terraform state編集をしない。
- 基盤再配備でfull planへ切り替えない。review外の `-target` を追加しない。
- `--restore-and-verify` を意図的なrollback/restore legとして使わない。
- approval publisher起動時の `--source-version refs/heads/dev` を省略しない。
- `OK-1` / `OK-2`、authorization、plan、receipt、apply attempt IDを再利用しない。
- IAM拡張、validator無効化、schema/status/locatorの手編集、内部helper直実行で
  NO-GOを迂回しない。
