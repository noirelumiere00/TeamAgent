#!/usr/bin/env bash
# Build-only TikTok launcher: signed source -> quarantine -> attestor -> promoter.
set -euo pipefail
umask 077

REGION="ap-northeast-1"
ACCOUNT_ID="718959508629"
EXPECTED_CALLER_ARN="arn:aws:iam::718959508629:user/teamagent-tiktok-build-caller"
LAUNCHER_ROLE_ARN="arn:aws:iam::718959508629:role/teamagent-dev-tiktok-build-launcher"
SESSION_NAME="teamagent-tiktok-build"
EXPECTED_SESSION_ARN="arn:aws:sts::718959508629:assumed-role/teamagent-dev-tiktok-build-launcher/teamagent-tiktok-build"
SOURCE_ORIGIN="git@github.com:noirelumiere00/tiktok-data-service.git"
SOURCE_BRANCH="main"
CONTROL_ORIGIN="git@github.com:noirelumiere00/TeamAgent.git"
CONTROL_BRANCH="dev"
EVIDENCE_BUCKET="teamagent-dev-image-release-evidence"
EVIDENCE_KMS_ALIAS="alias/teamagent-dev-image-release-evidence"
SOURCE_SIGNING_KEY_ALIAS="alias/teamagent-dev-tiktok-source-publisher"
IMAGE_PROJECT="teamagent-dev-tiktok-image-builder"
ATTESTOR_PROJECT="teamagent-dev-image-attestor"
PROMOTER_PROJECT="teamagent-dev-image-promoter"
QUARANTINE_REPOSITORY="teamagent-dev-tiktok-acquire-quarantine"
VERIFIED_CANDIDATE_REPOSITORY="teamagent-dev-tiktok-acquire-verified-candidates"
POLL_SECONDS=15
TIMEOUT_SECONDS=7200

die() {
  echo "FATAL: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
usage: build_tiktok_image.sh [--poll-seconds N] [--timeout-seconds N]

Run this script with the TikTok source repository as the current directory.
It accepts no source, repository, project, registry, region, tag, or buildspec
override. It never updates ECS, EventBridge, task definitions, or Terraform.
EOF
}

require_value() {
  [ "$#" -ge 2 ] && [ -n "${2-}" ] || die "$1 requires a value"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --poll-seconds) require_value "$@"; POLL_SECONDS="$2"; shift 2 ;;
    --timeout-seconds) require_value "$@"; TIMEOUT_SECONDS="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done
