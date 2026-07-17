#!/usr/bin/env bash
# Build-only trusted publisher for the isolated OpenClaw core/media project.
# It publishes immutable signed evidence, starts one fixed CodeBuild project,
# verifies the promoted release digests/referrers, and never mutates deployment.
set -euo pipefail
umask 077

REGION="ap-northeast-1"
EXPECTED_ACCOUNT_ID="718959508629"
EXPECTED_CALLER_ARN="arn:aws:iam::718959508629:user/AIIAdev"
PUBLISHER_ROLE_ARN="arn:aws:iam::718959508629:role/teamagent-dev-openclaw-build-publisher"
PUBLISHER_SESSION_NAME="openclaw-build-publisher"
EXPECTED_SESSION_ARN="arn:aws:sts::718959508629:assumed-role/teamagent-dev-openclaw-build-publisher/openclaw-build-publisher"
EXPECTED_BRANCH="dev"
EXPECTED_ORIGIN_URL="git@github.com:noirelumiere00/TeamAgent.git"
CODEBUILD_PROJECT="teamagent-dev-openclaw-provenance-builder"
EVIDENCE_BUCKET="teamagent-dev-openclaw-build-evidence"
SIGNING_KMS_KEY_ALIAS="alias/teamagent-dev-openclaw-build-publisher"
EVIDENCE_KMS_KEY_ALIAS="alias/teamagent-dev-openclaw-build-evidence"
CORE_QUARANTINE_REPOSITORY="teamagent-openclaw-quarantine"
CORE_RELEASE_REPOSITORY="teamagent-openclaw"
MEDIA_QUARANTINE_REPOSITORY="teamagent-openclaw-media-quarantine"
MEDIA_RELEASE_REPOSITORY="teamagent-openclaw-media"
POLL_SECONDS=15
TIMEOUT_SECONDS=7200

usage() {
  cat <<'EOF'
usage: build_openclaw_image.sh [options]

Options:
  --poll-seconds <seconds>       Build status polling interval (default: 15)
  --timeout-seconds <seconds>    Overall CodeBuild wait timeout (default: 7200)
  -h, --help                     Show this help

The core/media tags are derived from the exact remote dev commit. This command
is build-only: it never updates ECS, task definitions, schedules, or services.
EOF
}

die() {
  echo "FATAL: $*" >&2
  exit 1
}

require_value() {
  local option="$1"
  local value="${2-}"
  [ -n "$value" ] || die "$option requires a value"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --poll-seconds)
      require_value "$1" "${2-}"
      POLL_SECONDS="$2"
      shift 2
      ;;
    --timeout-seconds)
      require_value "$1" "${2-}"
      TIMEOUT_SECONDS="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown argument: $1"
      ;;
  esac
done

[[ "$POLL_SECONDS" =~ ^[1-9][0-9]*$ ]] || die "--poll-seconds must be positive"
[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || die "--timeout-seconds must be positive"
[ "$TIMEOUT_SECONDS" -ge "$POLL_SECONDS" ] || die "timeout must cover one poll interval"

for tool in git python3 aws jq curl cmp; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done

export AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true
unset AWS_ENDPOINT_URL AWS_ENDPOINT_URL_STS AWS_ENDPOINT_URL_S3 AWS_ENDPOINT_URL_ECR AWS_ENDPOINT_URL_KMS

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)" \
  || die "script is not inside a Git worktree"
PROVENANCE="$REPO_ROOT/infra/codebuild/openclaw_provenance.py"
BUNDLE_CONTRACT="$REPO_ROOT/infra/codebuild/openclaw_bundle_contract.json"
[ -f "$PROVENANCE" ] || die "OpenClaw provenance verifier is missing"
[ -f "$BUNDLE_CONTRACT" ] || die "OpenClaw bundle contract is missing"

if [ -n "$(git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=all --ignore-submodules=none)" ]; then
  die "Git worktree is dirty (tracked or untracked changes); commit or remove them first"
fi
COMMIT="$(git -C "$REPO_ROOT" rev-parse --verify HEAD^{commit})"
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || die "HEAD is not a full SHA-1 commit"
BRANCH="$(git -C "$REPO_ROOT" symbolic-ref --quiet --short HEAD)" \
  || die "detached HEAD is not allowed"
