# TeamAgent Terraform runtime operations

このstateは TeamAgent dev、AWS account `718959508629`、`ap-northeast-1` 専用です。
runtimeを含む plain Terraform、targeted apply、旧image-only script、可変
`codebuild/source.zip` builderは使用しません。操作入口は
`infra/deploy/terraform_runtime_guard.sh` だけです。適用時はexact trusted automation roleが
共有deployment lockを取得し、そのlockを直前再検証から保存planの適用完了まで保持して、
原子的apply receiptを発行します。

唯一のfirst-install例外は
`infra/deploy/bootstrap_provenance_iam.sh`です。通常guardが前提とするprovenance/IAM/
deployment-intent control planeだけを、rootが作成する一時CloudFormation seedから
1時間のSTS sessionへ移り、固定target・create/no-op限定の保存planでmain backendへ
直接作成します。update/delete/replace/import/move/drift/runtime resourceは拒否し、
main stateのlineage/serial/address引継ぎ検証後にsessionをrevokeしてseed stack/roleを
削除します。bootstrap Terraform stateは存在しないため、AWS objectの二重state ownershipは
ありません。詳細は`docs/runbooks/provenance_iam_bootstrap.md`を参照してください。

このguardは、手順に従うoperator向けのworkflow controlであり、AWSのauthorization boundary
ではありません。root、admin、既存IAM user/access keyの権限は維持し、このstateから
permissions boundaryやdeny policyを付けません。管理者はRegisterTaskDefinition、RunTask、
PassRole、plain AWS/Terraformでguardを迂回できます。これはユーザーが受容した残存リスクで、
guard/receiptを「管理者にも強制できる安全境界」とは扱いません。

## 現在のfail-closed状態

`infra/deploy/terraform_runtime_migrations.json` の2 migrationは、rollout入力が全て確定する
まで `enabled=false` です。空欄やdigest prefixから値を推測してはいけません。

現liveの既知差分は次のとおりです。

- connect-web は task definition `:53`、canaryは `:14`。connect-web imageは
  `teamagent-mcp@sha256:0f23860dc382e29d2051f3e6e415a427c853182d90ef05cce0935c3c7cecc144`。
  canary imageは環境変数だけを変えた`:13`から不変の
  `teamagent-mcp@sha256:fb44f7cdb19c7f683768fe074aa85ba3a99fdefe7b6c9e49422e46055bb458b5`。
  image内sourceの基点は
  `e4daa71986f544d66e0563879b7a4808b4e7b674` と一致する一方、OCI revisionは
  `unknown` のため、新coreの署名済みdigestとは扱いません。
- 手動gsheets ingest `:42` は完了済みです。runtime migration直前にもactive taskが
  0であることを再確認し、新規実行を止めた状態にします。
- `/app` のreview済みS3 objectは
  VersionId `FTXbcN70D0DCN90TI_hRK1IdQK_HhLee`、
  SHA-256 `03f8e8cc0adbc397cc636e30fcc8baaffeb1c53502cf74baf1031399cceb391c`、
  Vault manifest `aa451e744d26e9dc13c170b019307b0eb10d3645267960fbff41c4038e9b909e`、
  build inputs `6697acf311f0c9a96b41426e81ae05ad221482a6e6f69799281ad3532c2e78bf`
  です。guardはlatest objectをexact versionで再取得し、HTML bytesと埋込みprovenanceを
  照合するため、旧版や別publishへ暗黙に移行しません。
- 記録済みliveではingest-weekly/canary-hourlyが無効、morning-digestが有効です。ただし
  first-time versioning workflowは全EventBridge rule、Scheduler schedule、Lambda event
  source mappingを全page列挙して切断します。そのreceiptに続くruntime migrationの期待値は
  ingest/morning/canaryすべて`DISABLED`です。activation成功までは再有効化しません。