[[ "$POLL_SECONDS" =~ ^[1-9][0-9]*$ ]] || die "poll interval must be positive"
[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || die "timeout must be positive"
[ "$TIMEOUT_SECONDS" -ge "$POLL_SECONDS" ] || die "timeout is shorter than polling"
for tool in aws cmp git jq python3 sha256sum; do
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
  || die "launcher is not inside the TeamAgent control worktree"
SOURCE_ROOT="$(git -C "$(pwd -P)" rev-parse --show-toplevel 2>/dev/null)" \
  || die "current directory is not the TikTok source worktree"
PROVENANCE="$CONTROL_ROOT/infra/codebuild/tiktok_source_provenance.py"
CONTRACT="$CONTROL_ROOT/infra/codebuild/tiktok_release_contract.json"
RESOLVER="$CONTROL_ROOT/infra/codebuild/resolve_ecr_image.py"
[ -f "$PROVENANCE" ] && [ -f "$CONTRACT" ] && [ -f "$RESOLVER" ] \
  || die "trusted TikTok control files are missing"

verify_clean_remote_head() {
  local repo="$1"
  local branch="$2"
  local origin="$3"
  local label="$4"
  [ -z "$(git -C "$repo" status --porcelain=v1 --untracked-files=all --ignore-submodules=none)" ] \
    || die "$label worktree is dirty"
  [ "$(git -C "$repo" symbolic-ref --quiet --short HEAD)" = "$branch" ] \
    || die "$label must be on local $branch"
  [ "$(git -C "$repo" config --get remote.origin.url)" = "$origin" ] \
    || die "$label origin is not allowlisted"
  git -C "$repo" fetch --quiet --no-tags origin \
    "refs/heads/$branch:refs/remotes/origin/$branch" \
    || die "could not refresh $label origin/$branch"
  local head remote
  head="$(git -C "$repo" rev-parse --verify HEAD^{commit})"
  remote="$(git -C "$repo" rev-parse --verify "refs/remotes/origin/$branch^{commit}")"
  [[ "$head" =~ ^[0-9a-f]{40}$ ]] && [ "$head" = "$remote" ] \
    || die "$label HEAD must exactly equal origin/$branch"
  printf '%s\n' "$head"
}

CONTROL_COMMIT="$(verify_clean_remote_head "$CONTROL_ROOT" "$CONTROL_BRANCH" "$CONTROL_ORIGIN" TeamAgent)"
SOURCE_COMMIT="$(verify_clean_remote_head "$SOURCE_ROOT" "$SOURCE_BRANCH" "$SOURCE_ORIGIN" TikTok)"
[[ "$CONTROL_COMMIT" =~ ^[0-9a-f]{40}$ ]] || die "invalid control commit"
CONTRACT_SHA256="$(sha256sum "$CONTRACT" | awk '{print $1}')"
python3 - "$CONTRACT" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    contract = json.load(handle)
if contract.get("release") != {"ready": True, "blocked_reason": ""}:
    raise SystemExit("FATAL: TikTok release contract is not approved")
PY

identity() {
  AWS_PAGER="" aws sts get-caller-identity --query '[Account,Arn]' --output text
}
INITIAL_IDENTITY="$(identity)"
[[ "$INITIAL_IDENTITY" != *$'\n'* && "$INITIAL_IDENTITY" != *$'\r'* ]] \
  || die "malformed initial TikTok caller identity"
IFS=$'\t' read -r INITIAL_ACCOUNT INITIAL_ARN EXTRA <<<"$INITIAL_IDENTITY"
[ -z "${EXTRA:-}" ] && [ "$INITIAL_ACCOUNT" = "$ACCOUNT_ID" ] \
  && [ "$INITIAL_ARN" = "$EXPECTED_CALLER_ARN" ] \
  || die "launcher must start as the exact dedicated TikTok caller"
unset INITIAL_IDENTITY INITIAL_ACCOUNT INITIAL_ARN EXTRA

SESSION_CREDENTIALS="$(
  AWS_PAGER="" aws sts assume-role \
    --region "$REGION" \
    --role-arn "$LAUNCHER_ROLE_ARN" \
    --role-session-name "$SESSION_NAME" \
    --duration-seconds 10800 \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken,Expiration]' \
    --output text
)" || die "could not assume the TikTok launcher role"
[[ "$SESSION_CREDENTIALS" != *$'\n'* && "$SESSION_CREDENTIALS" != *$'\r'* ]] \
  || die "malformed TikTok launcher credentials"
IFS=$'\t' read -r AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN EXPIRATION EXTRA \
  <<<"$SESSION_CREDENTIALS"
[ -z "${EXTRA:-}" ] || die "malformed launcher credentials"
for credential in \
  "$AWS_ACCESS_KEY_ID" \
  "$AWS_SECRET_ACCESS_KEY" \
  "$AWS_SESSION_TOKEN" \
  "$EXPIRATION"; do
  [ -n "$credential" ] && [ "$credential" != "None" ] || die "incomplete launcher credentials"
done
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
export AWS_DEFAULT_REGION="$REGION" AWS_REGION="$REGION"
export AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null
unset AWS_PROFILE AWS_DEFAULT_PROFILE SESSION_CREDENTIALS EXPIRATION EXTRA credential
SESSION_IDENTITY="$(identity)"
IFS=$'\t' read -r SESSION_ACCOUNT SESSION_ARN EXTRA <<<"$SESSION_IDENTITY"
[ -z "${EXTRA:-}" ] && [ "$SESSION_ACCOUNT" = "$ACCOUNT_ID" ] \
  && [ "$SESSION_ARN" = "$EXPECTED_SESSION_ARN" ] \
  || die "unexpected pinned TikTok launcher session"
