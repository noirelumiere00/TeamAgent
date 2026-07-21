# OpenClaw trusted release and deployment Runbook

本書と `infra/terraform/README.md` が OpenClaw 本番変更の正準手順です。
`infra/codebuild/openclaw_bundle_contract.json` は現在
`release.ready=false` であり、OpenClaw の build、promotion、Terraform image
変更は意図的に fail closed です。ローカル検証に合格しても本番デプロイ資格には
なりません。

禁止経路は、ローカル `docker push`、mutable tag、direct ECR copy/tag、
direct ECS task-definition registration/update、`terraform -target`、
旧 `apply_openclaw.sh`、S3 metadata や隣接 checksum だけを根拠にした公開です。

## 1. 現在の判定

- OpenClaw core runtime: ローカル検証可能
- OpenClaw media subject: 未統合
- exact core/media bundle receipt: 未実装
- signed final-HEAD registry evidence: 未取得
- guarded post-apply functional rollback gate: 実装済み、独立レビュー・実環境証跡待ち
- production: **NO-GO**

上記の不足が解消され、独立レビュー後に contract を別変更で
`release.ready=true` にするまでは AWS build も Terraform image plan も停止します。

## 2. 最終HEADのローカル ARM64 検証

clean で attached な reviewed commit からだけ実行します。

```sh
test -z "$(git status --porcelain=v1 --untracked-files=all)"
COMMIT=$(git rev-parse HEAD)
TREE=$(git rev-parse HEAD^{tree})
SHORT=${COMMIT:0:12}
bash infra/openclaw/build-image.sh \
  --image "teamagent-openclaw:git-$SHORT" \
  --manifest "/tmp/openclaw-$SHORT-manifest.json" \
  --evidence-dir "/tmp/openclaw-$SHORT-evidence"
(cd /tmp && sha256sum -c "openclaw-$SHORT-manifest.json.sha256")
```

manifest は schema 5 で、次を満たす必要があります。

```sh
jq -e --arg commit "$COMMIT" --arg tree "$TREE" '
  .schemaVersion == 5 and
  .deploymentCredential == false and
  .source.commit == $commit and
  .source.tree == $tree and
  (.source.archiveSha256 | test("^[0-9a-f]{64}$")) and
  (.source.releaseContractSha256 | test("^[0-9a-f]{64}$")) and
  .promotion.status == "LOCAL_GATES_PASSED" and
  .promotion.registryPublished == false and
  .promotion.canonicalTagPublished == false and
  .runtime.platform == "linux/arm64" and
  .runtime.actualImageContractPassed == true and
  .runtime.controlUiFullAssetClosureValidated == true and
  .materials.exactSetMatch == true and
  .sbom.wholeFilesystemExactMatch == true and
  .sbom.physicalNpmMultisetExactMatch == true and
  .scan.exactSingleLinuxArm64Subject == true and
  .scan.critical == 0 and
  .scan.high == 0 and
  .scan.secrets == 0 and
  .scan.allKnownLiveFindingsAbsent == true
' "/tmp/openclaw-$SHORT-manifest.json"
```

基準は Critical=0、High=0、Secrets=0 です。最新 live で観測された
`CVE-2026-12087`、`13221`、`33845`、`34182`、`42010`、`55200`、
`57433`、`6100` の8件が候補に存在しないことも必須です。

統合 filesystem の唯一の digest claim は
`image.rootfs.inventorySha256` です。これは path/type/mode/uid/gid/size/link
target/content hash を正規化した `rootfs-inventory.json` を指し、同じ image
からの fresh export 2回で同一になることを実像テストで確認します。Docker
merged export tar の byte hash は tar metadata に依存して再現不能なので、
manifest、SBOM、equivalence、evidence index の証拠 claim に含めません。

この helper は registry credential、push、promotion、ECS/Terraform 操作を
持ちません。出力 manifest も `deploymentCredential=false` です。

## 3. 署名済み build と promotion

contract が独立承認で active になった後だけ、次の固定経路を使用します。

1. publisher が exact remote `dev` の full 40-character commit、Git tree、
   commit object、実行ファイル一覧、contract hash を source manifest に束縛する。
2. source manifest と署名を KMS、S3 VersionId、COMPLIANCE Object Lock で固定する。
3. 専用 OpenClaw CodeBuild が source を再取得して署名と exact `origin/dev`
   を確認する。
4. `infra/openclaw/build-bundle.sh` が core/media の2 subject を quarantine
   repository だけへ出力する。
5. source-free attestor が actual single `linux/arm64` image、OCI labels、
   installed binary probes、Trivy、SPDX SBOM、in-toto provenance、署名と
   referrer set を検証する。
6. source-free promoter が exact subject と referrer を
   verified-candidate、承認後に release repository へ immutable copy する。

build role は candidate/release repository、publisher evidence、deployment
resource を変更できません。publisher/launcher も ECS、EventBridge、
task definition、service を変更できません。

通常の build-only launcher は次です。現在は `release.ready=false` で AWS call
より前に停止することが正しい動作です。

