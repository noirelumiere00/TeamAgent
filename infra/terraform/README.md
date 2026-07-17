# TeamAgent Terraform runtime operations

このstateは TeamAgent dev、AWS account `718959508629`、`ap-northeast-1` 専用です。
runtimeを含む plain Terraform、targeted apply、旧image-only script、可変
`codebuild/source.zip` builderは使用しません。操作入口は
`infra/deploy/terraform_runtime_guard.sh` だけです。このscriptは保存planを作成・再検証
しますが、apply機能は意図的に持ちません。

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
- alarm SNSには確認済み配送先がありません。email endpointまたは既存chat integrationの
  どちらか一方を指定しない限りplanは生成されません。AWS providerはemail確認を待てない
  ため、このmigrationはsubscriptionを作りません。emailは先にcanonical topic上で確認を
  完了し、そのlive endpoint hashをmanifestへ記録してから指定します。chat経路もcanonical
  topicへ接続済みのexact ARNだけをplan前snapshotで受け付けます。未確認email、未接続chat、
  endpoint 0件はいずれもruntime変更前に停止します。

## 必須入力

第1段階を有効化するreview commitで、次を全てexact値で埋めます。

1. connect/ingest/morning/canaryを含む全live task definition ARNと完全image digest。
2. `e20411cc…` とHMAC契約 `2de3b156…` の両方を含むWolfi coreの完全digest、
   40桁source commit、固定KMS key ARN。
3. 独立したOpenClaw、TikTok、x-buzzの完全digest。
4. dispatcher code hash、legacy alarm参照数、確認済みalarm配送先。
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
  --preflight-receipt "$ARTIFACT_DIR/preflight.json"

bash ../deploy/terraform_runtime_guard.sh verify \
  --plan "$ARTIFACT_DIR/runtime.tfplan"
```

preflightはcandidate task definitionを実際に登録し、fresh Fargate volume上でUID 10001の
`/tmp` write、browser/cache/npx/yt-dlp、OpenClaw UID 65532と暗号化EFS writeを検証して
必ず後始末します。task definitionの設定だけを根拠に成功扱いしません。

保存planはexact allowlist、create-before-destroy、env/secrets/roles/network/healthの
live parity、dispatcher completion ack、API/SNS/monitoring/retention、legacy builder退役、
IAM direct-mutation boundaryを全て満たす場合だけ残ります。ingest/canary ruleはこの段階では
無効のままです。

適用は、このrepositoryの保存planとreceiptを検証する承認済みautomation roleだけが行う
前提です。長期IAM user `AIIAdev` にはmigrationの最後にservice promotion、schedule変更、
dispatcher更新、CodeBuild起動、API endpoint再有効化の明示Denyを付けます。AWS account
rootはIAM identity policyの対象外なので、root credential統制とOrganizations SCPは
account管理側の必須条件です。

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
  --preflight-receipt "$ARTIFACT_DIR/activation-preflight.json"

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

## ローカル検証

format、offline validate、shell/JQ syntax、runtime contract tests、dispatcher tests、
archive reproducibility testsを実行します。real planはAWSのread-only refreshとstate lockを
伴うため、外部変更停止中には実行しません。