unset SESSION_IDENTITY SESSION_ACCOUNT SESSION_ARN EXTRA

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/teamagent-tiktok-build.XXXXXXXX")"
trap 'rm -rf -- "$TMP_DIR"' EXIT
EVIDENCE_KMS_KEY_ARN="$(
  AWS_PAGER="" aws kms describe-key \
    --region "$REGION" \
    --key-id "$EVIDENCE_KMS_ALIAS" \
    --query KeyMetadata.Arn \
    --output text
)"
SOURCE_SIGNING_KEY_ARN="$(
  AWS_PAGER="" aws kms describe-key \
    --region "$REGION" \
    --key-id "$SOURCE_SIGNING_KEY_ALIAS" \
    --query KeyMetadata.Arn \
    --output text
)"
[[ "$EVIDENCE_KMS_KEY_ARN" =~ ^arn:aws:kms:ap-northeast-1:718959508629:key/[0-9a-f-]{36}$ ]] \
  || die "TikTok evidence KMS key is outside the fixed account"
[[ "$SOURCE_SIGNING_KEY_ARN" =~ ^arn:aws:kms:ap-northeast-1:718959508629:key/[0-9a-f-]{36}$ ]] \
  || die "TikTok signing KMS key is outside the fixed account"
[ "$(AWS_PAGER="" aws s3api get-bucket-versioning \
  --region "$REGION" \
  --bucket "$EVIDENCE_BUCKET" \
  --expected-bucket-owner "$ACCOUNT_ID" \
  --query Status \
  --output text)" = "Enabled" ] \
  || die "TikTok evidence bucket versioning must be Enabled"
OBJECT_LOCK="$(
  AWS_PAGER="" aws s3api get-object-lock-configuration \
    --region "$REGION" \
    --bucket "$EVIDENCE_BUCKET" \
    --expected-bucket-owner "$ACCOUNT_ID" \
    --query '[ObjectLockEnabled,Rule.DefaultRetention.Mode,Rule.DefaultRetention.Days]' \
    --output text
)"
[[ "$OBJECT_LOCK" != *$'\n'* && "$OBJECT_LOCK" != *$'\r'* ]] \
  || die "malformed TikTok evidence Object Lock configuration"
IFS=$'\t' read -r LOCK_ENABLED LOCK_MODE LOCK_DAYS EXTRA <<<"$OBJECT_LOCK"
[ -z "${EXTRA:-}" ] \
  && [ "$LOCK_ENABLED" = "Enabled" ] \
  && [ "$LOCK_MODE" = "COMPLIANCE" ] \
  && [ "$LOCK_DAYS" = "3650" ] \
  || die "TikTok evidence bucket must use durable COMPLIANCE Object Lock"
unset OBJECT_LOCK LOCK_ENABLED LOCK_MODE LOCK_DAYS EXTRA
SOURCE_MANIFEST="$TMP_DIR/tiktok-source-manifest.json"
SOURCE_SIGNATURE="$TMP_DIR/tiktok-source-manifest.sig"
python3 "$PROVENANCE" create-manifest \
  --repo-root "$SOURCE_ROOT" \
  --commit "$SOURCE_COMMIT" \
  --contract "$CONTRACT" \
  --output "$SOURCE_MANIFEST"
SOURCE_MANIFEST_SHA256="$(sha256sum "$SOURCE_MANIFEST" | awk '{print $1}')"
SOURCE_MANIFEST_KEY="source-manifests/tiktok/$SOURCE_COMMIT/$SOURCE_MANIFEST_SHA256.json"
SOURCE_SIGNATURE_KEY="$SOURCE_MANIFEST_KEY.sig"
python3 - "$SOURCE_MANIFEST" "$TMP_DIR/source-manifest.sha256" <<'PY'
import hashlib
import sys
with open(sys.argv[1], "rb") as source:
    digest = hashlib.sha256(source.read()).digest()
with open(sys.argv[2], "wb") as output:
    output.write(digest)
