#!/usr/bin/env bash
# Guarded release authorization: fresh attestation -> active/rollback tag.
set -euo pipefail
umask 077

REGION="ap-northeast-1"
ACCOUNT_ID="718959508629"
EXPECTED_CALLER_ARN="arn:aws:iam::718959508629:user/teamagent-release-caller"
LAUNCHER_ROLE_ARN="arn:aws:iam::718959508629:role/teamagent-dev-release-launcher"
SESSION_NAME="teamagent-release-authorization"
EXPECTED_SESSION_ARN="arn:aws:sts::718959508629:assumed-role/teamagent-dev-release-launcher/teamagent-release-authorization"
EXPECTED_CONTROL_BRANCH="dev"
EXPECTED_CONTROL_ORIGIN="git@github.com:noirelumiere00/TeamAgent.git"
EVIDENCE_BUCKET="teamagent-dev-image-release-evidence"
EVIDENCE_KMS_ALIAS="alias/teamagent-dev-image-release-evidence"
ATTESTOR_SIGNING_KEY_ALIAS="alias/teamagent-dev-image-attestor"
ATTESTOR_PROJECT="teamagent-dev-image-attestor"
PROMOTER_PROJECT="teamagent-dev-image-promoter"
PIPELINE=""
CHANNEL=""
LOCATOR_KEY=""
LOCATOR_VERSION=""
LOCATOR_SIGNATURE_VERSION=""
POLL_SECONDS=15
TIMEOUT_SECONDS=7200

die() {
  echo "FATAL: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
usage: authorize_image_release.sh \
  --pipeline mcp|tiktok|openclaw \
  --channel active|rollback \
  --receipt-key KEY \
  --receipt-version-id VERSION \
  --receipt-signature-version-id VERSION

The input receipt is an unexpired signed locator for a previously verified candidate.
The source-free attestor rechecks the signed source plus the exact immutable
verified-candidate digest/referrers/signatures, then the source-free promoter
creates the active/rollback tag. It never depends on an expired quarantine copy.
This command does not run Terraform or update ECS, EventBridge, task definitions,
or services.
EOF
}

value() {
  [ "$#" -ge 2 ] && [ -n "${2-}" ] || die "$1 requires a value"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --pipeline) value "$@"; PIPELINE="$2"; shift 2 ;;
    --channel) value "$@"; CHANNEL="$2"; shift 2 ;;
    --receipt-key) value "$@"; LOCATOR_KEY="$2"; shift 2 ;;
    --receipt-version-id) value "$@"; LOCATOR_VERSION="$2"; shift 2 ;;
    --receipt-signature-version-id)
      value "$@"
      LOCATOR_SIGNATURE_VERSION="$2"
      shift 2
      ;;
    --poll-seconds) value "$@"; POLL_SECONDS="$2"; shift 2 ;;
    --timeout-seconds) value "$@"; TIMEOUT_SECONDS="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done
case "$PIPELINE" in mcp|tiktok|openclaw) ;; *) die "pipeline is not allowlisted" ;; esac
case "$CHANNEL" in active|rollback) ;; *) die "channel must be active or rollback" ;; esac
[[ "$LOCATOR_KEY" =~ ^release-receipts/$PIPELINE/[0-9a-f]{40}/[0-9a-f]{64}\.json$ ]] \
  || die "receipt key is not content addressed for the selected pipeline"
for version in "$LOCATOR_VERSION" "$LOCATOR_SIGNATURE_VERSION"; do
  case "$version" in ""|None|null|*[!A-Za-z0-9._~+/=-]*) die "invalid receipt VersionId" ;; esac
done
[[ "$POLL_SECONDS" =~ ^[1-9][0-9]*$ ]] || die "poll interval must be positive"
[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || die "timeout must be positive"
for tool in aws git jq python3 sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done

export AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true
unset AWS_ENDPOINT_URL AWS_ENDPOINT_URL_STS AWS_ENDPOINT_URL_S3 AWS_ENDPOINT_URL_ECR AWS_ENDPOINT_URL_KMS
while IFS= read -r AWS_ENDPOINT_VARIABLE; do
  unset "$AWS_ENDPOINT_VARIABLE"
done < <(compgen -A variable AWS_ENDPOINT_URL)
unset AWS_ENDPOINT_VARIABLE

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
CONTROL_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)" \
  || die "release launcher is not inside the TeamAgent worktree"