[ "$BRANCH" = "$EXPECTED_BRANCH" ] || die "OpenClaw builds must run from local dev"
ORIGIN_URL="$(git -C "$REPO_ROOT" config --get remote.origin.url)" \
  || die "Git remote origin is missing"
[ "$ORIGIN_URL" = "$EXPECTED_ORIGIN_URL" ] || die "unexpected Git origin URL"
git -C "$REPO_ROOT" fetch --quiet --no-tags origin \
  "refs/heads/dev:refs/remotes/origin/dev" \
  || die "could not refresh origin/dev"
REMOTE_DEV_COMMIT="$(git -C "$REPO_ROOT" rev-parse --verify refs/remotes/origin/dev^{commit})" \
  || die "origin/dev is missing"
[ "$COMMIT" = "$REMOTE_DEV_COMMIT" ] || die "local dev HEAD must exactly equal origin/dev"
unset ORIGIN_URL REMOTE_DEV_COMMIT

BUNDLE_CONTRACT_SHA256="$(
  python3 "$PROVENANCE" contract-sha256 --contract "$BUNDLE_CONTRACT"
)" || die "OpenClaw bundle contract is invalid"
[[ "$BUNDLE_CONTRACT_SHA256" =~ ^[0-9a-f]{64}$ ]] \
  || die "OpenClaw bundle contract returned an invalid SHA-256"
python3 "$PROVENANCE" assert-release-ready --contract "$BUNDLE_CONTRACT" \
  || die "OpenClaw core/media contract is not approved for release"

# The blocked contract above intentionally prevents every AWS call until the
# Boyle-owned core/media interfaces and complete signed receipt have landed.
INITIAL_IDENTITY="$(
  AWS_PAGER="" aws sts get-caller-identity --query '[Account,Arn]' --output text
)"
[[ "$INITIAL_IDENTITY" != *$'\n'* && "$INITIAL_IDENTITY" != *$'\r'* ]] \
  || die "AWS returned a malformed initial identity"
IFS=$'\t' read -r ACTUAL_ACCOUNT_ID ACTUAL_CALLER_ARN EXTRA_IDENTITY <<<"$INITIAL_IDENTITY"
[ -z "${EXTRA_IDENTITY:-}" ] || die "AWS returned a malformed initial identity"
[ "$ACTUAL_ACCOUNT_ID" = "$EXPECTED_ACCOUNT_ID" ] || die "refusing the wrong AWS account"
[ "$ACTUAL_CALLER_ARN" = "$EXPECTED_CALLER_ARN" ] \
  || die "publisher must start as $EXPECTED_CALLER_ARN"
unset INITIAL_IDENTITY ACTUAL_ACCOUNT_ID ACTUAL_CALLER_ARN EXTRA_IDENTITY

SESSION_CREDENTIALS="$(
  AWS_PAGER="" aws sts assume-role \
    --region "$REGION" \
    --role-arn "$PUBLISHER_ROLE_ARN" \
    --role-session-name "$PUBLISHER_SESSION_NAME" \
    --duration-seconds 10800 \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken,Expiration]' \
    --output text
)" || die "could not assume the dedicated OpenClaw publisher role"
[[ "$SESSION_CREDENTIALS" != *$'\n'* && "$SESSION_CREDENTIALS" != *$'\r'* ]] \
  || die "STS returned malformed publisher credentials"
IFS=$'\t' read -r \
  AWS_ACCESS_KEY_ID \
  AWS_SECRET_ACCESS_KEY \
  AWS_SESSION_TOKEN \
  AWS_CREDENTIAL_EXPIRATION \
  EXTRA_CREDENTIAL <<<"$SESSION_CREDENTIALS"
[ -z "${EXTRA_CREDENTIAL:-}" ] || die "STS returned malformed publisher credentials"
for credential in \
  "$AWS_ACCESS_KEY_ID" \
  "$AWS_SECRET_ACCESS_KEY" \
  "$AWS_SESSION_TOKEN" \
  "$AWS_CREDENTIAL_EXPIRATION"; do
  [ -n "$credential" ] && [ "$credential" != "None" ] \
    || die "STS returned incomplete publisher credentials"
done
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
export AWS_DEFAULT_REGION="$REGION" AWS_REGION="$REGION"
export AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null
unset AWS_PROFILE AWS_DEFAULT_PROFILE SESSION_CREDENTIALS EXTRA_CREDENTIAL credential