- alarm SNSはtfvarとSNS `Endpoint`のUTF-8 byte列がexact
  `s-komata@vectorinc.co.jp`でなければ受理しません。trim、lowercase、Unicode正規化は
  一切しません。canonical/legacy両topicの全protocolとPendingConfirmation/Deletedを含む
  全page inventoryを取り、両topic合計でcanonical topic上のconfirmed `email` 1件だけを
  受理します。追加endpoint、`email-json`、pending/deleted、別email、filter、raw delivery、
  canonical/legacy topicを参照するAmazon Q Developer in chat applications
  (AWS Chatbot Slack/Teams/Chime) はfail closedです。destination hashはraw email、
  exact topic、confirmed/no-filter/raw-delivery-off、Chatbot 0件のcanonical objectから
  再計算します。
- `issue-alarm-challenge`はfresh 256-bit nonceをcanonical topicへ実publishし、SNS
  `MessageId`、AWS response Date/request-id、exact subscription metadata、全publisher
  inventoryをone-use ledgerへ束縛します。受信者は
  `teamagent-dev-alarm-recipient-ack-signer`の1時間STS roleから、challengeのexact
  MessageId/nonce/raw email/topicに加え、challenge全体とinventoryのcanonical hashをKMS
  `SIGN_VERIFY` keyで署名します。automation側はKMS署名、key metadata、期限、
  MessageId/nonce/challenge hash/inventory hash、未使用ledger row、再取得した全inventoryを
  検証してから短命receiptを発行します。未ack、再利用、期限切れ、別message、
  任意64-hex文字列は証跡になりません。

`runtime_evidence.tf`のrecipient KMS alias、MFA signer role、runtime automation roleと
exact evidence policyはmain state所有です。通常full planより先に必要なため、上記の
one-time bootstrapだけがcreate可能です。KMS keyは`SIGN_VERIFY`/`ECC_NIST_P256`/
customer-managed/AWS_KMS originへ固定し、runtime automationはrootのMFA付き短期STS、
exact session name/source identityだけを信頼します。access key/login profileは作りません。
signerは従来のexact organization recipient roleを維持しつつ、root-only初期環境では
`bootstrap_runtime_session.sh sign-alarm-ack`がMFA・exact session name/source identityを
要求する別STS sessionへ移ります。このwrapper/guard経路では、rootやruntime
automationが直接KMS Signする経路はありません。
automation role自身にはCodeBuild start、ECR image write、KMS Sign、release evidence object
write、debug session、長期human credentialの明示Denyがあります。

legacy alarm topicは一括切替しません。`advance-alarm-migration`は同じshared lockとDynamoDB
ledgerの下で、次のdurable chainだけを受理します。

1. 全publisherのexact dual-publish post-state。
2. publisher 1件ごとのcanonical-only checkpoint。処理済み/未処理のmixed stateを前checkpoint
   と照合し、各checkpointに元のexact topic stateへ戻す逆rollback planを保存する。
3. 全publisher checkpoint完了後のfresh SNS challenge/KMS recipient ack。
4. 全page inventoryでlegacy参照0を確認。
5. canonical publisher集合を維持したlegacy topic退役。

各phaseはpostcondition hash、前checkpoint hash、idempotency key、AWS観測時刻、exact
publisher-topic mapを条件付きtransactionで保存します。途中失敗は既存ledger headから
idempotentに再開し、phase skip、publisher集合差替え、時刻逆転、別message receiptを
拒否します。legacy退役後だけは自動再作成せず、新しいreview済みmigrationを要求します。

## CloudTrail / Bedrock log bucket hardening

CloudTrailとBedrock invocation logsの既存bucketはS3 versioningを有効化し、bucket policyへ
TLS未使用通信の明示Denyを追加します。Object LockとMFA Deleteは設定しません。
CloudTrail監査ログにはlifecycleを設定せず、自動削除しません。

