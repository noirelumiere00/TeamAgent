#!/usr/bin/env bash
# Build-only trusted publisher for the isolated OpenClaw core/media project.
# It publishes immutable signed evidence, starts one fixed CodeBuild project,
# verifies isolated candidate digests/referrers, and never mutates deployment.
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
ATTESTOR_PROJECT="teamagent-dev-image-attestor"
PROMOTER_PROJECT="teamagent-dev-image-promoter"
EVIDENCE_BUCKET="teamagent-dev-openclaw-build-evidence"
SIGNING_KMS_KEY_ALIAS="alias/teamagent-dev-openclaw-build-publisher"
EVIDENCE_KMS_KEY_ALIAS="alias/teamagent-dev-openclaw-build-evidence"
CORE_QUARANTINE_REPOSITORY="teamagent-openclaw-quarantine"
CORE_VERIFIED_CANDIDATE_REPOSITORY="teamagent-openclaw-verified-candidates"
CORE_RELEASE_REPOSITORY="teamagent-openclaw"
MEDIA_QUARANTINE_REPOSITORY="teamagent-openclaw-media-quarantine"
MEDIA_VERIFIED_CANDIDATE_REPOSITORY="teamagent-openclaw-media-verified-candidates"
MEDIA_RELEASE_REPOSITORY="teamagent-openclaw-media"
SOURCE_CONNECTION_NAME="teamagent-dev-openclaw-codebuild"
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
while IFS= read -r AWS_ENDPOINT_VARIABLE; do
  unset "$AWS_ENDPOINT_VARIABLE"
done < <(compgen -A variable AWS_ENDPOINT_URL)
unset AWS_ENDPOINT_VARIABLE

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)" \
  || die "script is not inside a Git worktree"
PROVENANCE="$REPO_ROOT/infra/codebuild/openclaw_provenance.py"
BUNDLE_CONTRACT="$REPO_ROOT/infra/codebuild/openclaw_bundle_contract.json"
IMAGE_RESOLVER="$REPO_ROOT/infra/codebuild/resolve_ecr_image.py"
[ -f "$PROVENANCE" ] || die "OpenClaw provenance verifier is missing"
[ -f "$BUNDLE_CONTRACT" ] || die "OpenClaw bundle contract is missing"
[ -f "$IMAGE_RESOLVER" ] || die "OpenClaw image resolver is missing"

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
PREASSUMED_PUBLISHER="false"
if [ "$ACTUAL_CALLER_ARN" = "$EXPECTED_SESSION_ARN" ]; then
  PREASSUMED_PUBLISHER="true"
elif [ "$ACTUAL_CALLER_ARN" != "$EXPECTED_CALLER_ARN" ]; then
  die "publisher must start as the dedicated caller or exact pinned STS publisher session"
fi
unset INITIAL_IDENTITY ACTUAL_ACCOUNT_ID ACTUAL_CALLER_ARN EXTRA_IDENTITY

if [ "$PREASSUMED_PUBLISHER" = "false" ]; then
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
fi
unset PREASSUMED_PUBLISHER

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