SESSION_IDENTITY="$(
  AWS_PAGER="" aws sts get-caller-identity --query '[Account,Arn]' --output text
)"
[[ "$SESSION_IDENTITY" != *$'\n'* && "$SESSION_IDENTITY" != *$'\r'* ]] \
  || die "AWS returned a malformed publisher role identity"
IFS=$'\t' read -r SESSION_ACCOUNT_ID SESSION_ARN EXTRA_SESSION_IDENTITY <<<"$SESSION_IDENTITY"
[ -z "${EXTRA_SESSION_IDENTITY:-}" ] || die "AWS returned malformed role identity"
[ "$SESSION_ACCOUNT_ID" = "$EXPECTED_ACCOUNT_ID" ] || die "publisher session account mismatch"
[ "$SESSION_ARN" = "$EXPECTED_SESSION_ARN" ] || die "unexpected publisher session ARN"
unset SESSION_IDENTITY SESSION_ACCOUNT_ID SESSION_ARN EXTRA_SESSION_IDENTITY

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/teamagent-openclaw-build.XXXXXXXX")"
cleanup() {
  rm -rf -- "$TMP_DIR"
}
trap cleanup EXIT

SIGNING_KMS_KEY_ARN="$(
  AWS_PAGER="" aws kms describe-key \
    --region "$REGION" \
    --key-id "$SIGNING_KMS_KEY_ALIAS" \
    --query KeyMetadata.Arn \
    --output text
)"
EVIDENCE_KMS_KEY_ARN="$(
  AWS_PAGER="" aws kms describe-key \
    --region "$REGION" \
    --key-id "$EVIDENCE_KMS_KEY_ALIAS" \
    --query KeyMetadata.Arn \
    --output text
)"
[[ "$SIGNING_KMS_KEY_ARN" =~ ^arn:aws:kms:ap-northeast-1:718959508629:key/ ]] \
  || die "unexpected OpenClaw signing KMS key"
[[ "$EVIDENCE_KMS_KEY_ARN" =~ ^arn:aws:kms:ap-northeast-1:718959508629:key/ ]] \
  || die "unexpected OpenClaw evidence KMS key"

BUCKET_VERSIONING="$(
  AWS_PAGER="" aws s3api get-bucket-versioning \
    --region "$REGION" \
    --bucket "$EVIDENCE_BUCKET" \
    --expected-bucket-owner "$EXPECTED_ACCOUNT_ID" \
    --query Status \
    --output text
)"
[ "$BUCKET_VERSIONING" = "Enabled" ] || die "evidence bucket versioning must be Enabled"
OBJECT_LOCK="$(
  AWS_PAGER="" aws s3api get-object-lock-configuration \
    --region "$REGION" \
    --bucket "$EVIDENCE_BUCKET" \
    --expected-bucket-owner "$EXPECTED_ACCOUNT_ID" \
    --query '[ObjectLockEnabled,Rule.DefaultRetention.Mode,Rule.DefaultRetention.Days]' \
    --output text
)"
[[ "$OBJECT_LOCK" != *$'\n'* && "$OBJECT_LOCK" != *$'\r'* ]] \
  || die "malformed evidence Object Lock configuration"
IFS=$'\t' read -r LOCK_ENABLED LOCK_MODE LOCK_DAYS EXTRA_LOCK <<<"$OBJECT_LOCK"
[ -z "${EXTRA_LOCK:-}" ] || die "malformed evidence Object Lock configuration"
[ "$LOCK_ENABLED" = "Enabled" ] && [ "$LOCK_MODE" = "COMPLIANCE" ] && [ "$LOCK_DAYS" = "30" ] \
  || die "evidence bucket must use 30-day COMPLIANCE Object Lock"
unset OBJECT_LOCK LOCK_ENABLED LOCK_MODE LOCK_DAYS EXTRA_LOCK

RETAIN_UNTIL="$(python3 - <<'PY'
import datetime

value = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=30, minutes=5)
print(value.isoformat(timespec="seconds").replace("+00:00", "Z"))
PY
)"

