#!/usr/bin/env bash
# The only supported OpenClaw production rollout path.
#
# This script never decides deployability from a release manifest.  A shared
# trusted-release worker must verify a KMS-signed, fresh, one-time receipt and
# must atomically consume it immediately before update-service.  Until that
# shared verifier exists in this checkout, render and deploy both fail closed.
set -Eeuo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd -P)
TASK_FILTER="$REPO_ROOT/infra/openclaw/harden-task-definition.jq"
LOCAL_TRUST_CONTRACT="$REPO_ROOT/infra/openclaw/trusted-release-contract.json"
TRUST_CONTRACT=/opt/teamagent/trusted-release/contracts/teamagent-openclaw-production-v1.json
TRUSTED_CLI=/opt/teamagent/trusted-release/bin/trusted-release
ROLLOUT_GATE="$REPO_ROOT/infra/openclaw/run-live-rollout-gates.mjs"

# These production identities are deliberately not environment-overridable.
AWS_ACCOUNT_ID=718959508629
AWS_REGION=ap-northeast-1
ECR_REPOSITORY=718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-openclaw
ECS_CLUSTER=teamagent-dev
ECS_SERVICE=teamagent-dev-openclaw
ECS_FAMILY=teamagent-dev-openclaw
TRUST_PROFILE=teamagent-openclaw-production-v1
METRIC_NAMESPACE=TeamAgent/dev

usage() {
  cat >&2 <<'USAGE'
usage:
  apply_openclaw.sh <trusted-deployment-receipt.json>
  apply_openclaw.sh --render-only <current-task-definition.json> <trusted-deployment-receipt.json>

The receipt is issued by the shared trusted-release framework after immutable
promotion. A release manifest or an adjacent checksum is not a deployment
credential. --render-only performs no AWS operation, but still requires the
shared verifier and a valid, fresh, unconsumed receipt.
USAGE
}

fail() {
  echo "[openclaw-deploy] FATAL: $*" >&2
  exit 1
}

for command in cmp jq sha256sum; do
  command -v "$command" >/dev/null || fail "required command not found: $command"
done
for required_file in "$TASK_FILTER" "$LOCAL_TRUST_CONTRACT"; do
  [[ -f "$required_file" && ! -L "$required_file" ]] || \
    fail "required deployment contract is missing or a symlink: $required_file"
done
[[ -x "$TRUSTED_CLI" && ! -L "$TRUSTED_CLI" ]] || \
  fail "shared trusted-release verifier is absent; render/deploy is blocked"
[[ -f "$TRUST_CONTRACT" && ! -L "$TRUST_CONTRACT" ]] || \
  fail "shared trusted-release contract is absent; render/deploy is blocked"
cmp -s "$LOCAL_TRUST_CONTRACT" "$TRUST_CONTRACT" || \
  fail "repository integration contract differs from the shared trusted contract"

jq -e \
  --arg profile "$TRUST_PROFILE" \
  --arg account "$AWS_ACCOUNT_ID" \
  --arg region "$AWS_REGION" \
  --arg repository "$ECR_REPOSITORY" \
  --arg cluster "$ECS_CLUSTER" \
  --arg service "$ECS_SERVICE" \
  --arg family "$ECS_FAMILY" '
  .schemaVersion == 1 and
  .profile == $profile and
  .sharedCliPath == "/opt/teamagent/trusted-release/bin/trusted-release" and
  .sharedContractPath == "/opt/teamagent/trusted-release/contracts/teamagent-openclaw-production-v1.json" and
  .target.awsAccountId == $account and
  .target.region == $region and
  .target.repository == $repository and
  .target.cluster == $cluster and
  .target.service == $service and
  .target.taskFamily == $family and
  .source.allowArbitraryS3Zip == false and
  .source.allowBuilderProducedVerificationBooleans == false and
  .promotion.requiredPlatform == "linux/arm64" and
  .promotion.requiredPlatformManifestCount == 1 and
  .promotion.requireAllTestsAndScansBeforeCanonicalTag == true and
  .promotion.requireAllEvidenceFilesIndexedAndSigned == true and
  .deploymentReceipt.oneTime == true and
  .deploymentReceipt.requireAtomicConsume == true and
  .deploymentReceipt.requiredScanStatus.critical == 0 and
  .deploymentReceipt.requiredScanStatus.high == 0 and
  .deploymentReceipt.requiredScanStatus.secrets == 0 and
  (.deploymentReceipt.requiredScanStatus.knownLiveFindingIdsAbsent | length) == 8