PY
AWS_PAGER="" aws kms sign \
  --region "$REGION" \
  --key-id "$SOURCE_SIGNING_KEY_ARN" \
  --message-type DIGEST \
  --message "fileb://$TMP_DIR/source-manifest.sha256" \
  --signing-algorithm RSASSA_PSS_SHA_256 \
  --output json >"$TMP_DIR/source-manifest-sign.json"
python3 - "$TMP_DIR/source-manifest-sign.json" "$SOURCE_SIGNATURE" <<'PY'
import base64
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    response = json.load(source)
with open(sys.argv[2], "wb") as output:
    output.write(base64.b64decode(response["Signature"], validate=True))
PY
[ "$(AWS_PAGER="" aws kms verify \
  --region "$REGION" \
  --key-id "$SOURCE_SIGNING_KEY_ARN" \
  --message-type DIGEST \
  --message "fileb://$TMP_DIR/source-manifest.sha256" \
  --signature "fileb://$SOURCE_SIGNATURE" \
  --signing-algorithm RSASSA_PSS_SHA_256 \
  --query SignatureValid \
  --output text)" = "True" ] \
  || die "TikTok source manifest KMS signature verification failed"

publish_evidence() {
  local body="$1"
  local key="$2"
  local content_type="$3"
  local retain_until version
  retain_until="$(python3 - <<'PY'
import datetime

value = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=3650)
print(value.isoformat(timespec="seconds").replace("+00:00", "Z"))
PY
)"
  version="$(
    AWS_PAGER="" aws s3api put-object \
      --region "$REGION" \
      --bucket "$EVIDENCE_BUCKET" \
      --key "$key" \
      --body "$body" \
      --expected-bucket-owner "$ACCOUNT_ID" \
      --content-type "$content_type" \
      --server-side-encryption aws:kms \
      --ssekms-key-id "$EVIDENCE_KMS_KEY_ARN" \
      --object-lock-mode COMPLIANCE \
      --object-lock-retain-until-date "$retain_until" \
      --if-none-match '*' \
      --query VersionId \
      --output text
  )" || die "could not publish immutable TikTok source evidence"
  case "$version" in ""|None|null|*[!A-Za-z0-9._~+/=-]*) die "invalid evidence VersionId" ;; esac
  printf '%s\n' "$version"
}

SOURCE_MANIFEST_VERSION_ID="$(
  publish_evidence "$SOURCE_MANIFEST" "$SOURCE_MANIFEST_KEY" application/json
)"
SOURCE_MANIFEST_SIGNATURE_VERSION_ID="$(
  publish_evidence "$SOURCE_SIGNATURE" "$SOURCE_SIGNATURE_KEY" application/octet-stream
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
    if "=" not in pair:
        raise SystemExit("invalid environment pair")
    name, value = pair.split("=", 1)
    values.append({"name": name, "value": value, "type": "PLAINTEXT"})
with open(output, "w", encoding="utf-8") as handle:
    json.dump(values, handle, sort_keys=True, separators=(",", ":"))
PY
}

start_build() {
  local project="$1"
  local env_file="$2"
  local source_version="${3-}"
  local args=(
    codebuild start-build
    --region "$REGION"
    --project-name "$project"
    --environment-variables-override "file://$env_file"
    --query build.id
    --output text
  )
  [ -z "$source_version" ] || args+=(--source-version "$source_version")
  AWS_PAGER="" aws "${args[@]}"
}

wait_build() {
  local build_id="$1"
  local expected_source="${2-}"
  local deadline=$((SECONDS + TIMEOUT_SECONDS))
  local response status source
  while :; do
    response="$(
      AWS_PAGER="" aws codebuild batch-get-builds \
        --region "$REGION" \
        --ids "$build_id" \
        --output json
    )"
    status="$(jq -er '.builds | if length == 1 then .[0].buildStatus else error("ambiguous build") end' <<<"$response")"
    source="$(jq -er '.builds[0].sourceVersion // ""' <<<"$response")"
    [ -z "$expected_source" ] || [ "$source" = "$expected_source" ] \
      || die "CodeBuild sourceVersion mismatch"
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