Bedrock AI入出力ログは承認済みの `bedrock_logs_retention_days = 60` に固定します。
lifecycleの対象は`bedrock/` prefixだけで、current expirationと
noncurrent-version expirationをそれぞれ60日以上に固定します。したがって同じkeyが
生成直後にoverwriteされても、どのversionも生成後60日未満では削除されません。
bucket policyは`bedrock.amazonaws.com`以外のpayload writerと手動
`DeleteObject`/`DeleteObjectVersion`を拒否し、live evidenceはexact lifecycle/policyを
再取得します。参照versionが無いexpired delete markerだけは削除します。他prefixや
CloudTrail objectはこのlifecycleの対象外で、CloudTrail監査ログには自動削除を
設定しません。

Bedrockの実配信先は
`bedrock/AWSLogs/<account>/BedrockModelInvocationLogs/*`へ固定し、
`bedrock.amazonaws.com`、exact `SourceAccount`、リージョン・アカウントを含む
`SourceArn`を要求します。KMS grantも同じ条件で`kms:GenerateDataKey`だけを許可し、
Encrypt/Decryptやwildcard actionへ広げません。

S3公式仕様では、bucketで初めてversioningを有効にした後は伝播に最大15分かかるため、
その間のobject PUT/DELETEを避けます。
https://docs.aws.amazon.com/AmazonS3/latest/userguide/manage-versioning-examples.html

稼働後に`Enabled`を観測しただけのreceipt、古いCloudTrail event history、operator記録時刻は
first-time authorizationになりません。`attest-log-versioning`はdisabled review manifest、
exact automation session、固定AWS CLI v2 bytes/endpoint、Terraform backend lockと同じ
shared workflow lockの下だけで、次を単一workflowとして実行します。

1. 全EventBridge bus/rule/target、Scheduler group/schedule、Lambda mappingを全page列挙し、
   全rule/schedule/mappingを`DISABLED`にする。全8 ECS family
   （openclaw/mcp/connect/ingest/morning/canary/TikTok/X）のRUNNING/PENDING taskを全pageで
   0にし、writer serviceのdesired/running/pendingを0、`teamagent-dev-` queueの
   visible/not-visible/delayedを0にする。CloudTrailは`StopLogging`、Bedrock loggingは
   deleteして、各disconnect response Date/request-idとexact resource集合を記録する。
2. producer-off最終観測後にbucket canonical owner ID、CreationDate、名前/ARN、
   `Unversioned`/MFA Delete disabledを再取得する。`--expected-bucket-owner`付きで直前の
   object-version集合をbaseline化してから、guard自身が両bucketへ
   `PutBucketVersioning(Status=Enabled)`を行う。成功response、request-id、AWS HTTP
   `Date`をauthorityとし、`errorCode`/`errorMessage`/`addendum`を含むresponseは拒否する。
3. 各bucketの`max(Put response Date, first-seen Enabled Date)+900`まで全producerを
   disconnectedのまま保ち、object-version集合がbaselineと同一であることを、AWS時刻が
   単調増加する2回の独立観測と最終再確認で証明する。
4. lockを再確認し、同じworkflow内でCloudTrailをexact trail/bucketへ再開し、Bedrockを
   exact bucket/prefix/データ種別へcutoverする。cutover response Date/request-id、
   producer-off契約、bucket identity、全timing、lock workflow IDをschema v4 receiptへ
   canonical hashで束縛する。さらに同じshared lock下でone-use
   `versioning-cutover#<workflow-id>` rowを条件付き保存・consistent-readし、action set、
   bucket identity、cutover、workflow claimsを1年間のdurable ledgerへ拘束する。