[ -z "$(git -C "$CONTROL_ROOT" status --porcelain=v1 --untracked-files=all --ignore-submodules=none)" ] \
  || die "TeamAgent control worktree is dirty"
[ "$(git -C "$CONTROL_ROOT" symbolic-ref --quiet --short HEAD)" = "$EXPECTED_CONTROL_BRANCH" ] \
  || die "release authorization must run from local dev"
[ "$(git -C "$CONTROL_ROOT" config --get remote.origin.url)" = "$EXPECTED_CONTROL_ORIGIN" ] \
  || die "TeamAgent origin is not allowlisted"
git -C "$CONTROL_ROOT" fetch --quiet --no-tags origin \
  "refs/heads/dev:refs/remotes/origin/dev" \
  || die "could not refresh origin/dev"
[ "$(git -C "$CONTROL_ROOT" rev-parse HEAD)" = "$(git -C "$CONTROL_ROOT" rev-parse refs/remotes/origin/dev)" ] \
  || die "local dev HEAD must exactly equal origin/dev"
EVIDENCE_HELPER="$CONTROL_ROOT/infra/codebuild/release_evidence.py"
case "$PIPELINE" in
  mcp) CONTRACT="$CONTROL_ROOT/infra/codebuild/teamagent_core_media_release_contract.json" ;;
  tiktok) CONTRACT="$CONTROL_ROOT/infra/codebuild/tiktok_release_contract.json" ;;
  openclaw) CONTRACT="$CONTROL_ROOT/infra/codebuild/openclaw_bundle_contract.json" ;;
esac
[ -f "$EVIDENCE_HELPER" ] && [ -f "$CONTRACT" ] || die "trusted release controls are missing"
CONTRACT_SHA256="$(sha256sum "$CONTRACT" | awk '{print $1}')"
python3 - "$CONTRACT" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    contract = json.load(handle)
if contract.get("release") != {"ready": True, "blocked_reason": ""}:
    raise SystemExit("FATAL: release.ready is false")
PY

identity() {
  AWS_PAGER="" aws sts get-caller-identity --query '[Account,Arn]' --output text
}
INITIAL="$(identity)"
IFS=$'\t' read -r INITIAL_ACCOUNT INITIAL_ARN EXTRA <<<"$INITIAL"
[ -z "${EXTRA:-}" ] && [ "$INITIAL_ACCOUNT" = "$ACCOUNT_ID" ] \
  && [ "$INITIAL_ARN" = "$EXPECTED_CALLER_ARN" ] \
  || die "release launcher must start as the exact dedicated caller"
unset INITIAL INITIAL_ACCOUNT INITIAL_ARN EXTRA
SESSION="$(
  AWS_PAGER="" aws sts assume-role \
    --region "$REGION" \
    --role-arn "$LAUNCHER_ROLE_ARN" \
    --role-session-name "$SESSION_NAME" \
    --duration-seconds 10800 \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken,Expiration]' \
    --output text
)" || die "could not assume the release launcher role"
IFS=$'\t' read -r AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN EXPIRATION EXTRA \
  <<<"$SESSION"
[ -z "${EXTRA:-}" ] || die "malformed release launcher credentials"
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
export AWS_DEFAULT_REGION="$REGION" AWS_REGION="$REGION"
export AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null
unset AWS_PROFILE AWS_DEFAULT_PROFILE SESSION EXPIRATION EXTRA
PINNED="$(identity)"
IFS=$'\t' read -r PINNED_ACCOUNT PINNED_ARN EXTRA <<<"$PINNED"
[ -z "${EXTRA:-}" ] && [ "$PINNED_ACCOUNT" = "$ACCOUNT_ID" ] \
  && [ "$PINNED_ARN" = "$EXPECTED_SESSION_ARN" ] \
  || die "unexpected pinned release launcher session"
unset PINNED PINNED_ACCOUNT PINNED_ARN EXTRA

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/teamagent-release.XXXXXXXX")"
trap 'rm -rf -- "$TMP_DIR"' EXIT
EVIDENCE_KMS_KEY_ARN="$(
  AWS_PAGER="" aws kms describe-key --region "$REGION" --key-id "$EVIDENCE_KMS_ALIAS" \
    --query KeyMetadata.Arn --output text
)"
ATTESTOR_SIGNING_KEY_ARN="$(
  AWS_PAGER="" aws kms describe-key --region "$REGION" --key-id "$ATTESTOR_SIGNING_KEY_ALIAS" \
    --query KeyMetadata.Arn --output text
)"
for item in "$EVIDENCE_KMS_KEY_ARN" "$ATTESTOR_SIGNING_KEY_ARN"; do
  [[ "$item" =~ ^arn:aws:kms:ap-northeast-1:718959508629:key/[0-9a-f-]{36}$ ]] \
    || die "release KMS key is outside the fixed account"