IMAGE_ENV="$TMP_DIR/image-env.json"
environment_json "$IMAGE_ENV" \
  "GIT_COMMIT=$SOURCE_COMMIT" \
  "GIT_BRANCH=main" \
  "TIKTOK_CONTRACT_SHA256=$CONTRACT_SHA256" \
  "SOURCE_MANIFEST_KEY=$SOURCE_MANIFEST_KEY" \
  "SOURCE_MANIFEST_VERSION_ID=$SOURCE_MANIFEST_VERSION_ID" \
  "SOURCE_MANIFEST_SHA256=$SOURCE_MANIFEST_SHA256" \
  "SOURCE_MANIFEST_SIGNATURE_KEY=$SOURCE_SIGNATURE_KEY" \
  "SOURCE_MANIFEST_SIGNATURE_VERSION_ID=$SOURCE_MANIFEST_SIGNATURE_VERSION_ID"
IMAGE_BUILD_ID="$(start_build "$IMAGE_PROJECT" "$IMAGE_ENV" "$SOURCE_COMMIT")"
[[ "$IMAGE_BUILD_ID" == "$IMAGE_PROJECT:"* ]] || die "invalid TikTok build ID"
wait_build "$IMAGE_BUILD_ID" "$SOURCE_COMMIT"

TAG_DIGEST="$(
  AWS_PAGER="" aws ecr describe-images \
    --region "$REGION" \
    --registry-id "$ACCOUNT_ID" \
    --repository-name "$QUARANTINE_REPOSITORY" \
    --image-ids "imageTag=$SOURCE_COMMIT" \
    --query 'imageDetails[0].imageDigest' \
    --output text
)"
[[ "$TAG_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || die "invalid TikTok quarantine digest"
AWS_PAGER="" aws ecr batch-get-image \
  --region "$REGION" \
  --registry-id "$ACCOUNT_ID" \
  --repository-name "$QUARANTINE_REPOSITORY" \
  --image-ids "imageDigest=$TAG_DIGEST" \
  --accepted-media-types \
    application/vnd.docker.distribution.manifest.list.v2+json \
    application/vnd.oci.image.index.v1+json \
    application/vnd.docker.distribution.manifest.v2+json \
    application/vnd.oci.image.manifest.v1+json \
  --output json >"$TMP_DIR/tiktok-parent.json"
VERIFIED_DIGEST="$(
  python3 "$RESOLVER" resolve-platform \
    --batch-response "$TMP_DIR/tiktok-parent.json" \
    --expected-image-digest "$TAG_DIGEST" \
    --os linux \
    --architecture arm64
)"
[[ "$VERIFIED_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || die "invalid TikTok arm64 digest"
SUBJECTS_JSON="$(
  jq -cn --arg digest "$VERIFIED_DIGEST" '[{
    name: "tiktok",
    quarantine_repository: "teamagent-dev-tiktok-acquire-quarantine",
    candidate_repository: "teamagent-dev-tiktok-acquire-verified-candidates",
    release_repository: "teamagent-dev-tiktok-acquire",
    digest: $digest
  }]'
)"

ATTESTOR_ENV="$TMP_DIR/attestor-env.json"
environment_json "$ATTESTOR_ENV" \
  "PIPELINE=tiktok" \
  "PROMOTION_CHANNEL=verified-candidate" \
  "SOURCE_COMMIT=$SOURCE_COMMIT" \
  "CONTRACT_SHA256=$CONTRACT_SHA256" \
  "SOURCE_EVIDENCE_BUCKET=$EVIDENCE_BUCKET" \
  "SOURCE_EVIDENCE_KEY=$SOURCE_MANIFEST_KEY" \
  "SOURCE_EVIDENCE_VERSION_ID=$SOURCE_MANIFEST_VERSION_ID" \
  "SOURCE_EVIDENCE_SHA256=$SOURCE_MANIFEST_SHA256" \
  "SOURCE_EVIDENCE_SIGNATURE_KEY=$SOURCE_SIGNATURE_KEY" \
  "SOURCE_EVIDENCE_SIGNATURE_VERSION_ID=$SOURCE_MANIFEST_SIGNATURE_VERSION_ID" \
  "BUILD_ID=$IMAGE_BUILD_ID" \
  "SUBJECTS_JSON=$SUBJECTS_JSON"