```sh
bash infra/deploy/build_openclaw_image.sh
```

## 4. Release authorization

actual-image evidence と独立レビューが揃った後、exact candidate receipt key と
manifest/signature の S3 VersionId を使って active または rollback authorization
を作ります。

```sh
bash infra/deploy/authorize_image_release.sh --help
```

candidate receipt、signature、release digest のいずれかが曖昧、期限切れ、
mutable、別contract、別source、別platformなら停止します。authorization は
Terraform を実行しません。

## 5. One-time full saved plan

`terraform.tfvars` には release repository の digest と exact evidence
VersionId、および明示的なSlack DM契約を設定します。

```hcl
openclaw_image = "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-openclaw@sha256:<RELEASE_DIGEST>"
image_release_evidence = {
  openclaw = {
    bucket               = "teamagent-dev-image-release-evidence"
    key                  = "<exact receipt key>"
    version_id           = "<exact receipt VersionId>"
    signature_key        = "<exact signature key>"
    signature_version_id = "<exact signature VersionId>"
  }
}
shared_company_domains = "vectorinc.co.jp"        # §G 会社共有ドメイン
openclaw_model_id      = "jp.anthropic.claude-haiku-4-5"   # 手順0で確定した値
enable_vpc_endpoints   = true
alarm_email_endpoints  = ["s-komata@vectorinc.co.jp"]

# 全DM送信者をOpenClawへ通す場合（後段のTeamAgent identity/RLS gateは別途必須）
slack_dm_allowlist = "*"

# または、DMをexact Slack user IDsだけに限定する場合
# slack_dm_allowlist = "U09CX1CCBLN,U0123456789"
```

`slack_dm_allowlist`の空文字既定値は「本番では明示指定が必須」を表す安全な
sentinelです。空/未設定、空白、重複、`*`とIDの混在、U以外のIDはTerraform
planで拒否します。`"*"`は`dmPolicy=open`かつ`allowFrom=["*"]`、個別U ID群は
`dmPolicy=allowlist`かつ指定順のexact `allowFrom`へentrypointが同時変換します。
task hardenerとentrypointも同じ契約を再検証し、不一致は起動前にfail-closedです。

`image_deployment_intent_id` は手入力しません。plan は worktree 外へ作成し、
全差分をレビューして同じ opaque saved plan を一度だけ apply します。

```sh
bash infra/deploy/terraform_runtime_guard.sh plan --help
terraform show /secure/local/path/openclaw-release.tfplan
bash infra/deploy/terraform_runtime_guard.sh apply --plan /secure/local/path/openclaw-release.tfplan --out /secure/local/path/openclaw-release.apply.json
```

`terraform_runtime_guard.sh` は clean exact `origin/dev`、固定 automation role、
backend/workspace/state lineage/serial、resource ownership、contract、
receipt/signature VersionId、release graph、one-time intent と receipt claims を
再検証します。plan の apply attempt を開始した後は、成功・失敗にかかわらず
同じ plan を再実行しません。曖昧な失敗は reconcile し、fresh receipt、
new intent、new plan で roll-forward または rollback します。旧
`infra/terraform/apply_image_release_plan.sh` は廃止済みで、迂回経路として
使用しません。

### 5.1 通常運用logの30日adoption

runtime migrationには、既存のContainer Insights
`/aws/ecs/containerinsights/teamagent-dev/performance` と
`/aws/ecs/containerinsights/teamagent-dev-tiktok/performance`（live初期値はいずれも1日）を
含む7 log groupのin-place import/adoptionが必要です。各groupの既存eventをexact
S3 bucket/key/versionからfresh fileへ取得したretention export receiptを作り、
`terraform_runtime_guard.sh plan`へ渡します。receiptはcanonical path/device/inode/
nlink=1/size/timestamps/hash、AWS metadata、delivery時刻を拘束し、saved plan時とapply直前に
guardが再取得・再hashします。7 groupの1件でも欠落、別version、差替え、時刻逆転なら中断し、
log groupを削除・再作成したりdirect retention変更で迂回しません。

### 5.2 Slack prerequisites

OpenClaw は既存 Bot と分離した専用 Slack アプリ、Socket Mode、専用チャネルを
使用します。必要 scope、exact team ID、`xoxb-`/`xapp-` secret、DM allowlist は
事前にレビューし、値をログ、image、evidence、tfvarsへ書き出しません。反映は
direct `update-service` ではなく、署名済み release と同じ guarded saved-plan
だけで行います。

詳細は `infra/terraform/README.md` を参照してください。

## 6. Runtime/Fargate contract

本番 task definition は次を同時に満たします。

- exact release repository `@sha256`、ARM64 Fargate
- UID/GID `65532:65532`
- read-only root filesystem
- writable path は task-scoped `openclaw-tmp` を mount した `/tmp` のみ
- capability drop `ALL`、`privileged=false`
- image の canonical ENTRYPOINT/CMD を上書きしない
- `SLACK_TEAM_ID`はcanonical `T...` exact IDをOpenClaw/MCP双方へ注入し、空・不正値を
  Terraform validation、entrypoint、MCP起動時の三層で拒否