done
LOCATOR_SIGNATURE_KEY="$LOCATOR_KEY.sig"
for object in receipt signature; do
  if [ "$object" = "receipt" ]; then
    key="$LOCATOR_KEY"
    version_id="$LOCATOR_VERSION"
    destination="$TMP_DIR/locator.json"
  else
    key="$LOCATOR_SIGNATURE_KEY"
    version_id="$LOCATOR_SIGNATURE_VERSION"
    destination="$TMP_DIR/locator.sig"
  fi
  AWS_PAGER="" aws s3api head-object \
    --region "$REGION" \
    --bucket "$EVIDENCE_BUCKET" \
    --key "$key" \
    --version-id "$version_id" \
    --expected-bucket-owner "$ACCOUNT_ID" \
    --output json >"$TMP_DIR/$object-head.json"
  jq -e --arg version "$version_id" --arg kms "$EVIDENCE_KMS_KEY_ARN" '
    .VersionId == $version and
    .ObjectLockMode == "COMPLIANCE" and
    .ServerSideEncryption == "aws:kms" and
    .SSEKMSKeyId == $kms
  ' "$TMP_DIR/$object-head.json" >/dev/null \
    || die "locator $object is not exact immutable evidence"
  python3 - "$TMP_DIR/$object-head.json" <<'PY'
import datetime
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    retained = json.load(handle).get("ObjectLockRetainUntilDate", "")
try:
    timestamp = datetime.datetime.fromisoformat(retained.replace("Z", "+00:00"))
except ValueError as exc:
    raise SystemExit("FATAL: locator retention timestamp is invalid") from exc
if timestamp <= datetime.datetime.now(datetime.UTC):
    raise SystemExit("FATAL: locator immutable retention has expired")
PY
  downloaded="$(
    AWS_PAGER="" aws s3api get-object \
      --region "$REGION" \
      --bucket "$EVIDENCE_BUCKET" \
      --key "$key" \
      --version-id "$version_id" \
      --expected-bucket-owner "$ACCOUNT_ID" \
      --query VersionId \
      --output text \
      "$destination"
  )"
  [ "$downloaded" = "$version_id" ] || die "locator $object VersionId mismatch"
done
LOCATOR_KEY_SHA256="${LOCATOR_KEY%.json}"
LOCATOR_KEY_SHA256="${LOCATOR_KEY_SHA256##*/}"
[ "$(sha256sum "$TMP_DIR/locator.json" | awk '{print $1}')" = "$LOCATOR_KEY_SHA256" ] \
  || die "verified-candidate locator bytes do not match the content key"
python3 - "$TMP_DIR/locator.json" "$TMP_DIR/locator.sha256" <<'PY'
import hashlib
import sys
with open(sys.argv[1], "rb") as source:
    digest = hashlib.sha256(source.read()).digest()
with open(sys.argv[2], "wb") as output:
    output.write(digest)
PY
AWS_PAGER="" aws kms verify \
  --region "$REGION" \
  --key-id "$ATTESTOR_SIGNING_KEY_ARN" \
  --message-type DIGEST \
  --message "fileb://$TMP_DIR/locator.sha256" \
  --signature "fileb://$TMP_DIR/locator.sig" \
  --signing-algorithm RSASSA_PSS_SHA_256 \
  --output json >"$TMP_DIR/locator-verify.json"
[ "$(jq -er '.SignatureValid' "$TMP_DIR/locator-verify.json")" = "true" ] \
  || die "verified-candidate locator signature is invalid"
python3 "$EVIDENCE_HELPER" verify-release-locator \
  --receipt "$TMP_DIR/locator.json" \
  --expected-pipeline "$PIPELINE" \
  --expected-contract-sha256 "$CONTRACT_SHA256"

SOURCE_COMMIT="$(jq -er '.build.source_commit' "$TMP_DIR/locator.json")"
[[ "$LOCATOR_KEY" == "release-receipts/$PIPELINE/$SOURCE_COMMIT/"* ]] \
  || die "verified-candidate locator commit/key mismatch"
