# TeamAgent Terraform runtime operations

このstateは TeamAgent dev、AWS account `718959508629`、`ap-northeast-1` 専用です。
runtimeを含む plain Terraform、targeted apply、旧image-only script、可変
`codebuild/source.zip` builderは使用しません。操作入口は
`infra/deploy/terraform_runtime_guard.sh` だけです。適用時はexact trusted automation roleが
共有deployment lockを取得し、そのlockを直前再検証から保存planの適用完了まで保持して、
原子的apply receiptを発行します。

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
- ingest-weeklyとcanary-hourlyは無効、morning-digestは有効のまま第1段階を行います。
- alarm SNSには確認済み配送先がありません。approved exact email
  `s-komata@vectorinc.co.jp`を指定しない限りplanは生成されません。AWS providerはemail確認を待てない
  ため、このmigrationはsubscriptionを作りません。emailは先にcanonical topic上で確認を
  完了し、正規化済み`s-komata@vectorinc.co.jp`のexact hashと一致してから指定します。
  canonical topicの全protocolとPendingConfirmation/Deletedを含むsubscription inventoryを
  hash化し、approved emailのconfirmed `email` protocol 1件だけを受理します。追加endpoint、
  `email-json`、pending/deleted subscription、別email、canonical topicへ接続した
  Amazon Q Developer in chat applications (AWS Chatbot) はruntime変更前に停止します。
  approved destinationはtopic ARN、正規化email、confirmed/no-filter/raw-delivery-off、
  Chatbot 0件を含む固定destination-state hashにも束縛します。confirmed subscriptionは
  `GetSubscriptionAttributes`で再取得し、`PendingConfirmation=false`、email確認済み、
  `FilterPolicy`/`FilterPolicyScope`なしを検証します。さらにunique test messageの実受信を
  人が確認した短命`teamagent-alarm-delivery-test-receipt`が無い限りmigration planを
  生成しません。receiptにはendpoint値を残さずhashだけを結合します。

## CloudTrail / Bedrock log bucket hardening

CloudTrailとBedrock invocation logsの既存bucketはS3 versioningを有効化し、bucket policyへ
TLS未使用通信の明示Denyを追加します。Object LockとMFA Deleteは設定しません。
CloudTrail監査ログにはlifecycleを設定せず、自動削除しません。

Bedrock AI入出力ログは承認済みの `bedrock_logs_retention_days = 60` に固定します。
lifecycleの対象は`bedrock/` prefixだけです。Bedrockが生成するappend-onlyの一意object
keyは59日後にdelete markerで非現行化し、非現行versionを1日後、すなわち生成から合計
60日の境界で完全削除します。現行60日＋非現行60日という120日保持にはしません。
参照versionが無いexpired delete markerも削除します。他prefixやCloudTrail objectは
このlifecycleの対象外で、CloudTrail監査ログには自動削除を設定しません。

Bedrockの実配信先は
`bedrock/AWSLogs/<account>/BedrockModelInvocationLogs/*`へ固定し、
`bedrock.amazonaws.com`、exact `SourceAccount`、リージョン・アカウントを含む
`SourceArn`を要求します。KMS grantも同じ条件で`kms:GenerateDataKey`だけを許可し、
Encrypt/Decryptやwildcard actionへ広げません。

S3公式仕様では、bucketで初めてversioningを有効にした後は伝播に最大15分かかるため、
その間のobject PUT/DELETEを避けます。
https://docs.aws.amazon.com/AmazonS3/latest/userguide/manage-versioning-examples.html

現liveはCloudTrail/Bedrock producerが既に配信中です。稼働中destinationをその場で
UnversionedからEnabledへ変更するcommandも、稼働後の観測を過去のpre-cutover証跡として
扱う経路も提供しません。新しいdestinationはproducerを向ける前にversioningを有効化し、
AWS CloudTrail event historyの各`PutBucketVersioning(Status=Enabled)`時刻から900秒待って
から、別のreview済みfull planでproducerをcutoverします。既存producerのdestroy/pauseを
「待機」とみなす方式も禁止です。

1. 新destinationをproducer未接続の状態で作成・versioning有効化する。CloudTrail trailは
   exact destinationへ設定済みでも`IsLogging=false`、Bedrock invocation loggingは未設定
   でなければならない。
2. `attest-log-versioning`はexact trusted automation roleと共有lockの下で、両bucketの
   `Status=Enabled`、CloudTrailの削除lifecycle無し、全writer disconnected、CloudTrail event
   history由来の独立したversioning-enabled時刻、900秒settle完了、bucket/versioning identity、
   初期化済みbackend/state、将来のexact producer cutoverを原子的pre-cutover receiptへ束縛する。
   Unversioned/Suspended、writer接続済み、時刻欠落、900秒未満なら書き換えずfail closedする。
3. reviewed cutoverはそのreceiptの`cutover.not_before_epoch`後だけに行う。guarded runtime planは
   producerをexact no-opに固定し、receiptへ束縛されたcutoverと現producerが一致しなければ停止する。
4. cutover後のCloudTrail最新log+digest、Bedrock最新delivery、30日化する5 log groupのexport
   manifestを、0600の`teamagent-log-readiness-evidence`へ具体的なkey/version/ETag/size/timestamp
   とともに記録する。各delivery/retention content hashは別々の0600 export fileのcanonical
   path/inode/sizeへ束縛し、delivery/retention timestampはevidence observation時刻以下にする。
   guardはplan、verify、apply直前に全export fileを再hashし、path/inode/sizeも再検証する。