assert_source_connection_available() {
  local inventory="$TMP_DIR/codeconnections.json"
  local exact="$TMP_DIR/codeconnection.json"
  local connection_arn
  AWS_PAGER="" aws codeconnections list-connections \
    --region "$REGION" \
    --max-results 100 \
    --output json >"$inventory"
  jq -e \
    --arg name "$SOURCE_CONNECTION_NAME" \
    --arg account "$EXPECTED_ACCOUNT_ID" '
    (.NextToken // "") == "" and
    ([.Connections[]? |
      select(.ConnectionName == $name)] | length) == 1 and
    ([.Connections[]? | select(.ConnectionName == $name)][0] |
      .ProviderType == "GitHub" and
      .OwnerAccountId == $account)
  ' "$inventory" >/dev/null ||
    die "the exact OpenClaw GitHub CodeConnection is missing or ambiguous"
  connection_arn="$(
    jq -er --arg name "$SOURCE_CONNECTION_NAME" '
      .Connections[] | select(.ConnectionName == $name) | .ConnectionArn
    ' "$inventory"
  )"
  [[ "$connection_arn" =~ ^arn:aws:(codeconnections|codestar-connections):ap-northeast-1:718959508629:connection/[0-9a-f-]+$ ]] \
    || die "the OpenClaw CodeConnection ARN is outside the fixed account"
  AWS_PAGER="" aws codeconnections get-connection \
    --region "$REGION" \
    --connection-arn "$connection_arn" \
    --output json >"$exact"
  jq -e \
    --arg name "$SOURCE_CONNECTION_NAME" \
    --arg arn "$connection_arn" \
    --arg account "$EXPECTED_ACCOUNT_ID" '
    .Connection == {
      ConnectionName:$name,
      ConnectionArn:$arn,
      ProviderType:"GitHub",
      OwnerAccountId:$account,
      ConnectionStatus:"AVAILABLE"
    }
  ' "$exact" >/dev/null ||
    die "the OpenClaw GitHub CodeConnection is not AVAILABLE"
}

assert_source_connection_available

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
[ "$LOCK_ENABLED" = "Enabled" ] \
  && { [ "$LOCK_MODE" = "COMPLIANCE" ] || [ "$LOCK_MODE" = "GOVERNANCE" ]; } \
  && [ "$LOCK_DAYS" = "3650" ] \
  || die "evidence bucket must use durable Object Lock"
unset OBJECT_LOCK LOCK_ENABLED LOCK_MODE LOCK_DAYS EXTRA_LOCK

RETAIN_UNTIL="$(python3 - <<'PY'
import datetime

value = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=3650, minutes=5)
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
      --object-lock-mode GOVERNANCE \
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
if value.get("ObjectLockMode") not in {"COMPLIANCE", "GOVERNANCE"}:
    raise SystemExit("object is not durably locked")
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

environment_json() {
  local output="$1"
  shift
  python3 - "$output" "$@" <<'PY'
import json
import sys

output, *pairs = sys.argv[1:]
values = []
for pair in pairs:
    if "=" not in pair:
        raise SystemExit("invalid environment pair")
    name, value = pair.split("=", 1)
    values.append({"name": name, "value": value, "type": "PLAINTEXT"})
with open(output, "w", encoding="utf-8") as handle:
    json.dump(values, handle, sort_keys=True, separators=(",", ":"))
PY
}

start_source_free_build() {
  local project="$1"
  local environment_file="$2"
  AWS_PAGER="" aws codebuild start-build \
    --region "$REGION" \
    --project-name "$project" \
    --environment-variables-override "file://$environment_file" \
    --query build.id \
    --output text
}

wait_source_free_build() {
  local build_id="$1"
  local deadline=$((SECONDS + TIMEOUT_SECONDS))
  local status
  while :; do
    status="$(
      AWS_PAGER="" aws codebuild batch-get-builds \
        --region "$REGION" \
        --ids "$build_id" \
        --query 'builds[0].buildStatus' \
        --output text
    )"
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

exported_build_value() {
  local build_id="$1"
  local name="$2"
  AWS_PAGER="" aws codebuild batch-get-builds \
    --region "$REGION" \
    --ids "$build_id" \
    --output json \
    | jq -er --arg name "$name" '
        [.builds[0].exportedEnvironmentVariables[] | select(.name == $name) | .value] |
        if length == 1 then .[0] else error("missing or duplicate export") end
      '
}