file_sha256() {
  python3 - "$1" <<'PY'
import hashlib
import sys

digest = hashlib.sha256()
with open(sys.argv[1], "rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
print(digest.hexdigest())
PY
}

write_digest() {
  python3 - "$1" "$2" <<'PY'
import hashlib
import sys

with open(sys.argv[1], "rb") as source:
    digest = hashlib.sha256(source.read()).digest()
with open(sys.argv[2], "wb") as output:
    output.write(digest)
PY
}

sign_file() {
  local body="$1"
  local signature="$2"
  local digest_file="$TMP_DIR/signing-digest.bin"
  local response="$TMP_DIR/kms-sign.json"
  write_digest "$body" "$digest_file"
  AWS_PAGER="" aws kms sign \
    --region "$REGION" \
    --key-id "$SIGNING_KMS_KEY_ARN" \
    --message-type DIGEST \
    --message "fileb://$digest_file" \
    --signing-algorithm RSASSA_PKCS1_V1_5_SHA_256 \
    --output json > "$response"
  python3 - "$response" "$signature" <<'PY'
import base64
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    value = json.load(source)
with open(sys.argv[2], "wb") as output:
    output.write(base64.b64decode(value["Signature"], validate=True))
PY
}

verify_signature() {
  local body="$1"
  local signature="$2"
  local digest_file="$TMP_DIR/verification-digest.bin"
  write_digest "$body" "$digest_file"
  [ "$(AWS_PAGER="" aws kms verify \
    --region "$REGION" \
    --key-id "$SIGNING_KMS_KEY_ARN" \
    --message-type DIGEST \
    --message "fileb://$digest_file" \
    --signature "fileb://$signature" \
    --signing-algorithm RSASSA_PKCS1_V1_5_SHA_256 \
    --query SignatureValid \
    --output text)" = "True" ] || die "KMS signature verification failed"
}

publish_or_verify_object() {
  local body="$1"
  local key="$2"
  local content_type="$3"
  local key_id
  local head_json
  local downloaded
  local get_json
  local version_id
  key_id="$(python3 - "$key" <<'PY'
import hashlib
import sys

print(hashlib.sha256(sys.argv[1].encode()).hexdigest())
PY
)"
  head_json="$TMP_DIR/$key_id-head.json"
  downloaded="$TMP_DIR/$key_id-downloaded"
  get_json="$TMP_DIR/$key_id-get.json"
  if ! AWS_PAGER="" aws s3api head-object \
    --region "$REGION" \
    --bucket "$EVIDENCE_BUCKET" \
    --key "$key" \
    --expected-bucket-owner "$EXPECTED_ACCOUNT_ID" \
    --output json > "$head_json" 2>/dev/null; then
    AWS_PAGER="" aws s3api put-object \
      --region "$REGION" \
      --bucket "$EVIDENCE_BUCKET" \
      --key "$key" \
      --body "$body" \
      --content-type "$content_type" \
      --server-side-encryption aws:kms \
      --ssekms-key-id "$EVIDENCE_KMS_KEY_ARN" \
      --object-lock-mode COMPLIANCE \
      --object-lock-retain-until-date "$RETAIN_UNTIL" \
      --expected-bucket-owner "$EXPECTED_ACCOUNT_ID" \
      --if-none-match '*' \
      --output json > "$TMP_DIR/$key_id-put.json"
    AWS_PAGER="" aws s3api head-object \
      --region "$REGION" \
      --bucket "$EVIDENCE_BUCKET" \
      --key "$key" \
      --expected-bucket-owner "$EXPECTED_ACCOUNT_ID" \
      --output json > "$head_json"
  fi
  python3 - "$head_json" "$EVIDENCE_KMS_KEY_ARN" <<'PY'
import datetime
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    value = json.load(source)
if value.get("ObjectLockMode") != "COMPLIANCE":
    raise SystemExit("object is not COMPLIANCE locked")
if value.get("ServerSideEncryption") != "aws:kms":
    raise SystemExit("object is not SSE-KMS encrypted")
if value.get("SSEKMSKeyId") != sys.argv[2]:
    raise SystemExit("object uses the wrong evidence KMS key")
if value.get("VersionId") in {None, "", "None", "null"}:
    raise SystemExit("object has no usable VersionId")
retained = datetime.datetime.fromisoformat(
    value["ObjectLockRetainUntilDate"].replace("Z", "+00:00")
)
if retained <= datetime.datetime.now(datetime.UTC):
    raise SystemExit("object retention has expired")
PY
  version_id="$(jq -er '.VersionId' "$head_json")"
  AWS_PAGER="" aws s3api get-object \
    --region "$REGION" \
    --bucket "$EVIDENCE_BUCKET" \
    --key "$key" \
    --version-id "$version_id" \
    --expected-bucket-owner "$EXPECTED_ACCOUNT_ID" \
    "$downloaded" > "$get_json"
  [ "$(jq -er '.VersionId' "$get_json")" = "$version_id" ] \
    || die "downloaded evidence VersionId mismatch"
  cmp -s "$body" "$downloaded" || die "immutable evidence bytes differ at $key"
  printf '%s\n' "$version_id"
}