初期状態が両方exact `Unversioned`でない、1 controlでも未列挙/未切断、queue/taskが非0、
時計が逆転、900秒未満、bucket identity/ownerが変化、lockが変化した場合はfail closedです。
後日の観測でこのfirst-time receiptを新規作成する経路はありません。
5. cutover後のCloudTrail最新log+digest、Bedrock最新delivery、30日化する7 log groupのexport
   manifestを、0600の`teamagent-log-readiness-evidence`へ具体的なkey/version/ETag/size/timestamp
   とともに記録する。各delivery/retention content hashは別々の0600 export fileのcanonical
   path/inode/sizeへ束縛し、delivery/retention timestampはevidence observation時刻以下にする。
   guardはplan、verify、apply直前に全export fileを再hashし、path/inode/sizeも再検証する。

```bash
bash ../deploy/terraform_runtime_guard.sh attest-log-versioning \
  --out "$ARTIFACT_DIR/log-versioning.json"
```

このcommandはwriter切断、versioning有効化、CloudTrail/Bedrock cutoverというAWS writeを
行います。現在はreview manifestが`enabled=false`なので実行できません。
`terraform plan -target`や部分applyは禁止です。receipt後のnon-targeted runtime migration
full-root saved planでTLS deny、Bedrock exact delivery policy/KMS、`bedrock/`の
current/noncurrent各60日lifecycle、7 log groupのimport/30日retentionを収束させます。

```bash
# cutover後、secure evidence/export artifactを確認して作るreceiptの必須binding:
# versioning_receipt_sha256 = sha256(log-versioning.json)
```

現在はlog versioning attestation stageとruntime manifestがともに`enabled=false`です。
main-state bootstrap receipt、KMS/SSO signer/automation role、live permissionのいずれかが
未確認ならattestation、migration plan、applyはfail closedし、production writeはNO-GOです。

## Existing operational log retention

次の7つの既存log groupは削除・再作成せず、固定Terraform addressへimportして
`retention_in_days = 30`だけをin-place更新します。

- `/aws/codebuild/teamagent-dev-aiia-image-builder` →
  `aws_cloudwatch_log_group.codebuild_aiia_image_builder`
- `/aws/codebuild/teamagent-dev-image-builder` →
  `aws_cloudwatch_log_group.codebuild_image`
- `/aws/ecs/containerinsights/teamagent-dev/performance` →
  `aws_cloudwatch_log_group.ecs_containerinsights_teamagent`
- `/aws/ecs/containerinsights/teamagent-dev-tiktok/performance` →
  `aws_cloudwatch_log_group.ecs_containerinsights_tiktok`
- `/aws/lambda/teamagent-dev-reminders-notify` →
  `aws_cloudwatch_log_group.reminder_notify`
- `/aws/lambda/teamagent-dev-tiktok-acquire-dispatch` →
  `aws_cloudwatch_log_group.tiktok_dispatch`
- `/aws/lambda/teamagent-dev-x-buzz-dispatch` →
  `aws_cloudwatch_log_group.x_dispatch`

Container Insightsの2 groupはliveで確認済みの現在値1日を初期値とし、CodeBuild/Lambdaの
5 groupは現在のNever Expireを初期値とします。各resourceは`prevent_destroy`で保護し、
`kms_key_id`をignoreするため、既存のKMS関連付けを追加・解除しません。migration guardは
exact import ID、各初期retention、30日への更新、KMS不変、その他属性不変を検証します。
guardは`.terraform/terraform.tfstate`の
初期化済みbackend metadataを毎回再読取りし、credential/endpoint/workspace prefix等の
注入が無いexact S3 bucket/key/region/DynamoDB lock/encrypt設定を正規化hashで束縛します。
さらにdefault workspace、state lineage/serial、`data.` prefixとmodule/string/numeric
indexを含む全address-set hash、7 addressのremote ID所有権をreceiptへ結合します。
runtime migrationでは未importの既存group→exact import+30日update、import済みNever
ExpireまたはContainer Insights 1日→30日update、既に30日→no-opを正規状態として扱うため、
部分成功後も安全に再開できます。
provider planの`tags=null`とprovider defaultを含む`tags_all`だけを受理します。
別state/addressとのcollisionはapplyせず、state所有者を確認して専用の
`moved`/state migrationを先にレビューしてください。