```bash
bash ../deploy/terraform_runtime_guard.sh attest-log-versioning \
  --out "$ARTIFACT_DIR/log-versioning.json"
```

このattestation commandはAWS bucket設定を書きません。`terraform plan -target`や部分applyは
禁止です。pre-versioned destinationへの非targeted runtime migrationのfull-root planでTLS deny、
Bedrock exact delivery policy/KMS、
`bedrock/` 60日lifecycle、5 log groupのimport/30日retentionを収束させます。
CloudTrailとBedrock producer resourceは`prevent_destroy`で保護し、plan validatorも両者を
exact `no-op`に固定するため、versioning伝播待ちをproducerの停止で代替できません。

```bash
# cutover後、secure evidence/export artifactを確認して作るreceiptの必須binding:
# versioning_receipt_sha256 = sha256(log-versioning.json)
```

`enable_cloudtrail_log_delivery=false`と
`enable_bedrock_invocation_log_delivery=false`は未採用の新規bootstrap前だけの入力です。
採用済みproducerを擬似的にpauseする用途には使いません。適用は別途承認後だけです。
現在はlog versioning attestation stageとruntime manifestがともに`enabled=false`で、
pre-versioned cutover destinationも未承認です。attestation、migration plan、applyは
いずれもfail closedし、全writeはNO-GOです。

## Auto-created CodeBuild / Lambda log retention

次の既存log groupは削除・再作成せず、固定Terraform addressへimportして
`retention_in_days = 30`だけをin-place更新します。

- `/aws/codebuild/teamagent-dev-aiia-image-builder` →
  `aws_cloudwatch_log_group.codebuild_aiia_image_builder`
- `/aws/codebuild/teamagent-dev-image-builder` →
  `aws_cloudwatch_log_group.codebuild_image_builder`
- `/aws/lambda/teamagent-dev-reminders-notify` →
  `aws_cloudwatch_log_group.reminder_notify`
- `/aws/lambda/teamagent-dev-tiktok-acquire-dispatch` →
  `aws_cloudwatch_log_group.tiktok_dispatch`
- `/aws/lambda/teamagent-dev-x-buzz-dispatch` →
  `aws_cloudwatch_log_group.x_dispatch`

各resourceは`prevent_destroy`で保護し、`kms_key_id`をignoreするため、既存のKMS関連付けを
追加・解除しません。migration guardはimport ID、現在のNever Expire、30日への更新、
KMS不変、その他属性不変をexactに検証します。guardは`.terraform/terraform.tfstate`の
初期化済みbackend metadataを毎回再読取りし、credential/endpoint/workspace prefix等の
注入が無いexact S3 bucket/key/region/DynamoDB lock/encrypt設定を正規化hashで束縛します。
さらにdefault workspace、state lineage/serial、`data.` prefixとmodule/string/numeric
indexを含む全address-set hash、5 addressのremote ID所有権をreceiptへ結合します。
runtime migrationでは未importの既存group→exact import+30日update、import済みNever
Expire→30日update、既に30日→no-opを正規状態として扱うため、部分成功後も安全に再開できます。
provider planの`tags=null`とprovider defaultを含む`tags_all`だけを受理します。
別state/addressとのcollisionはapplyせず、state所有者を確認して専用の
`moved`/state migrationを先にレビューしてください。

この変更はlog groupを再作成しませんが、30日より古い既存eventはretention適用後に削除対象と
なり、AWS公式仕様では通常最大72時間で削除されます。最古eventが30日以内であること、または
30日超の履歴をexportしてchecksum付きで保全したこと（もしくは明示的な廃棄承認）をreview
evidenceへ残すまではruntime migrationを有効化しません。
https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/Working-with-log-groups-and-streams.html

## 必須入力

第1段階を有効化するreview commitで、次を全てexact値で埋めます。

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

bash ../deploy/terraform_runtime_guard.sh plan \
  --var-file "$PWD/terraform.tfvars" \
  --out "$ARTIFACT_DIR/runtime.tfplan" \
  --runtime-migration 2026-07-wolfi-runtime-v1 \
  --preflight-receipt "$ARTIFACT_DIR/preflight.json" \
  --alarm-delivery-receipt "$ARTIFACT_DIR/alarm-delivery.json" \
  --versioning-receipt "$ARTIFACT_DIR/log-versioning.json" \
  --log-readiness-receipt "$ARTIFACT_DIR/log-readiness.json"

bash ../deploy/terraform_runtime_guard.sh verify \
  --plan "$ARTIFACT_DIR/runtime.tfplan"

# 実行は承認後、exact trusted automation roleだけで行う。
bash ../deploy/terraform_runtime_guard.sh apply \
  --plan "$ARTIFACT_DIR/runtime.tfplan" \
  --out "$ARTIFACT_DIR/runtime-apply.json"
```

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
  --log-readiness-receipt "$ARTIFACT_DIR/log-readiness.json"

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
5. pre-versioned destinationへreview済みcutover後、exact attestationを実行し、追加900秒
   待機後の配信・secure export evidenceを作る。
6. 署名済みWolfi/core、OpenClaw、TikTok、x-buzzをFargate preflightし、runtime migrationを
   行う。
7. service health、SNS実配送、ACL quarantine、canary成功後にactivationだけを行う。

manifestは必要digest/role ARN/destinationが空の間は`enabled=false`を維持します。空欄を
live値から推測したり、source側でscheduleやdestinationを先行有効化したりしません。

## ローカル検証

format、offline validate、shell/JQ syntax、runtime contract tests、dispatcher tests、
archive reproducibility testsを実行します。real planはAWSのread-only refreshとstate lockを
伴うため、外部変更停止中には実行しません。