SOURCE_MANIFEST="$TMP_DIR/openclaw-source-manifest.json"
SOURCE_SIGNATURE="$TMP_DIR/openclaw-source-manifest.sig"
python3 "$PROVENANCE" create-source-manifest \
  --repo-root "$REPO_ROOT" \
  --commit "$COMMIT" \
  --contract "$BUNDLE_CONTRACT" \
  --output "$SOURCE_MANIFEST"
SOURCE_MANIFEST_SHA256="$(file_sha256 "$SOURCE_MANIFEST")"
SOURCE_MANIFEST_KEY="source-manifests/$COMMIT/$SOURCE_MANIFEST_SHA256.json"
SOURCE_SIGNATURE_KEY="$SOURCE_MANIFEST_KEY.sig"
sign_file "$SOURCE_MANIFEST" "$SOURCE_SIGNATURE"
verify_signature "$SOURCE_MANIFEST" "$SOURCE_SIGNATURE"
SOURCE_SIGNATURE_VERSION="$(
  publish_or_verify_object "$SOURCE_SIGNATURE" "$SOURCE_SIGNATURE_KEY" application/octet-stream
)"
SOURCE_MANIFEST_VERSION="$(
  publish_or_verify_object "$SOURCE_MANIFEST" "$SOURCE_MANIFEST_KEY" application/json
)"

BUILD_ID="$(
  AWS_PAGER="" aws codebuild start-build \
    --region "$REGION" \
    --project-name "$CODEBUILD_PROJECT" \
    --source-version "$COMMIT" \
    --query build.id \
    --output text
)"
[[ "$BUILD_ID" =~ ^teamagent-dev-openclaw-provenance-builder:[0-9a-f-]{36}$ ]] \
  || die "CodeBuild returned an invalid OpenClaw build ID"

DEADLINE=$((SECONDS + TIMEOUT_SECONDS))
BUILD_STATUS=""
BUILD_SOURCE_VERSION=""
BUILD_RESOLVED_SOURCE_VERSION=""
while :; do
  BUILD_STATE="$(
    AWS_PAGER="" aws codebuild batch-get-builds \
      --region "$REGION" \
      --ids "$BUILD_ID" \
      --query 'builds[0].[buildStatus,sourceVersion,resolvedSourceVersion]' \
      --output text
  )"
  [[ "$BUILD_STATE" != *$'\n'* && "$BUILD_STATE" != *$'\r'* ]] \
    || die "CodeBuild returned a multi-line build state"
  IFS=$'\t' read -r \
    BUILD_STATUS \
    BUILD_SOURCE_VERSION \
    BUILD_RESOLVED_SOURCE_VERSION \
    EXTRA_STATE <<<"$BUILD_STATE"
  [ -z "${EXTRA_STATE:-}" ] || die "CodeBuild returned an unexpected build state"
  [ "$BUILD_SOURCE_VERSION" = "$COMMIT" ] || die "CodeBuild sourceVersion mismatch"
  case "$BUILD_STATUS" in
    IN_PROGRESS)
      if [ -n "${BUILD_RESOLVED_SOURCE_VERSION:-}" ] \
        && [ "$BUILD_RESOLVED_SOURCE_VERSION" != "None" ] \
        && [ "$BUILD_RESOLVED_SOURCE_VERSION" != "$COMMIT" ]; then
        die "CodeBuild resolvedSourceVersion mismatch"
      fi
      [ "$SECONDS" -lt "$DEADLINE" ] || die "timed out waiting for OpenClaw CodeBuild"
      sleep "$POLL_SECONDS"
      ;;
    SUCCEEDED|FAILED|FAULT|STOPPED|TIMED_OUT)
      break
      ;;
    *)
      die "unexpected CodeBuild status: $BUILD_STATUS"
      ;;
  esac