この変更はlog groupを再作成しませんが、30日より古い既存eventはretention適用後に削除対象と
なり、AWS公式仕様では通常最大72時間で削除されます。最古eventが30日以内であること、または
30日超の履歴をexportしてchecksum付きで保全したことを、7 groupすべてについてreview
evidenceへ残すまではruntime migrationを有効化しません。特に現在1日のContainer Insights
2 groupも既存eventをexact S3 bucket/key/versionからfresh `O_NOFOLLOW|O_EXCL` fileへ取得し、
canonical path/device/inode/nlink=1/size/timestamps/content hashとAWS metadataをreceiptへ
拘束します。guardはsaved plan時とapply直前に同じversionを再取得・再hashし、差替え、
hardlink、古いfile、別version、観測時刻を超えるdelivery/exportを拒否します。
https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/Working-with-log-groups-and-streams.html

## 必須入力

第1段階のcandidate commitで、次を全てexact値で埋めます。この時点では
`enabled=false`、`reviewed_plan=null`のままにし、固定UUIDの
`reviewed_inputs.image_deployment_intent_id`も同じcommitへ含めます。

1. connect/ingest/morning/canaryを含む全live task definition ARNと完全image digest。
2. `0ff2ca8c…`（#255まで）とHMAC契約 `2de3b156…` の両方を含むWolfi coreの完全digest、
   40桁source commit、固定KMS key ARN。
3. 独立したOpenClaw、TikTok、x-buzzの完全digest。
4. dispatcherのlive/from code hashとreview済みdestination archive code hash、legacy alarm
   参照数、確認済みalarm配送先。
5. migration期限とallowlistがreview時点のliveに一致すること。

新coreは `cosign verify` で固定KMS key、Rekor、exact
`org.opencontainers.image.revision` annotationを検証します。さらにECR OCI configの
ARM64、UID/GID、`VOLUME /tmp`、契約labelを照合します。digestだけの差替えや
`revision=unknown` は通りません。

## 第1段階: runtime収束

作業用 `terraform.tfvars` と出力directoryは所有者限定にし、既存ファイルへ上書き
しません。

```bash
cd infra/terraform
chmod 600 terraform.tfvars
ARTIFACT_DIR="$(mktemp -d /tmp/teamagent-runtime.XXXXXX)"
chmod 700 "$ARTIFACT_DIR"

bash ../deploy/terraform_runtime_guard.sh preflight \
  --migration 2026-07-wolfi-runtime-v1 \
  --out "$ARTIFACT_DIR/preflight.json"

bash ../deploy/terraform_runtime_guard.sh review-plan \
  --var-file "$PWD/terraform.tfvars" \
  --out "$ARTIFACT_DIR/runtime-reviewed-plan.json" \
  --runtime-migration 2026-07-wolfi-runtime-v1 \
  --preflight-receipt "$ARTIFACT_DIR/preflight.json" \
  --alarm-delivery-receipt "$ARTIFACT_DIR/alarm-delivery.json" \
  --versioning-receipt "$ARTIFACT_DIR/log-versioning.json" \
  --log-readiness-receipt "$ARTIFACT_DIR/log-readiness.json" \
  --alarm-migration-receipt "$ARTIFACT_DIR/alarm-migration-final.json"

# runtime-reviewed-plan.jsonをそのままreviewed_planへ入れ、enabled=trueにする。
# candidate commitから変更してよいpathはterraform_runtime_migrations.jsonだけ。
# それ以外の入力・コード・Terraformを同時変更するとpreflight receiptは失効する。

bash ../deploy/terraform_runtime_guard.sh plan \
  --var-file "$PWD/terraform.tfvars" \
  --out "$ARTIFACT_DIR/runtime.tfplan" \
  --runtime-migration 2026-07-wolfi-runtime-v1 \
  --preflight-receipt "$ARTIFACT_DIR/preflight.json" \
  --alarm-delivery-receipt "$ARTIFACT_DIR/alarm-delivery.json" \
  --versioning-receipt "$ARTIFACT_DIR/log-versioning.json" \
  --log-readiness-receipt "$ARTIFACT_DIR/log-readiness.json" \
  --alarm-migration-receipt "$ARTIFACT_DIR/alarm-migration-final.json"

bash ../deploy/terraform_runtime_guard.sh verify \
  --plan "$ARTIFACT_DIR/runtime.tfplan"

# 実行は承認後、exact trusted automation roleだけで行う。
bash ../deploy/terraform_runtime_guard.sh apply \
  --plan "$ARTIFACT_DIR/runtime.tfplan" \
  --out "$ARTIFACT_DIR/runtime-apply.json"
```