SUBJECTS_JSON="[]"
for SUBJECT in core media; do
  if [ "$SUBJECT" = "core" ]; then
    QUARANTINE_REPOSITORY="$CORE_QUARANTINE_REPOSITORY"
    VERIFIED_CANDIDATE_REPOSITORY="$CORE_VERIFIED_CANDIDATE_REPOSITORY"
    RELEASE_REPOSITORY="$CORE_RELEASE_REPOSITORY"
  else
    QUARANTINE_REPOSITORY="$MEDIA_QUARANTINE_REPOSITORY"
    VERIFIED_CANDIDATE_REPOSITORY="$MEDIA_VERIFIED_CANDIDATE_REPOSITORY"
    RELEASE_REPOSITORY="$MEDIA_RELEASE_REPOSITORY"
  fi
  TAG="candidate-$COMMIT-$SUBJECT"
  QUARANTINE_TAG_DIGEST="$(
    AWS_PAGER="" aws ecr describe-images \
      --region "$REGION" \
      --registry-id "$EXPECTED_ACCOUNT_ID" \
      --repository-name "$QUARANTINE_REPOSITORY" \
      --image-ids "imageTag=$TAG" \
      --query 'imageDetails[0].imageDigest' \
      --output text
  )"
  [[ "$QUARANTINE_TAG_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || die "$SUBJECT quarantine tag digest is invalid"
  AWS_PAGER="" aws ecr batch-get-image \
    --region "$REGION" \
    --registry-id "$EXPECTED_ACCOUNT_ID" \
    --repository-name "$QUARANTINE_REPOSITORY" \
    --image-ids "imageDigest=$QUARANTINE_TAG_DIGEST" \
    --accepted-media-types \
      application/vnd.docker.distribution.manifest.list.v2+json \
      application/vnd.oci.image.index.v1+json \
      application/vnd.docker.distribution.manifest.v2+json \
      application/vnd.oci.image.manifest.v1+json \
    --output json >"$TMP_DIR/openclaw-$SUBJECT-parent.json"
  QUARANTINE_DIGEST="$(
    python3 "$IMAGE_RESOLVER" resolve-platform \
      --batch-response "$TMP_DIR/openclaw-$SUBJECT-parent.json" \
      --expected-image-digest "$QUARANTINE_TAG_DIGEST" \
      --os linux \
      --architecture arm64
  )"
  [[ "$QUARANTINE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || die "$SUBJECT resolved arm64 quarantine digest is invalid"
  SUBJECTS_JSON="$(
    jq -cn \
      --argjson current "$SUBJECTS_JSON" \
      --arg name "$SUBJECT" \
      --arg quarantine "$QUARANTINE_REPOSITORY" \
      --arg candidate "$VERIFIED_CANDIDATE_REPOSITORY" \
      --arg release "$RELEASE_REPOSITORY" \
      --arg digest "$QUARANTINE_DIGEST" \
      '$current + [{
        name: $name,
        quarantine_repository: $quarantine,
        candidate_repository: $candidate,
        release_repository: $release,
        digest: $digest
      }]'
  )"
done

ATTESTOR_ENV="$TMP_DIR/attestor-env.json"
environment_json "$ATTESTOR_ENV" \
  "PIPELINE=openclaw" \
  "PROMOTION_CHANNEL=verified-candidate" \
  "SOURCE_COMMIT=$COMMIT" \
  "CONTRACT_SHA256=$BUNDLE_CONTRACT_SHA256" \
  "SOURCE_EVIDENCE_BUCKET=$EVIDENCE_BUCKET" \
  "SOURCE_EVIDENCE_KEY=$SOURCE_MANIFEST_KEY" \
  "SOURCE_EVIDENCE_VERSION_ID=$SOURCE_MANIFEST_VERSION" \
  "SOURCE_EVIDENCE_SHA256=$SOURCE_MANIFEST_SHA256" \
  "SOURCE_EVIDENCE_SIGNATURE_KEY=$SOURCE_SIGNATURE_KEY" \
  "SOURCE_EVIDENCE_SIGNATURE_VERSION_ID=$SOURCE_SIGNATURE_VERSION" \
  "BUILD_ID=$BUILD_ID" \
  "SUBJECTS_JSON=$SUBJECTS_JSON"