BUILD_ID="$(jq -er '.build.build_id' "$TMP_DIR/locator.json")"
SOURCE_BUCKET="$(jq -er '.source_evidence.bucket' "$TMP_DIR/locator.json")"
SOURCE_KEY="$(jq -er '.source_evidence.key' "$TMP_DIR/locator.json")"
SOURCE_VERSION="$(jq -er '.source_evidence.version_id' "$TMP_DIR/locator.json")"
SOURCE_SHA256="$(jq -er '.source_evidence.sha256' "$TMP_DIR/locator.json")"
SOURCE_SIGNATURE_KEY="$(jq -er '.source_evidence.signature_key' "$TMP_DIR/locator.json")"
SOURCE_SIGNATURE_VERSION="$(jq -er '.source_evidence.signature_version_id' "$TMP_DIR/locator.json")"
SUBJECTS_JSON="$(
  jq -c '[.subjects[] | {
    name,
    quarantine_repository,
    candidate_repository,
    release_repository,
    digest
  }]' "$TMP_DIR/locator.json"
)"

environment_json() {
  local output="$1"
  shift
  python3 - "$output" "$@" <<'PY'
import json
import sys
output, *pairs = sys.argv[1:]
values = []
for pair in pairs:
    name, value = pair.split("=", 1)
    values.append({"name": name, "value": value, "type": "PLAINTEXT"})
with open(output, "w", encoding="utf-8") as handle:
    json.dump(values, handle, sort_keys=True, separators=(",", ":"))
PY
}

start_build() {
  local project="$1"
  local env_file="$2"
  AWS_PAGER="" aws codebuild start-build \
    --region "$REGION" \
    --project-name "$project" \
    --environment-variables-override "file://$env_file" \
    --query build.id \
    --output text
}

wait_build() {
  local build_id="$1"
  local deadline=$((SECONDS + TIMEOUT_SECONDS))
  local response status
  while :; do
    response="$(
      AWS_PAGER="" aws codebuild batch-get-builds \
        --region "$REGION" --ids "$build_id" --output json
    )"
    status="$(jq -er '.builds | if length == 1 then .[0].buildStatus else error("ambiguous build") end' <<<"$response")"
    case "$status" in
      IN_PROGRESS)
        [ "$SECONDS" -lt "$deadline" ] || die "timed out waiting for $build_id"
        sleep "$POLL_SECONDS"
        ;;
      SUCCEEDED) return 0 ;;
      FAILED|FAULT|STOPPED|TIMED_OUT) die "build failed: $build_id ($status)" ;;
      *) die "unexpected build state: $status" ;;
    esac
  done
}

exported_value() {
  local build_id="$1"
  local name="$2"
  AWS_PAGER="" aws codebuild batch-get-builds \
    --region "$REGION" --ids "$build_id" --output json \
    | jq -er --arg name "$name" '
      [.builds[0].exportedEnvironmentVariables[] | select(.name == $name) | .value] |
      if length == 1 then .[0] else error("missing or duplicate export") end
    '
}

ATTESTOR_ENV="$TMP_DIR/attestor-env.json"
environment_json "$ATTESTOR_ENV" \
  "PIPELINE=$PIPELINE" \
  "PROMOTION_CHANNEL=$CHANNEL" \
  "SOURCE_COMMIT=$SOURCE_COMMIT" \
  "CONTRACT_SHA256=$CONTRACT_SHA256" \
  "SOURCE_EVIDENCE_BUCKET=$SOURCE_BUCKET" \
  "SOURCE_EVIDENCE_KEY=$SOURCE_KEY" \
  "SOURCE_EVIDENCE_VERSION_ID=$SOURCE_VERSION" \
  "SOURCE_EVIDENCE_SHA256=$SOURCE_SHA256" \
  "SOURCE_EVIDENCE_SIGNATURE_KEY=$SOURCE_SIGNATURE_KEY" \
  "SOURCE_EVIDENCE_SIGNATURE_VERSION_ID=$SOURCE_SIGNATURE_VERSION" \
  "BUILD_ID=$BUILD_ID" \
  "SUBJECTS_JSON=$SUBJECTS_JSON" \
  "CANDIDATE_RECEIPT_KEY=$LOCATOR_KEY" \
  "CANDIDATE_RECEIPT_VERSION_ID=$LOCATOR_VERSION" \
  "CANDIDATE_RECEIPT_SIGNATURE_KEY=$LOCATOR_SIGNATURE_KEY" \
  "CANDIDATE_RECEIPT_SIGNATURE_VERSION_ID=$LOCATOR_SIGNATURE_VERSION"