`review-plan`はapply可能なplanやDynamoDB intentを公開せず、全resource change、
drift、output、unknown、replace pathのhashを含むexact contractだけを出力します。
final `plan`は同じ固定intent、同じpreflight時刻、同じreceipt、同じlive/stateから
再生成したcontractの完全一致を要求します。Terraformのwall-clock値はplanへ入れません。

preflightはcandidate task definitionを実際に登録し、fresh Fargate volume上でUID 10001の
`/tmp` write、browser/cache/npx/yt-dlp、OpenClaw UID 65532と暗号化EFS writeを検証して
必ず後始末します。task definitionの設定だけを根拠に成功扱いしません。

保存planはexact allowlist、create-before-destroy、env/secrets/roles/network/healthの
live parity、dispatcher completion ack、API/SNS/monitoring/retention、legacy builder退役、
administrator IAM非干渉を全て満たす場合だけ残ります。ingest/canary ruleはこの段階では
無効のままです。

guardのapplyは`teamagent-dev-terraform-runtime-automation` roleだけを受け付け、
`teamagent-tflock`上の共有lockをverify直前から保存plan適用完了まで保持します。直前にlive
drift、alarm subscription attributes/FilterPolicy、versioning、export evidence、backend
lineage/serial/所有権を再確認し、検証したprivate plan copyだけを適用します。既存管理者の
権限やaccess keyは変更しません。

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
2. Start from a clean reviewed `origin/dev`. Existing release repositories,
   evidence-bearing log groups, the existing builder, roles, and policies must
   be adopted rather than recreated. State backup, ownership discovery, and
   resumable imports belong to the same composed migration entrypoint; raw
   Terraform import, plan, target, or apply commands are not an approved
   operator path.
3. `release.ready=false` remains fail closed for build, authorization, runtime
   migration, and activation. The sole control-plane bootstrap exception is the
   fixed `bootstrap_provenance_iam.sh` target set. It accepts create/no-op only,
   writes directly to the main backend, burns a separate one-use ledger row,
   and retires its seed. It cannot update runtime or start a build/release.

4. The GitHub CodeConnections may be created in `PENDING` by the one-time
   bootstrap. Complete the GitHub
   App handshake for `teamagent-dev-openclaw-codebuild` and
   `teamagent-dev-tiktok-codebuild`, then verify both are `AVAILABLE`.
   The TikTok connection is absent (safely) when media/TikTok is disabled.
   The MCP publisher reuses the TeamAgent/OpenClaw connection. Every launcher
   rejects missing/ambiguous/paginated/PENDING connections before evidence
   writes or CodeBuild start.
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

### Embedded-contract updates

The former control-only and image-only launchers are retired. Every embedded
contract or production image change must be represented by an enabled,
time-bounded entry in `terraform_runtime_migrations.json` and flow through
`infra/deploy/terraform_runtime_guard.sh`. The guard creates one complete saved
plan, binds it to the runtime and production-provenance gates, consumes one
intent, and holds the shared deployment lock through supervised apply.