ATTESTOR_BUILD_ID="$(start_build "$ATTESTOR_PROJECT" "$ATTESTOR_ENV")"
[[ "$ATTESTOR_BUILD_ID" == "$ATTESTOR_PROJECT:"* ]] || die "invalid attestor build ID"
wait_build "$ATTESTOR_BUILD_ID"

RECEIPT_KEY="$(exported_build_value "$ATTESTOR_BUILD_ID" RECEIPT_KEY)"
RECEIPT_VERSION="$(exported_build_value "$ATTESTOR_BUILD_ID" RECEIPT_VERSION_ID)"
RECEIPT_SIGNATURE_KEY="$(exported_build_value "$ATTESTOR_BUILD_ID" RECEIPT_SIGNATURE_KEY)"
RECEIPT_SIGNATURE_VERSION="$(
  exported_build_value "$ATTESTOR_BUILD_ID" RECEIPT_SIGNATURE_VERSION_ID
)"
[ "$RECEIPT_SIGNATURE_KEY" = "$RECEIPT_KEY.sig" ] || die "receipt signature key mismatch"

PROMOTER_ENV="$TMP_DIR/promoter-env.json"
environment_json "$PROMOTER_ENV" \
  "PIPELINE=tiktok" \
  "PROMOTION_CHANNEL=verified-candidate" \
  "SOURCE_COMMIT=$SOURCE_COMMIT" \
  "CONTRACT_SHA256=$CONTRACT_SHA256" \
  "RECEIPT_KEY=$RECEIPT_KEY" \
  "RECEIPT_VERSION_ID=$RECEIPT_VERSION" \
  "RECEIPT_SIGNATURE_KEY=$RECEIPT_SIGNATURE_KEY" \
  "RECEIPT_SIGNATURE_VERSION_ID=$RECEIPT_SIGNATURE_VERSION"
PROMOTER_BUILD_ID="$(start_build "$PROMOTER_PROJECT" "$PROMOTER_ENV")"
[[ "$PROMOTER_BUILD_ID" == "$PROMOTER_PROJECT:"* ]] || die "invalid promoter build ID"
wait_build "$PROMOTER_BUILD_ID"

VERIFIED_CANDIDATE_DIGEST="$(
  AWS_PAGER="" aws ecr describe-images \
    --region "$REGION" \
    --registry-id "$ACCOUNT_ID" \
    --repository-name "$VERIFIED_CANDIDATE_REPOSITORY" \
    --image-ids "imageTag=verified-$SOURCE_COMMIT" \
    --query 'imageDetails[0].imageDigest' \
    --output text
)"
[ "$VERIFIED_CANDIDATE_DIGEST" = "$VERIFIED_DIGEST" ] \
  || die "TikTok verified-candidate digest differs from verified quarantine digest"
echo "Build-only TikTok verified candidate completed:"
echo "  commit=$SOURCE_COMMIT"
echo "  verified_candidate_image=718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/$VERIFIED_CANDIDATE_REPOSITORY@$VERIFIED_CANDIDATE_DIGEST"
echo "  source_manifest_key=$SOURCE_MANIFEST_KEY"
echo "  source_manifest_version_id=$SOURCE_MANIFEST_VERSION_ID"
echo "  source_signature_key=$SOURCE_SIGNATURE_KEY"
echo "  source_signature_version_id=$SOURCE_MANIFEST_SIGNATURE_VERSION_ID"
echo "  receipt_key=$RECEIPT_KEY"
echo "  receipt_version_id=$RECEIPT_VERSION"
echo "  receipt_signature_key=$RECEIPT_SIGNATURE_KEY"
echo "  receipt_signature_version_id=$RECEIPT_SIGNATURE_VERSION"
echo "No ECS, EventBridge, task definition, service, schedule, or Terraform change was made."