done
[ "$BUILD_STATUS" = "SUCCEEDED" ] \
  || die "OpenClaw build failed with status $BUILD_STATUS (build ID: $BUILD_ID)"
[ "$BUILD_RESOLVED_SOURCE_VERSION" = "$COMMIT" ] \
  || die "CodeBuild did not resolve the exact signed commit"

REFERRER_DIR="$TMP_DIR/release-referrers"
mkdir -p "$REFERRER_DIR"
SUBJECT_DIGEST_ARGUMENTS=()
for SUBJECT in core media; do
  if [ "$SUBJECT" = "core" ]; then
    QUARANTINE_REPOSITORY="$CORE_QUARANTINE_REPOSITORY"
    RELEASE_REPOSITORY="$CORE_RELEASE_REPOSITORY"
  else
    QUARANTINE_REPOSITORY="$MEDIA_QUARANTINE_REPOSITORY"
    RELEASE_REPOSITORY="$MEDIA_RELEASE_REPOSITORY"
  fi
  TAG="git-${COMMIT:0:12}-$SUBJECT"
  QUARANTINE_DIGEST="$(
    AWS_PAGER="" aws ecr describe-images \
      --region "$REGION" \
      --registry-id "$EXPECTED_ACCOUNT_ID" \
      --repository-name "$QUARANTINE_REPOSITORY" \
      --image-ids "imageTag=$TAG" \
      --query 'imageDetails[0].imageDigest' \
      --output text
  )"
  RELEASE_DIGEST="$(
    AWS_PAGER="" aws ecr describe-images \
      --region "$REGION" \
      --registry-id "$EXPECTED_ACCOUNT_ID" \
      --repository-name "$RELEASE_REPOSITORY" \
      --image-ids "imageTag=$TAG" \
      --query 'imageDetails[0].imageDigest' \
      --output text
  )"
  [[ "$QUARANTINE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || die "$SUBJECT quarantine digest is invalid"
  [ "$RELEASE_DIGEST" = "$QUARANTINE_DIGEST" ] \
    || die "$SUBJECT release digest differs from verified quarantine"
  MANIFEST_RESPONSE="$TMP_DIR/$SUBJECT-release-arm64-manifest.json"
  AWS_PAGER="" aws ecr batch-get-image \
    --region "$REGION" \
    --registry-id "$EXPECTED_ACCOUNT_ID" \
    --repository-name "$RELEASE_REPOSITORY" \
    --image-ids "imageDigest=$RELEASE_DIGEST" \
    --accepted-media-types application/vnd.oci.image.manifest.v1+json \
    --output json > "$MANIFEST_RESPONSE"
  CONFIG_DIGEST="$(python3 "$PROVENANCE" arm64-config-digest \
    --response "$MANIFEST_RESPONSE" \
    --contract "$BUNDLE_CONTRACT" \
    --expected-image-digest "$RELEASE_DIGEST" \
    --expected-repository "$RELEASE_REPOSITORY" \
    --expected-registry-id "$EXPECTED_ACCOUNT_ID")"
  CONFIG_URL="$(
    AWS_PAGER="" aws ecr get-download-url-for-layer \
      --region "$REGION" \
      --registry-id "$EXPECTED_ACCOUNT_ID" \
      --repository-name "$RELEASE_REPOSITORY" \
      --layer-digest "$CONFIG_DIGEST" \
      --query downloadUrl \
      --output text
  )"
  [[ "$CONFIG_URL" == https://* ]] || die "$SUBJECT release OCI config URL is invalid"
  CONFIG_FILE="$TMP_DIR/$SUBJECT-release-arm64-config.json"
  curl --proto '=https' --tlsv1.2 --fail --silent --show-error \
    --output "$CONFIG_FILE" "$CONFIG_URL"
  unset CONFIG_URL
  python3 "$PROVENANCE" verify-arm64-config \
    --config "$CONFIG_FILE" \
    --expected-config-digest "$CONFIG_DIGEST" \
    --expected-commit "$COMMIT"
  SUBJECT_RESPONSE="$REFERRER_DIR/$SUBJECT-subject-referrers.json"
  AWS_PAGER="" aws ecr list-image-referrers \
    --region "$REGION" \
    --registry-id "$EXPECTED_ACCOUNT_ID" \
    --repository-name "$RELEASE_REPOSITORY" \
    --subject-id "imageDigest=$RELEASE_DIGEST" \
    --max-results 100 \
    --output json > "$SUBJECT_RESPONSE"
  python3 "$PROVENANCE" verify-subject-referrers \
    --response "$SUBJECT_RESPONSE" \
    --contract "$BUNDLE_CONTRACT" \
    > "$TMP_DIR/$SUBJECT-attestation-digests"
  while IFS= read -r ATTESTATION_DIGEST; do
    SIGNATURE_RESPONSE="$REFERRER_DIR/$SUBJECT-${ATTESTATION_DIGEST#sha256:}-signature-referrers.json"
    AWS_PAGER="" aws ecr list-image-referrers \
      --region "$REGION" \
      --registry-id "$EXPECTED_ACCOUNT_ID" \
      --repository-name "$RELEASE_REPOSITORY" \
      --subject-id "imageDigest=$ATTESTATION_DIGEST" \
      --max-results 100 \
      --output json > "$SIGNATURE_RESPONSE"
    python3 "$PROVENANCE" verify-signature-referrers \
      --response "$SIGNATURE_RESPONSE" \
      --contract "$BUNDLE_CONTRACT"
  done < "$TMP_DIR/$SUBJECT-attestation-digests"
  SUBJECT_DIGEST_ARGUMENTS+=(
    --subject-digest "$SUBJECT=$QUARANTINE_DIGEST=$RELEASE_DIGEST"
  )
done

RELEASE_EVIDENCE="$TMP_DIR/openclaw-release-evidence.json"
python3 "$PROVENANCE" create-release-evidence \
  --contract "$BUNDLE_CONTRACT" \
  --source-manifest "$SOURCE_MANIFEST" \
  --source-manifest-key "$SOURCE_MANIFEST_KEY" \
  --source-manifest-version-id "$SOURCE_MANIFEST_VERSION" \
  --source-signature-key "$SOURCE_SIGNATURE_KEY" \
  --source-signature-version-id "$SOURCE_SIGNATURE_VERSION" \
  --build-id "$BUILD_ID" \
  --commit "$COMMIT" \
  "${SUBJECT_DIGEST_ARGUMENTS[@]}" \
  --referrer-directory "$REFERRER_DIR" \
  --output "$RELEASE_EVIDENCE"
RELEASE_EVIDENCE_SHA256="$(file_sha256 "$RELEASE_EVIDENCE")"
BUILD_UUID="${BUILD_ID#*:}"
RELEASE_EVIDENCE_KEY="release-evidence/$COMMIT/$BUILD_UUID/$RELEASE_EVIDENCE_SHA256.json"
RELEASE_SIGNATURE_KEY="$RELEASE_EVIDENCE_KEY.sig"
RELEASE_SIGNATURE="$TMP_DIR/openclaw-release-evidence.sig"
sign_file "$RELEASE_EVIDENCE" "$RELEASE_SIGNATURE"
verify_signature "$RELEASE_EVIDENCE" "$RELEASE_SIGNATURE"
RELEASE_SIGNATURE_VERSION="$(
  publish_or_verify_object "$RELEASE_SIGNATURE" "$RELEASE_SIGNATURE_KEY" application/octet-stream
)"
RELEASE_EVIDENCE_VERSION="$(
  publish_or_verify_object "$RELEASE_EVIDENCE" "$RELEASE_EVIDENCE_KEY" application/json
)"
python3 "$PROVENANCE" verify-release-evidence \
  --evidence "$RELEASE_EVIDENCE" \
  --contract "$BUNDLE_CONTRACT" \
  --expected-build-id "$BUILD_ID" \
  --expected-commit "$COMMIT" \
  --expected-evidence-sha256 "$RELEASE_EVIDENCE_SHA256"

echo "OpenClaw core/media build verified (build only; no deployment performed):"
echo "  build_id=$BUILD_ID"
echo "  commit=$COMMIT"
echo "  source_manifest_key=$SOURCE_MANIFEST_KEY"
echo "  source_manifest_version_id=$SOURCE_MANIFEST_VERSION"
echo "  source_signature_version_id=$SOURCE_SIGNATURE_VERSION"
echo "  release_evidence_key=$RELEASE_EVIDENCE_KEY"
echo "  release_evidence_version_id=$RELEASE_EVIDENCE_VERSION"
echo "  release_signature_version_id=$RELEASE_SIGNATURE_VERSION"