### Build, release authorization, and signed-digest deploy

After contracts are ready, required connections are available, and the
one-time bootstrap has provisioned the trusted
`teamagent-dev-terraform-runtime-automation/teamagent-terraform-worker` session:

1. From a root-only management terminal, use
   `bootstrap_provenance_session.sh` to assume the exact MFA/source-identity
   pinned launcher role, then run exactly one build-only launcher from clean
   remote HEAD:
   `build_teamagent_image.sh`, `build_openclaw_image.sh`, or
   `build_tiktok_image.sh`. Root is accepted only by STS role trust and is
   rejected as the direct build/release caller. Existing dedicated IAM callers
   remain compatible; no access key is created.
2. Use `authorize_image_release.sh` with the exact candidate receipt key and
   both S3 VersionIds before the signed 30-day candidate window expires. It
   rechecks the candidate manifest/referrers and all cosign signatures, issues
   a fresh short-lived active/rollback receipt, and invokes the source-free
   promoter. It does not run Terraform.
3. Set the applicable production image variable to the fixed release
   repository `@sha256:<digest>` and set `image_release_evidence` to the exact
   receipt/signature keys and VersionIds. Commit the one-use UUID under
   `reviewed_inputs.image_deployment_intent_id`; the planner refuses a
   caller-generated replacement at final-plan time.
4. Add the candidate change to the exact runtime migration manifest, run
   `review-plan`, then commit only its output as `reviewed_plan` together with
   `enabled=true`. Store artifacts outside the worktree and create the final
   saved plan only with the composed guard:

   ```bash
   bash infra/deploy/terraform_runtime_guard.sh plan \
     --var-file /secure/local/path/teamagent.tfvars \
     --runtime-migration REVIEWED_MIGRATION_ID \
     --preflight-receipt /secure/local/path/preflight.json \
     --alarm-delivery-receipt /secure/local/path/alarm-delivery.json \
     --versioning-receipt /secure/local/path/versioning.json \
     --log-readiness-receipt /secure/local/path/log-readiness.json \
     --alarm-migration-receipt /secure/local/path/alarm-migration-final.json \
     --out /secure/local/path/image-release.tfplan
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
   When the separately owned HMAC generation ledger is integrated, pass only
   its non-secret `{table_arn, generation, high_water_t0, stage}` snapshot in
   `image_release_shared_generation_ledger`. The exact snapshot hash is bound
   into the gate result, saved plan, `PREPARED` intent, `APPLYING` transition,
   and `CONSUMED` transition. Any changed generation, T0, stage, or table
   identity requires a fresh plan and intent.
5. After review, apply exactly that saved plan:

   ```bash
   bash infra/deploy/terraform_runtime_guard.sh apply \
     --plan /secure/local/path/image-release.tfplan \
     --out /secure/local/path/image-release.apply.json
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

There is no one-time target-list exception. Runtime, provenance, retention,
and evidence changes use one complete saved plan or remain fail closed.

CodeBuild log groups, including the legacy `aiia-image-builder` group, are
normal operational logs and use 30-day retention. AI input/output logs use the
separately owned 60-day policy; this CodeBuild provenance change does not alter
that storage or the audit branch's S3 deletion lifecycle. The legacy resource
is retention-only and does not recreate
its retired project. Shell tracing stays disabled; ECR tokens go only to
fixed-registry `--password-stdin` logins, and temporary AWS credentials,
signature bytes, and secret values are never printed.

## Lambda 本体について

## 第2段階: schedule有効化

第1段階のservice rollout、health、log、alarm配送を確認後、activation manifestへ
post-runtimeのexact ingest/canary task definition ARNとdigestを記録します。

activation preflightはACL quarantineとcanary heartbeatを先に検証します。その後の
allowlistはheartbeat alarm作成と ingest/canary ruleの
`DISABLED` → `ENABLED` だけです。targetやtask definitionを同時変更しないため、apply途中に
旧taskが先行発火しません。