ATTESTOR_BUILD_ID="$(start_build "$ATTESTOR_PROJECT" "$ATTESTOR_ENV")"
[[ "$ATTESTOR_BUILD_ID" == "$ATTESTOR_PROJECT:"* ]] || die "invalid attestor build ID"
wait_build "$ATTESTOR_BUILD_ID"
NEW_RECEIPT_KEY="$(exported_value "$ATTESTOR_BUILD_ID" RECEIPT_KEY)"
NEW_RECEIPT_VERSION="$(exported_value "$ATTESTOR_BUILD_ID" RECEIPT_VERSION_ID)"
NEW_SIGNATURE_KEY="$(exported_value "$ATTESTOR_BUILD_ID" RECEIPT_SIGNATURE_KEY)"
NEW_SIGNATURE_VERSION="$(exported_value "$ATTESTOR_BUILD_ID" RECEIPT_SIGNATURE_VERSION_ID)"

PROMOTER_ENV="$TMP_DIR/promoter-env.json"
environment_json "$PROMOTER_ENV" \
  "PIPELINE=$PIPELINE" \
  "PROMOTION_CHANNEL=$CHANNEL" \
  "SOURCE_COMMIT=$SOURCE_COMMIT" \
  "CONTRACT_SHA256=$CONTRACT_SHA256" \
  "RECEIPT_KEY=$NEW_RECEIPT_KEY" \
  "RECEIPT_VERSION_ID=$NEW_RECEIPT_VERSION" \
  "RECEIPT_SIGNATURE_KEY=$NEW_SIGNATURE_KEY" \
  "RECEIPT_SIGNATURE_VERSION_ID=$NEW_SIGNATURE_VERSION"
PROMOTER_BUILD_ID="$(start_build "$PROMOTER_PROJECT" "$PROMOTER_ENV")"
[[ "$PROMOTER_BUILD_ID" == "$PROMOTER_PROJECT:"* ]] || die "invalid promoter build ID"
wait_build "$PROMOTER_BUILD_ID"

SUBJECT_COUNT="$(jq -er '.subjects | length' "$TMP_DIR/locator.json")"
while IFS= read -r subject; do
  name="$(jq -er '.name' <<<"$subject")"
  repository="$(jq -er '.release_repository' <<<"$subject")"
  expected_digest="$(jq -er '.digest' <<<"$subject")"
  suffix=""
  if [ "$SUBJECT_COUNT" -gt 1 ]; then
    suffix="-$name"
  fi
  release_tag="$CHANNEL-$SOURCE_COMMIT$suffix"
  actual_digest="$(
    AWS_PAGER="" aws ecr describe-images \
      --region "$REGION" \
      --registry-id "$ACCOUNT_ID" \
      --repository-name "$repository" \
      --image-ids "imageTag=$release_tag" \
      --query 'imageDetails[0].imageDigest' \
      --output text
  )"
  [ "$actual_digest" = "$expected_digest" ] \
    || die "release digest differs from the signed verified candidate"
done < <(jq -c '.subjects[]' "$TMP_DIR/locator.json")

echo "Guarded release authorization completed (no deployment performed):"
echo "  pipeline=$PIPELINE"
echo "  channel=$CHANNEL"
echo "  commit=$SOURCE_COMMIT"
echo "  receipt_key=$NEW_RECEIPT_KEY"
echo "  receipt_version_id=$NEW_RECEIPT_VERSION"
echo "  receipt_signature_key=$NEW_SIGNATURE_KEY"
echo "  receipt_signature_version_id=$NEW_SIGNATURE_VERSION"
echo "Set these exact image_release_evidence values and the release @sha256, then use:"
echo "  bash infra/deploy/terraform_runtime_guard.sh plan --var-file /secure/local/path/terraform.tfvars --out /secure/local/path/image-release.tfplan --runtime-migration MIGRATION_ID [required receipts]"
echo "  bash infra/deploy/terraform_runtime_guard.sh apply --plan /secure/local/path/image-release.tfplan --out /secure/local/path/image-release.apply.json"
echo "The saved plan, deployment intent, and exact receipt versions authorize at most one deployment."
