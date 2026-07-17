#!/usr/bin/env bash
# OpenClaw の唯一の production deploy path。
# 検証済み release manifest から digest image を取り出し、現 task definition
# の IAM/env/secrets/logs を保持したまま runtime hardening を毎回強制する。
set -Eeuo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd -P)
TASK_FILTER="$REPO_ROOT/infra/openclaw/harden-task-definition.jq"

usage() {
  cat >&2 <<'USAGE'
usage:
  apply_openclaw.sh <release-manifest.json>
  apply_openclaw.sh --render-only <current-task-definition.json> <release-manifest.json>

The normal mode registers a hardened task revision and rolls the existing ECS
service. --render-only performs no AWS operation and writes the registration
payload to stdout for review/tests.
USAGE
}

fail() {
  echo "[openclaw-deploy] FATAL: $*" >&2
  exit 1
}

for command in jq sha256sum; do
  command -v "$command" >/dev/null || fail "required command not found: $command"
done
[[ -f "$TASK_FILTER" && ! -L "$TASK_FILTER" ]] || fail "task hardening filter is missing or a symlink"

MODE=deploy
CURRENT_TASK_PATH=""
if [[ ${1:-} == "--render-only" ]]; then
  (($# == 3)) || { usage; exit 2; }
  MODE=render
  CURRENT_TASK_PATH=$2
  MANIFEST_PATH=$3
else
  (($# == 1)) || { usage; exit 2; }
  MANIFEST_PATH=$1
fi

[[ -f "$MANIFEST_PATH" && ! -L "$MANIFEST_PATH" ]] || \
  fail "release manifest is missing or a symlink"
CHECKSUM_PATH="$MANIFEST_PATH.sha256"
[[ -f "$CHECKSUM_PATH" && ! -L "$CHECKSUM_PATH" ]] || \
  fail "release manifest checksum is missing or a symlink"
expected_manifest_sha=$(awk 'NR == 1 {print $1}' "$CHECKSUM_PATH")
[[ "$expected_manifest_sha" =~ ^[0-9a-f]{64}$ ]] || fail "invalid manifest checksum file"
actual_manifest_sha=$(sha256sum "$MANIFEST_PATH" | cut -d' ' -f1)
[[ "$actual_manifest_sha" == "$expected_manifest_sha" ]] || fail "manifest checksum mismatch"

jq -e '
  .schemaVersion == 3 and
  (.image.runtimeRef | test(
    "^[0-9]+\\.dkr\\.ecr\\.[a-z0-9-]+\\.amazonaws\\.com/[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}$"
  )) and
  .image.runtimeRef == (
    (.image.requested | sub(":[^/:]+$"; "")) + "@" + .image.manifestDigest
  ) and
  .runtime.platform == "linux/arm64" and
  .runtime.uid == 65532 and .runtime.gid == 65532 and
  .runtime.actualImageContractPassed == true and
  .runtime.forbiddenPackageOrPluginArtifacts == 0 and
  .runtime.developmentPayloadArtifacts == 0 and
  .runtime.browserReachabilityValidated == true and
  .runtime.controlUiImportClosureValidated == true and
  .runtime.controlUiHttpAssetClosureValidated == true and
  .scan.critical == 0 and .scan.high == 0 and .scan.secrets == 0 and
  .sbom.physicalNpmMultisetExactMatch == true and
  (.sbom.format | test("^CycloneDX 1\\.[0-9]+$")) and
  .buildAttestations.registryPublished == true and
  .buildAttestations.subjectValidated == true and
  .buildAttestations.sourceValidated == true and
  .buildAttestations.builderValidated == true and
  (.buildAttestations.builderId | test("^https://"))
' "$MANIFEST_PATH" >/dev/null || fail "release manifest is not deployable"

NEW_IMAGE=$(jq -er '.image.runtimeRef' "$MANIFEST_PATH")
tmp_dir=$(mktemp -d /tmp/openclaw-deploy.XXXXXX)
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

if [[ "$MODE" == render ]]; then
  [[ -f "$CURRENT_TASK_PATH" && ! -L "$CURRENT_TASK_PATH" ]] || \
    fail "current task definition is missing or a symlink"
  jq --arg image "$NEW_IMAGE" -f "$TASK_FILTER" "$CURRENT_TASK_PATH"
  exit 0
fi

command -v aws >/dev/null || fail "required command not found: aws"
R=${AWS_REGION:-ap-northeast-1}
CLUSTER=${OPENCLAW_ECS_CLUSTER:-teamagent-dev}
SVC=${OPENCLAW_ECS_SERVICE:-teamagent-dev-openclaw}
FAMILY=${OPENCLAW_ECS_FAMILY:-teamagent-dev-openclaw}

echo "== 1) 現 task definition 取得（IAM/env/secrets/logs を保持）=="
aws ecs describe-task-definition --region "$R" --task-definition "$FAMILY" \
  --query taskDefinition --output json >"$tmp_dir/current-task.json"
echo "   現在: $(jq -r '.taskDefinitionArn' "$tmp_dir/current-task.json")"

echo "== 2) release digest と hardening を強制した revision を register =="
jq --arg image "$NEW_IMAGE" -f "$TASK_FILTER" \
  "$tmp_dir/current-task.json" >"$tmp_dir/register-task.json"
NEW_TD=$(aws ecs register-task-definition --region "$R" \
  --cli-input-json "file://$tmp_dir/register-task.json" \
  --query taskDefinition.taskDefinitionArn --output text)
[[ "$NEW_TD" == arn:aws:ecs:* ]] || fail "register-task-definition returned an invalid ARN"
echo "   new task def=$NEW_TD"

echo "== 3) rolling update -> services-stable =="
aws ecs update-service --region "$R" --cluster "$CLUSTER" --service "$SVC" \
  --task-definition "$NEW_TD" >/dev/null
aws ecs wait services-stable --region "$R" --cluster "$CLUSTER" --services "$SVC"
echo "== DONE: $NEW_TD / manifest_sha256=$actual_manifest_sha =="