```bash
bash ../deploy/terraform_runtime_guard.sh preflight \
  --migration 2026-07-enable-ingest-canary-v1 \
  --out "$ARTIFACT_DIR/activation-preflight.json"

bash ../deploy/terraform_runtime_guard.sh plan \
  --var-file "$PWD/terraform.tfvars" \
  --out "$ARTIFACT_DIR/activation.tfplan" \
  --runtime-migration 2026-07-enable-ingest-canary-v1 \
  --preflight-receipt "$ARTIFACT_DIR/activation-preflight.json" \
  --alarm-delivery-receipt "$ARTIFACT_DIR/alarm-delivery.json" \
  --versioning-receipt "$ARTIFACT_DIR/log-versioning.json" \
  --log-readiness-receipt "$ARTIFACT_DIR/log-readiness.json" \
  --alarm-migration-receipt "$ARTIFACT_DIR/alarm-migration-final.json" \
  --prior-apply-receipt "$ARTIFACT_DIR/runtime-apply.json"

bash ../deploy/terraform_runtime_guard.sh verify \
  --plan "$ARTIFACT_DIR/activation.tfplan"
```

完了後はmigrationを再び `enabled=false` に戻し、strict syncだけを許可します。strict syncは
mcp/connect/ingest/morning/canaryのdigest完全一致、確認済みalarm配送、legacy SNS退役、
API custom-domain mapping、S3 `/app` exact metadataをliveから再取得して照合します。

## HMAC rotation

MAILとREPORTは別secretです。primary、`*_PREVIOUS`、
`*_PREVIOUS_ROTATION_STARTED_AT` はsecret値ではなく実デプロイmetadataだけを検証します。
previousとT0は同一revisionで追加・削除し、同じprevious中のT0変更を拒否します。
issuer切替はT0+900秒以内、previous削除はmailがT0+87300秒以後、reportが
T0+605700秒以後です。secret値はplan、log、receiptへ出しません。

用途別generation/high-water/stageのdurable stateはHMAC worker側の最終integrationで
追加するため、この監査branchではDynamoDB table/IAMを重複作成しません。guardは既知resource
だけでなくstate全address setのhash、lineage、serialを毎回receiptへ結合するので、最終rebaseで
追加されるtable/address/import/lock ownershipを改めてexact検証し、新しいreceiptを発行します。

## 統合順序

rolloutは次の順序を固定し、後段を先行させません。

1. PR #238/#252のruntime interfaceとstate ownership前提を取り込む。
2. HMAC separation commit
   `2de3b15632bb2d671a4836d5cf3f252dd9b25727`とworker側durable stateをrebaseし、
   用途別secret/T0/table/address/import/lock契約を満たす。
3. `/app`のexact VersionId/SHA/Vault manifest/build inputs provenanceを固定する。
4. ingestの実行中taskが0であることと、morning/canaryを含むruntime interfaceを確定する。
5. disabled review manifestを承認した後、guard-owned first-time workflowで全writer切断、
   versioning有効化、900秒無書込み二回観測、同一lock内cutoverを行い、その後の配信・
   secure exact-version export evidenceを作る。
6. 署名済みWolfi/core、OpenClaw、TikTok、x-buzzをFargate preflightし、runtime migrationを
   行う。
7. service health、SNS実配送、ACL quarantine、canary成功後にactivationだけを行う。

manifestは必要digest/role ARN/destinationが空の間は`enabled=false`を維持します。空欄を
live値から推測したり、source側でscheduleやdestinationを先行有効化したりしません。

## ローカル検証

format、offline validate、shell/JQ syntax、runtime contract tests、dispatcher tests、
archive reproducibility testsを実行します。real planはAWSのread-only refreshとstate lockを
伴うため、外部変更停止中には実行しません。