' "$LOCAL_TRUST_CONTRACT" >/dev/null || fail "local trusted-release integration contract is invalid"

MODE=deploy
CURRENT_TASK_PATH=""
if [[ ${1:-} == "--render-only" ]]; then
  (($# == 3)) || { usage; exit 2; }
  MODE=render
  CURRENT_TASK_PATH=$2
  RECEIPT_PATH=$3
else
  (($# == 1)) || { usage; exit 2; }
  RECEIPT_PATH=$1
fi

[[ -f "$RECEIPT_PATH" && ! -L "$RECEIPT_PATH" ]] || \
  fail "trusted deployment receipt is missing or a symlink"

tmp_dir=$(mktemp -d /tmp/openclaw-deploy.XXXXXX)
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

canonicalize_current_task() {
  jq -S -c '
    (.taskDefinition // .) |
    del(
      .revision,
      .status,
      .requiresAttributes,
      .compatibilities,
      .registeredAt,
      .registeredBy,
      .deregisteredAt
    )
  ' "$1"
}

canonical_sha256() {
  jq -S -c . "$1" | sha256sum | cut -d" " -f1
}

if [[ "$MODE" == render ]]; then
  [[ -f "$CURRENT_TASK_PATH" && ! -L "$CURRENT_TASK_PATH" ]] || \
    fail "current task definition is missing or a symlink"
  cp "$CURRENT_TASK_PATH" "$tmp_dir/current-task.json"
else
  command -v aws >/dev/null || fail "required command not found: aws"
  command -v node >/dev/null || fail "required command not found: node"
  [[ -f "$ROLLOUT_GATE" && ! -L "$ROLLOUT_GATE" ]] || \
    fail "live rollout gate is absent; deployment is blocked"

  aws ecs describe-services \
    --region "$AWS_REGION" \
    --cluster "$ECS_CLUSTER" \
    --services "$ECS_SERVICE" \
    --output json >"$tmp_dir/current-service.json"
  jq -e \
    --arg service "$ECS_SERVICE" \
    --arg cluster "$ECS_CLUSTER" '
    (.failures | length) == 0 and
    (.services | length) == 1 and
    .services[0].serviceName == $service and
    (.services[0].clusterArn | endswith("/" + $cluster)) and
    (.services[0].taskDefinition |
      test("^arn:aws:ecs:ap-northeast-1:718959508629:task-definition/teamagent-dev-openclaw:[1-9][0-9]*$"))
  ' "$tmp_dir/current-service.json" >/dev/null || fail "unexpected ECS service identity"
  CURRENT_TASK_ARN=$(jq -er '.services[0].taskDefinition' "$tmp_dir/current-service.json")
  aws ecs describe-task-definition \
    --region "$AWS_REGION" \
    --task-definition "$CURRENT_TASK_ARN" \
    --query taskDefinition \
    --output json >"$tmp_dir/current-task.json"
fi

canonicalize_current_task "$tmp_dir/current-task.json" >"$tmp_dir/current-task-canonical.json"
CURRENT_TASK_SHA256=$(sha256sum "$tmp_dir/current-task-canonical.json" | cut -d" " -f1)
CURRENT_TASK_ARN=$(jq -er '
  (.taskDefinition // .).taskDefinitionArn |
  select(test("^arn:aws:ecs:ap-northeast-1:718959508629:task-definition/teamagent-dev-openclaw:[1-9][0-9]*$"))
' "$tmp_dir/current-task.json") || fail "current task ARN does not match the fixed family"

"$TRUSTED_CLI" inspect-deployment-receipt \
  --profile "$TRUST_PROFILE" \
  --contract "$TRUST_CONTRACT" \
  --receipt "$RECEIPT_PATH" \
  --current-task "$tmp_dir/current-task-canonical.json" \
  --output "$tmp_dir/inspection.json"

jq -e \
  --arg profile "$TRUST_PROFILE" \
  --arg account "$AWS_ACCOUNT_ID" \
  --arg region "$AWS_REGION" \
  --arg repository "$ECR_REPOSITORY" \
  --arg cluster "$ECS_CLUSTER" \
  --arg service "$ECS_SERVICE" \
  --arg family "$ECS_FAMILY" \
  --arg currentArn "$CURRENT_TASK_ARN" \
  --arg currentSha "$CURRENT_TASK_SHA256" '
  def sha256: test("^sha256:[0-9a-f]{64}$");
  def hex256: test("^[0-9a-f]{64}$");
  .schemaVersion == 1 and
  .profile == $profile and
  .verification.kmsSignatureVerified == true and
  .verification.fresh == true and
  .verification.oneTime == true and
  .verification.unconsumed == true and
  (.receipt.id | test("^[A-Za-z0-9._:-]{16,256}$")) and
  (.receipt.issuedAt | fromdateiso8601 | type) == "number" and
  (.receipt.expiresAt | fromdateiso8601 | type) == "number" and
  .target == {
    awsAccountId:$account,
    region:$region,
    repository:$repository,
    cluster:$cluster,
    service:$service,
    taskFamily:$family
  } and
  .release.repository == $repository and
  (.release.imageManifestDigest | sha256) and
  .release.runtimeRef == ($repository + "@" + .release.imageManifestDigest) and
  (.release.sourceCommit | test("^[0-9a-f]{40}$")) and
  (.release.sourceArchiveSha256 | hex256) and
  (.release.buildIdentity |
    test("^arn:aws:codebuild:ap-northeast-1:718959508629:build/teamagent-dev-openclaw-image-builder:[A-Za-z0-9-]+$")) and
  (.release.wholeFilesystemSbomDigest | sha256) and
  (.release.provenanceDigest | sha256) and
  (.release.imageSignatureDigest | sha256) and
  (.release.referrerSetDigest | sha256) and
  (.release.evidenceIndexDigest | sha256) and
  .release.platform == "linux/arm64" and
  .release.platformManifestCount == 1 and
  .release.scanStatus.status == "PASS" and
  .release.scanStatus.platform == "linux/arm64" and
  .release.scanStatus.platformManifestCount == 1 and
  .release.scanStatus.critical == 0 and
  .release.scanStatus.high == 0 and
  .release.scanStatus.secrets == 0 and
  (.release.scanStatus.knownLiveFindingIdsAbsent | sort) == ([
    "CVE-2026-12087",
    "CVE-2026-13221",
    "CVE-2026-33845",
    "CVE-2026-34182",
    "CVE-2026-42010",
    "CVE-2026-55200",
    "CVE-2026-57433",
    "CVE-2026-6100"
  ] | sort) and
  .release.exactMaterialsVerified == true and
  .release.wholeFilesystemSbomExact == true and
  .release.allEvidenceFilesBound == true and
  .release.canonicalPromotionVerified == true and
  .app.bucket == "teamagent-dev-raw-files" and
  .app.key == "codebuild/connect-web-app.html" and
  (.app.versionId | type) == "string" and
  (.app.versionId | length) > 0 and
  (.app.anchors | keys | sort) ==
    (["appHtmlSha256","buildInputsSha256","dataSha256","manifestSha256"] | sort) and
  (.app.anchors | all(. | hex256)) and
  .deployment.intent == "ecs-update-service" and
  (.deployment.planDigest | sha256) and
  .deployment.currentTaskDefinitionArn == $currentArn and
  .deployment.previousTaskDefinitionArn == $currentArn and
  .deployment.currentTaskDefinitionSha256 == $currentSha and
  (.deployment.registrationPayloadSha256 | hex256) and
  .deployment.circuitBreakerRollback == true and
  .deployment.postStableCanaryRequired == true
' "$tmp_dir/inspection.json" >/dev/null || \
  fail "trusted verifier returned an incomplete or retargeted receipt"

NEW_IMAGE=$(jq -er '.release.runtimeRef' "$tmp_dir/inspection.json")
jq --arg image "$NEW_IMAGE" -f "$TASK_FILTER" \
  "$tmp_dir/current-task.json" >"$tmp_dir/register-task.json"
REGISTER_TASK_SHA256=$(canonical_sha256 "$tmp_dir/register-task.json")
[[ "$REGISTER_TASK_SHA256" == \
  "$(jq -er '.deployment.registrationPayloadSha256' "$tmp_dir/inspection.json")" ]] || \
  fail "rendered task does not match the signed deployment plan"

"$TRUSTED_CLI" verify-deployment-plan \
  --profile "$TRUST_PROFILE" \
  --contract "$TRUST_CONTRACT" \
  --receipt "$RECEIPT_PATH" \
  --inspection "$tmp_dir/inspection.json" \
  --current-task "$tmp_dir/current-task-canonical.json" \
  --rendered-task "$tmp_dir/register-task.json" \
  --output "$tmp_dir/verified-plan.json"
jq -e \
  --arg receiptId "$(jq -er '.receipt.id' "$tmp_dir/inspection.json")" \
  --arg currentArn "$CURRENT_TASK_ARN" \
  --arg currentSha "$CURRENT_TASK_SHA256" \
  --arg renderedSha "$REGISTER_TASK_SHA256" '
  .schemaVersion == 1 and
  .verified == true and
  .kmsSignatureVerified == true and
  .fresh == true and
  .unconsumed == true and
  .receiptId == $receiptId and
  .currentTaskDefinitionArn == $currentArn and
  .currentTaskDefinitionSha256 == $currentSha and
  .registrationPayloadSha256 == $renderedSha
' "$tmp_dir/verified-plan.json" >/dev/null || fail "trusted deployment plan verification failed"

if [[ "$MODE" == render ]]; then
  jq . "$tmp_dir/register-task.json"
  exit 0
fi

NEW_TD=$(aws ecs register-task-definition \
  --region "$AWS_REGION" \
  --cli-input-json "file://$tmp_dir/register-task.json" \
  --query taskDefinition.taskDefinitionArn \
  --output text)
[[ "$NEW_TD" =~ ^arn:aws:ecs:ap-northeast-1:718959508629:task-definition/teamagent-dev-openclaw:[1-9][0-9]*$ ]] || \
  fail "register-task-definition returned an unexpected ARN"

# Re-read the service immediately before atomic consumption.  A concurrent
# rollout invalidates the receipt instead of silently changing rollback state.
aws ecs describe-services \
  --region "$AWS_REGION" \
  --cluster "$ECS_CLUSTER" \
  --services "$ECS_SERVICE" \
  --output json >"$tmp_dir/preconsume-service.json"
PRECONSUME_TASK_ARN=$(jq -er '.services[0].taskDefinition' "$tmp_dir/preconsume-service.json")
[[ "$PRECONSUME_TASK_ARN" == "$CURRENT_TASK_ARN" ]] || \
  fail "service task definition changed after receipt verification"

"$TRUSTED_CLI" consume-deployment-receipt \
  --profile "$TRUST_PROFILE" \
  --contract "$TRUST_CONTRACT" \
  --receipt "$RECEIPT_PATH" \
  --verified-plan "$tmp_dir/verified-plan.json" \
  --current-task-definition-arn "$CURRENT_TASK_ARN" \
  --new-task-definition-arn "$NEW_TD" \
  --output "$tmp_dir/consumption.json"
jq -e \
  --arg receiptId "$(jq -er '.receipt.id' "$tmp_dir/inspection.json")" \
  --arg previous "$CURRENT_TASK_ARN" \
  --arg next "$NEW_TD" '
  .schemaVersion == 1 and
  .receiptId == $receiptId and
  .verified == true and
  .consumed == true and
  .atomic == true and
  .previousTaskDefinitionArn == $previous and
  .newTaskDefinitionArn == $next and
  (.durableReceipt.uri |
    test("^s3://teamagent-dev-raw-files/trusted-release/openclaw/deployment-receipts/")) and
  (.durableReceipt.versionId | type) == "string" and
  (.durableReceipt.versionId | length) > 0 and
  (.durableReceipt.sha256 | test("^[0-9a-f]{64}$"))
' "$tmp_dir/consumption.json" >/dev/null || \
  fail "receipt was not atomically consumed with durable rollback state"

alarm_and_restore() {
  local reason=$1
  set +e
  aws cloudwatch put-metric-data \
    --region "$AWS_REGION" \
    --namespace "$METRIC_NAMESPACE" \
    --metric-data "MetricName=OpenClawRolloutGateFailure,Value=1,Unit=Count" >/dev/null
  aws ecs update-service \
    --region "$AWS_REGION" \
    --cluster "$ECS_CLUSTER" \
    --service "$ECS_SERVICE" \
    --task-definition "$CURRENT_TASK_ARN" \
    --deployment-configuration 'deploymentCircuitBreaker={enable=true,rollback=true}' >/dev/null
  aws ecs wait services-stable \
    --region "$AWS_REGION" \
    --cluster "$ECS_CLUSTER" \
    --services "$ECS_SERVICE"
  local rollback_status=$?
  set -e
  if ((rollback_status != 0)); then
    fail "$reason; automatic rollback to $CURRENT_TASK_ARN also failed"
  fi
  fail "$reason; restored durable previous task $CURRENT_TASK_ARN"
}

# No command that can mutate deployment state occurs between atomic receipt
# consumption and this update-service call.
if ! aws ecs update-service \
  --region "$AWS_REGION" \
  --cluster "$ECS_CLUSTER" \
  --service "$ECS_SERVICE" \
  --task-definition "$NEW_TD" \
  --deployment-configuration 'deploymentCircuitBreaker={enable=true,rollback=true}' \
  >/dev/null; then
  alarm_and_restore "ECS update-service failed"
fi
if ! aws ecs wait services-stable \
  --region "$AWS_REGION" \
  --cluster "$ECS_CLUSTER" \
  --services "$ECS_SERVICE"; then
  alarm_and_restore "ECS deployment did not reach services-stable"
fi

if ! node "$ROLLOUT_GATE" \
  --new-task-definition "$NEW_TD" \
  --previous-task-definition "$CURRENT_TASK_ARN" \
  --receipt-consumption "$tmp_dir/consumption.json" \
  --output "$tmp_dir/rollout-gates.json"; then
  alarm_and_restore "post-stable Slack/Bedrock/MCP rollout gates failed"
fi
jq -e \
  --arg receiptId "$(jq -er '.receiptId' "$tmp_dir/consumption.json")" \
  --arg previous "$CURRENT_TASK_ARN" \
  --arg next "$NEW_TD" '
  .schemaVersion == 1 and
  .receiptId == $receiptId and
  .account == "718959508629" and
  .region == "ap-northeast-1" and
  .cluster == "teamagent-dev" and
  .service == "teamagent-dev-openclaw" and
  .taskFamily == "teamagent-dev-openclaw" and
  .previousTaskDefinitionArn == $previous and
  .newTaskDefinitionArn == $next and
  .ecsServiceStable == true and
  .circuitBreakerRollbackEnabled == true and
  .oneOffTask.exactTaskDefinition == true and
  .oneOffTask.exitCode == 0 and
  .mcp.toolCount == 12 and
  (.mcp.toolNamesSha256 | test("^[0-9a-f]{64}$")) and
  .bedrock.request == "Converse" and
  .bedrock.passed == true and
  (.bedrock.credentialSource |
    test("^ECS_CONTAINER_CREDENTIALS_(RELATIVE|FULL)_URI$")) and
  .slack.connected == true and
  .slack.mentionReplyExact == true and
  .passed == true
' "$tmp_dir/rollout-gates.json" >/dev/null || \
  alarm_and_restore "post-stable rollout evidence is incomplete"

if ! "$TRUSTED_CLI" record-deployment-result \
  --profile "$TRUST_PROFILE" \
  --contract "$TRUST_CONTRACT" \
  --receipt "$RECEIPT_PATH" \
  --consumption "$tmp_dir/consumption.json" \
  --rollout-result "$tmp_dir/rollout-gates.json" \
  --output "$tmp_dir/durable-rollout-result.json"; then
  alarm_and_restore "trusted framework could not durably record rollout gates"
fi
jq -e \
  --arg receiptId "$(jq -er '.receiptId' "$tmp_dir/consumption.json")" \
  --arg previous "$CURRENT_TASK_ARN" \
  --arg next "$NEW_TD" '
  .schemaVersion == 1 and
  .verified == true and
  .signed == true and
  .durable == true and
  .receiptId == $receiptId and
  .previousTaskDefinitionArn == $previous and
  .newTaskDefinitionArn == $next and
  .rolloutPassed == true and
  (.rolloutEvidenceSha256 | test("^[0-9a-f]{64}$")) and
  (.durableRecord.uri |
    test("^s3://teamagent-dev-raw-files/trusted-release/openclaw/deployment-results/")) and
  (.durableRecord.versionId | type) == "string" and
  (.durableRecord.versionId | length) > 0 and
  (.durableRecord.sha256 | test("^[0-9a-f]{64}$"))
' "$tmp_dir/durable-rollout-result.json" >/dev/null || \
  alarm_and_restore "trusted rollout result record is incomplete"

echo "[openclaw-deploy] PASS task=$NEW_TD receipt=$(jq -r '.receiptId' "$tmp_dir/consumption.json")"