ATTESTOR_BUILD_ID="$(start_source_free_build "$ATTESTOR_PROJECT" "$ATTESTOR_ENV")"
[[ "$ATTESTOR_BUILD_ID" == "$ATTESTOR_PROJECT:"* ]] || die "invalid attestor build ID"
wait_source_free_build "$ATTESTOR_BUILD_ID"
RECEIPT_KEY="$(exported_build_value "$ATTESTOR_BUILD_ID" RECEIPT_KEY)"
RECEIPT_VERSION="$(exported_build_value "$ATTESTOR_BUILD_ID" RECEIPT_VERSION_ID)"
RECEIPT_SIGNATURE_KEY="$(exported_build_value "$ATTESTOR_BUILD_ID" RECEIPT_SIGNATURE_KEY)"
RECEIPT_SIGNATURE_VERSION="$(
  exported_build_value "$ATTESTOR_BUILD_ID" RECEIPT_SIGNATURE_VERSION_ID
)"
[ "$RECEIPT_SIGNATURE_KEY" = "$RECEIPT_KEY.sig" ] || die "receipt signature key mismatch"

PROMOTER_ENV="$TMP_DIR/promoter-env.json"
environment_json "$PROMOTER_ENV" \
  "PIPELINE=openclaw" \
  "PROMOTION_CHANNEL=verified-candidate" \
  "SOURCE_COMMIT=$COMMIT" \
  "CONTRACT_SHA256=$BUNDLE_CONTRACT_SHA256" \
  "RECEIPT_KEY=$RECEIPT_KEY" \
  "RECEIPT_VERSION_ID=$RECEIPT_VERSION" \
  "RECEIPT_SIGNATURE_KEY=$RECEIPT_SIGNATURE_KEY" \
  "RECEIPT_SIGNATURE_VERSION_ID=$RECEIPT_SIGNATURE_VERSION"
PROMOTER_BUILD_ID="$(start_source_free_build "$PROMOTER_PROJECT" "$PROMOTER_ENV")"
[[ "$PROMOTER_BUILD_ID" == "$PROMOTER_PROJECT:"* ]] || die "invalid promoter build ID"
wait_source_free_build "$PROMOTER_BUILD_ID"

while IFS= read -r SUBJECT; do
  NAME="$(jq -er '.name' <<<"$SUBJECT")"
  VERIFIED_CANDIDATE_REPOSITORY="$(jq -er '.candidate_repository' <<<"$SUBJECT")"
  VERIFIED_DIGEST="$(jq -er '.digest' <<<"$SUBJECT")"
  VERIFIED_CANDIDATE_DIGEST="$(
    AWS_PAGER="" aws ecr describe-images \
      --region "$REGION" \
      --registry-id "$EXPECTED_ACCOUNT_ID" \
      --repository-name "$VERIFIED_CANDIDATE_REPOSITORY" \
      --image-ids "imageTag=verified-$COMMIT-$NAME" \
      --query 'imageDetails[0].imageDigest' \
      --output text
  )"
  [ "$VERIFIED_CANDIDATE_DIGEST" = "$VERIFIED_DIGEST" ] \
    || die "$NAME verified-candidate digest differs from the signed quarantine digest"
done < <(jq -c '.[]' <<<"$SUBJECTS_JSON")

echo "OpenClaw core/media build verified (build only; no deployment performed):"
echo "  build_id=$BUILD_ID"
echo "  commit=$COMMIT"
echo "  source_manifest_key=$SOURCE_MANIFEST_KEY"
echo "  source_manifest_version_id=$SOURCE_MANIFEST_VERSION"
echo "  source_signature_key=$SOURCE_SIGNATURE_KEY"
echo "  source_signature_version_id=$SOURCE_SIGNATURE_VERSION"
echo "  release_receipt_key=$RECEIPT_KEY"
echo "  release_receipt_version_id=$RECEIPT_VERSION"
echo "  release_signature_key=$RECEIPT_SIGNATURE_KEY"
echo "  release_signature_version_id=$RECEIPT_SIGNATURE_VERSION"
echo "No ECS, EventBridge, task definition, service, schedule, or Terraform change was made."