- `TEAMAGENT_CALLER_CLAIM_SECRET`はMCP bearerとは別の32-byte以上のSecrets Manager値。
  OpenClawとMCPだけに注入し、image/config/evidenceへ焼き込まない。誤ってbearerと同値を
  格納した場合もOpenClaw/MCP双方が起動を拒否
- MCPだけに`TEAMAGENT_CALLER_CLAIM_REPLAY_TABLE`を注入し、専用DynamoDB tableへの
  `dynamodb:PutItem`だけを許可する。`attribute_not_exists(nonce)`の条件付き書込みが
  rolling task間も含むone-useの正準判定で、DynamoDB障害時はcaller認可をfail closed
- `SLACK_DM_ALLOWLIST`は明示必須で、`"*"`または1〜100件の重複しない
  comma-separated Slack U IDだけを受理
- `/readyz` health check
- sidecar、追加 volume/mount、環境 retarget、role retarget を禁止
- ECS deployment circuit breaker と rollback を有効化

OpenClaw 2026.7.1の実runtime hook契約では、`inbound_claim`/`message_received`のSlack event
`user/team/channel/message/session/thread/runId`を内部pluginがexact bindingとして保持し、
authoritative agent contextでも同じrunを照合します。runIdの無い互換hookは一意なfresh
eventだけをrunへ束縛します。`before_tool_call`ではhost側の`runId`/`toolCallId`一致と
一回性を検証し、exact tool・全引数hash・nonce・iat/exp・audienceへ署名します。
同一sessionへ別userのeventが並行到着してもlatest値は使いません。
MCPは申告`slack_user_id`との一致、署名、request binding、一回性を検証してから
`users.info` resolverを呼びます。company-sharedもresolver成功、exact team、
非guest/非strangerが必須で、欠落・未知・障害はfail closedです。
OpenClaw nativeの`message`、filesystem read/write/edit、全session toolは明示denyし、
署名resolver+nonceを通る`bundle-mcp`だけを追加許可します。

Fargate は Docker `no-new-privileges` を強制できません。これは隠さず残余リスク
として扱い、nonroot、read-only rootfs、capability drop、固定IAM/SGで補償します。

## 7. Post-apply functional gates

canonical `terraform_runtime_guard.sh apply` は、同じ shared deployment lock と
backend workflow lock を保持したまま、intent を `APPLIED` にする前に
`run-live-rollout-gates.mjs` を実行します。old/new revision は異なる必要があり、
OpenClaw revision が変わらない apply では gate を明示的に skip した結果を
apply receipt に束縛します。revision が変わる場合は次をすべて自動実行します。

- ECS `services-stable`
- Slack canary の前後で running service task を全件列挙し、全taskが exact
  candidate revision であることを再確認
- 同一network/task revisionの one-off canary exit 0
- ECS task-role credential による Bedrock `Converse`
- MCP `tools/list` が既定12件かつ reviewed 28件の範囲内
- Slack Socket Mode の exact mention/reply と candidate task log stream の相関
- apply attempt、old/new revision、exact automation role、one-use rollback
  authorization を束縛した rollout result
- fixed KMS asymmetric signature と fixed S3 VersionId、SSE-KMS、
  COMPLIANCE Object Lock、downloaded hash/signature の再検証

post-apply のどの確認が失敗しても、intent を `APPLIED` にする前に one-use
rollback authorization を消費し、durable previous task definition を検証して
service と全 running task の復旧を確認します。guard cleanup も同じ lock 内で
idempotent に復旧を再試行します。

この実装だけでは production evidence ではありません。Terraform resource の
実環境適用、独立レビュー、実際の signed S3 result と forced-failure rollback
receipt が揃うまで contract は closed のままです。手動実行を production evidence
として代用しません。

## 8. Alarms and operational checks

- `openclaw_config_invariant_violation` と `openclaw_entrypoint_error` は
  `OpenClawStartupFailure` に集約する。
- log group、metric filter、alarm、SNS subscription を plan で確認する。
- startup failure、ECS circuit-breaker rollback、task exit、Slack/Bedrock/MCP
  gate失敗を監視する。
- OpenClaw の通常運用ログ保持は30日。AI入出力ログは60日。audit は別方針で
  自動削除しない。

## 9. Production GO条件

次のすべてが揃うまで production は **NO-GO** です。

- media subject と exact core/media receipt emitter が実装・レビュー済み
- `release.ready=true` の別変更が全CI・独立レビュー済み
- final `origin/dev` の signed source、actual-image C0/H0/S0、SBOM、
  provenance、signature、immutable receipt が揃う
- full saved plan と live state が一致する
- post-apply functional rollback gate が one-time apply と統合済み
- 実環境の CodeBuild/ECR/ECS/Slack/Bedrock/MCP/CloudWatch 検証が全緑

ローカル合格、merge、CI全緑のいずれも単独では production GO ではありません。
