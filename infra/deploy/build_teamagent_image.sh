#!/usr/bin/env bash
# Build-only MCP launcher: publisher -> quarantine -> attestor -> verified-candidate.
set -euo pipefail
umask 077

REGION="ap-northeast-1"
ACCOUNT_ID="718959508629"
EXPECTED_CALLER_ARN="arn:aws:iam::718959508629:user/AIIAdev"
LAUNCHER_ROLE_ARN="arn:aws:iam::718959508629:role/teamagent-dev-codebuild-launcher"
LAUNCHER_SESSION_NAME="teamagent-build-launcher"
EXPECTED_SESSION_ARN="arn:aws:sts::718959508629:assumed-role/teamagent-dev-codebuild-launcher/teamagent-build-launcher"
EXPECTED_BRANCH="dev"
EXPECTED_ORIGIN_URL="git@github.com:noirelumiere00/TeamAgent.git"
APP_BUCKET="teamagent-dev-raw-files"
APP_KEY="codebuild/connect-web-app.html"
EVIDENCE_BUCKET="teamagent-dev-image-release-evidence"
SOURCE_PUBLISHER_PROJECT="teamagent-dev-mcp-source-publisher"
IMAGE_PROJECT="teamagent-dev-image-builder"
ATTESTOR_PROJECT="teamagent-dev-image-attestor"
PROMOTER_PROJECT="teamagent-dev-image-promoter"
VERIFIED_CANDIDATE_REPOSITORY="teamagent-mcp-verified-candidates"
MEDIA_VERIFIED_CANDIDATE_REPOSITORY="teamagent-media-worker-verified-candidates"
POLL_SECONDS=15
TIMEOUT_SECONDS=7200

die() {
  echo "FATAL: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
usage: build_teamagent_image.sh [--poll-seconds N] [--timeout-seconds N]

Builds, attests, and publishes the exact current origin/dev commit to the
isolated verified-candidate repository. It does not
accept an image tag or source path. The candidate/verified tags are derived
from the full commit. This launcher never updates ECS, EventBridge, Terraform,
task definitions, services, or schedules.
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
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)" \
  || die "launcher is not inside a Git worktree"
SOURCE_MANIFEST_CONTRACT="$REPO_ROOT/infra/codebuild/teamagent_runtime_contract.json"
RELEASE_CONTRACT="$REPO_ROOT/infra/codebuild/teamagent_core_media_release_contract.json"
PROVENANCE="$REPO_ROOT/infra/codebuild/source_provenance.py"
BUNDLE_PROVENANCE="$REPO_ROOT/infra/codebuild/teamagent_bundle_provenance.py"
[ -f "$SOURCE_MANIFEST_CONTRACT" ] && [ -f "$RELEASE_CONTRACT" ] \
  && [ -f "$PROVENANCE" ] && [ -f "$BUNDLE_PROVENANCE" ] \
  || die "trusted local contract helpers are missing"
[ -z "$(git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=all --ignore-submodules=none)" ] \
  || die "Git worktree is dirty"
BRANCH="$(git -C "$REPO_ROOT" symbolic-ref --quiet --short HEAD)" \
  || die "detached HEAD is not allowed"
[ "$BRANCH" = "$EXPECTED_BRANCH" ] || die "builds must run from local dev"
[ "$(git -C "$REPO_ROOT" config --get remote.origin.url)" = "$EXPECTED_ORIGIN_URL" ] \
  || die "unexpected Git origin"
git -C "$REPO_ROOT" fetch --quiet --no-tags origin \
  "refs/heads/dev:refs/remotes/origin/dev" \
  || die "could not refresh origin/dev"
COMMIT="$(git -C "$REPO_ROOT" rev-parse --verify HEAD^{commit})"
REMOTE_COMMIT="$(git -C "$REPO_ROOT" rev-parse --verify refs/remotes/origin/dev^{commit})"
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || die "HEAD is not a full SHA"
[ "$COMMIT" = "$REMOTE_COMMIT" ] || die "local dev HEAD must exactly equal remote origin/dev"
SOURCE_MANIFEST_CONTRACT_SHA256="$(
  python3 "$PROVENANCE" contract-sha256 --contract "$SOURCE_MANIFEST_CONTRACT"
)"
RELEASE_CONTRACT_SHA256="$(
  python3 "$BUNDLE_PROVENANCE" contract-sha256 --contract "$RELEASE_CONTRACT"
)"
python3 "$BUNDLE_PROVENANCE" assert-release-ready --contract "$RELEASE_CONTRACT" \
  || die "TeamAgent core/media release contract is not approved for release"
mapfile -t PRODUCTION_APP_RECORD < <(
  python3 "$BUNDLE_PROVENANCE" production-record \
    --deploy-log "$REPO_ROOT/infra/deploy_log.md" \
    --format lines
)
[ "${#PRODUCTION_APP_RECORD[@]}" -eq 4 ] || die "production app record is incomplete"
APP_VERSION_ID="${PRODUCTION_APP_RECORD[0]}"
APP_SHA256="${PRODUCTION_APP_RECORD[1]}"
VAULT_MANIFEST_SHA256="${PRODUCTION_APP_RECORD[2]}"
BUILD_INPUTS_SHA256="${PRODUCTION_APP_RECORD[3]}"
BAKED_APP_HTML_VERSION_ID="$(
  jq -er '.app_html.baked_fallback.s3_version_id' "$RELEASE_CONTRACT"
)" || die "release contract lacks an exact baked fallback S3 VersionId"
BAKED_APP_HTML_SHA256="$(
  jq -er '.app_html.baked_fallback.sha256' "$RELEASE_CONTRACT"
)"
APP_PROVENANCE_SHA256="$(
  python3 "$BUNDLE_PROVENANCE" app-provenance-sha256 \
    --contract "$RELEASE_CONTRACT" \
    --deploy-log "$REPO_ROOT/infra/deploy_log.md"
)"

identity() {
  AWS_PAGER="" aws sts get-caller-identity --query '[Account,Arn]' --output text
}
INITIAL_IDENTITY="$(identity)"
[[ "$INITIAL_IDENTITY" != *$'\n'* && "$INITIAL_IDENTITY" != *$'\r'* ]] \
  || die "malformed initial AWS identity"
IFS=$'\t' read -r INITIAL_ACCOUNT INITIAL_ARN EXTRA <<<"$INITIAL_IDENTITY"
[ -z "${EXTRA:-}" ] && [ "$INITIAL_ACCOUNT" = "$ACCOUNT_ID" ] \
  && [ "$INITIAL_ARN" = "$EXPECTED_CALLER_ARN" ] \
  || die "launcher must start as the exact dedicated caller in account $ACCOUNT_ID"
unset INITIAL_IDENTITY INITIAL_ACCOUNT INITIAL_ARN EXTRA

SESSION_CREDENTIALS="$(
  AWS_PAGER="" aws sts assume-role \
    --region "$REGION" \
    --role-arn "$LAUNCHER_ROLE_ARN" \
    --role-session-name "$LAUNCHER_SESSION_NAME" \
    --duration-seconds 10800 \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken,Expiration]' \
    --output text
)" || die "could not assume the dedicated launcher role"
[[ "$SESSION_CREDENTIALS" != *$'\n'* && "$SESSION_CREDENTIALS" != *$'\r'* ]] \
  || die "malformed launcher credentials"
IFS=$'\t' read -r AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN EXPIRATION EXTRA \
  <<<"$SESSION_CREDENTIALS"
[ -z "${EXTRA:-}" ] || die "malformed launcher credentials"
for credential in "$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY" "$AWS_SESSION_TOKEN" "$EXPIRATION"; do
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
  || die "unexpected pinned launcher session"
unset SESSION_IDENTITY SESSION_ACCOUNT SESSION_ARN EXTRA

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/teamagent-build-launcher.XXXXXXXX")"
cleanup() {
  rm -rf -- "$TMP_DIR"
}
trap cleanup EXIT

DOWNLOADED_APP_VERSION="$(
  AWS_PAGER="" aws s3api get-object \
    --region "$REGION" \
    --bucket "$APP_BUCKET" \
    --key "$APP_KEY" \
    --version-id "$APP_VERSION_ID" \
    --expected-bucket-owner "$ACCOUNT_ID" \
    --query VersionId \
    --output text \
    "$TMP_DIR/app.html"
)"
[ "$DOWNLOADED_APP_VERSION" = "$APP_VERSION_ID" ] || die "app HTML VersionId mismatch"
[ "$(sha256sum "$TMP_DIR/app.html" | awk '{print $1}')" = "$APP_SHA256" ] \
  || die "app HTML does not match the production canonical hash"
DOWNLOADED_FALLBACK_VERSION="$(
  AWS_PAGER="" aws s3api get-object \
    --region "$REGION" \
    --bucket "$APP_BUCKET" \
    --key "$APP_KEY" \
    --version-id "$BAKED_APP_HTML_VERSION_ID" \
    --expected-bucket-owner "$ACCOUNT_ID" \
    --query VersionId \
    --output text \
    "$TMP_DIR/baked-app.html"
)"
[ "$DOWNLOADED_FALLBACK_VERSION" = "$BAKED_APP_HTML_VERSION_ID" ] \
  || die "baked fallback VersionId mismatch"
[ "$(sha256sum "$TMP_DIR/baked-app.html" | awk '{print $1}')" = "$BAKED_APP_HTML_SHA256" ] \
  || die "baked fallback does not match its approved hash"
[ "$APP_SHA256" != "$BAKED_APP_HTML_SHA256" ] \
  || die "production app cannot be substituted for the distinct baked fallback"

environment_json() {
  local output="$1"
  shift
  python3 - "$output" "$@" <<'PY'
import json
import sys

output, *pairs = sys.argv[1:]
if any("=" not in pair for pair in pairs):
    raise SystemExit("invalid environment pair")
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
  local source_version="${3-}"
  local args=(
    codebuild start-build
    --region "$REGION"
    --project-name "$project"
    --environment-variables-override "file://$env_file"
    --query build.id
    --output text
  )
  if [ -n "$source_version" ]; then
    args+=(--source-version "$source_version")
  fi
  AWS_PAGER="" aws "${args[@]}"
}

wait_build() {
  local build_id="$1"
  local expected_source="${2-}"
  local deadline=$((SECONDS + TIMEOUT_SECONDS))
  local state status source extra
  while :; do
    state="$(
      AWS_PAGER="" aws codebuild batch-get-builds \
        --region "$REGION" \
        --ids "$build_id" \
        --query 'builds[0].[buildStatus,sourceVersion]' \
        --output text
    )"
    [[ "$state" != *$'\n'* && "$state" != *$'\r'* ]] || die "malformed build state"
    IFS=$'\t' read -r status source extra <<<"$state"
    [ -z "${extra:-}" ] || die "malformed build state"
    if [ -n "$expected_source" ] && [ "$source" != "$expected_source" ]; then
      die "CodeBuild sourceVersion mismatch"
    fi
    case "$status" in
      IN_PROGRESS)
        [ "$SECONDS" -lt "$deadline" ] || die "timed out waiting for $build_id"
        sleep "$POLL_SECONDS"
        ;;
      SUCCEEDED) return 0 ;;
      FAILED|FAULT|STOPPED|TIMED_OUT) die "build failed: $build_id ($status)" ;;
      *) die "unexpected CodeBuild state: $status" ;;
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

PUBLISHER_ENV="$TMP_DIR/publisher-env.json"
environment_json "$PUBLISHER_ENV" \
  "EXPECTED_COMMIT=$COMMIT" \
  "SOURCE_MANIFEST_CONTRACT_SHA256=$SOURCE_MANIFEST_CONTRACT_SHA256" \
  "RELEASE_CONTRACT_SHA256=$RELEASE_CONTRACT_SHA256"
PUBLISHER_BUILD_ID="$(start_build "$SOURCE_PUBLISHER_PROJECT" "$PUBLISHER_ENV" "$COMMIT")"
[[ "$PUBLISHER_BUILD_ID" == "$SOURCE_PUBLISHER_PROJECT:"* ]] || die "invalid publisher build ID"
wait_build "$PUBLISHER_BUILD_ID" "$COMMIT"

SOURCE_VERSION_ID="$(exported_build_value "$PUBLISHER_BUILD_ID" PUBLISHED_SOURCE_VERSION_ID)"
DECLARATION_KEY="$(exported_build_value "$PUBLISHER_BUILD_ID" SOURCE_DECLARATION_KEY)"
DECLARATION_VERSION="$(exported_build_value "$PUBLISHER_BUILD_ID" SOURCE_DECLARATION_VERSION_ID)"
DECLARATION_SHA256="$(exported_build_value "$PUBLISHER_BUILD_ID" SOURCE_DECLARATION_SHA256)"
DECLARATION_SIGNATURE_KEY="$(exported_build_value "$PUBLISHER_BUILD_ID" SOURCE_DECLARATION_SIGNATURE_KEY)"
DECLARATION_SIGNATURE_VERSION="$(
  exported_build_value "$PUBLISHER_BUILD_ID" SOURCE_DECLARATION_SIGNATURE_VERSION_ID
)"
[[ "$DECLARATION_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "invalid source declaration hash"
[ "$DECLARATION_SIGNATURE_KEY" = "$DECLARATION_KEY.sig" ] || die "source signature key mismatch"

IMAGE_ENV="$TMP_DIR/image-env.json"
environment_json "$IMAGE_ENV" \
  "GIT_COMMIT=$COMMIT" \
  "GIT_BRANCH=dev" \
  "APP_HTML_VERSION_ID=$APP_VERSION_ID" \
  "APP_HTML_SHA256=$APP_SHA256" \
  "VAULT_MANIFEST_SHA256=$VAULT_MANIFEST_SHA256" \
  "BUILD_INPUTS_SHA256=$BUILD_INPUTS_SHA256" \
  "BAKED_APP_HTML_VERSION_ID=$BAKED_APP_HTML_VERSION_ID" \
  "BAKED_APP_HTML_SHA256=$BAKED_APP_HTML_SHA256" \
  "APP_PROVENANCE_SHA256=$APP_PROVENANCE_SHA256" \
  "SOURCE_MANIFEST_CONTRACT_SHA256=$SOURCE_MANIFEST_CONTRACT_SHA256" \
  "RELEASE_CONTRACT_SHA256=$RELEASE_CONTRACT_SHA256" \
  "SOURCE_ARCHIVE_VERSION_ID=$SOURCE_VERSION_ID" \
  "SOURCE_DECLARATION_KEY=$DECLARATION_KEY" \
  "SOURCE_DECLARATION_VERSION_ID=$DECLARATION_VERSION" \
  "SOURCE_DECLARATION_SHA256=$DECLARATION_SHA256" \
  "SOURCE_DECLARATION_SIGNATURE_KEY=$DECLARATION_SIGNATURE_KEY" \
  "SOURCE_DECLARATION_SIGNATURE_VERSION_ID=$DECLARATION_SIGNATURE_VERSION"
IMAGE_BUILD_ID="$(start_build "$IMAGE_PROJECT" "$IMAGE_ENV" "$SOURCE_VERSION_ID")"
[[ "$IMAGE_BUILD_ID" == "$IMAGE_PROJECT:"* ]] || die "invalid image build ID"
wait_build "$IMAGE_BUILD_ID" "$SOURCE_VERSION_ID"

CORE_TAG_DIGEST="$(exported_build_value "$IMAGE_BUILD_ID" MCP_CORE_TAG_DIGEST)"
CORE_VERIFIED_DIGEST="$(exported_build_value "$IMAGE_BUILD_ID" MCP_CORE_ARM64_DIGEST)"
MEDIA_TAG_DIGEST="$(exported_build_value "$IMAGE_BUILD_ID" MCP_MEDIA_TAG_DIGEST)"
MEDIA_VERIFIED_DIGEST="$(exported_build_value "$IMAGE_BUILD_ID" MCP_MEDIA_ARM64_DIGEST)"
for digest in \
  "$CORE_TAG_DIGEST" "$CORE_VERIFIED_DIGEST" \
  "$MEDIA_TAG_DIGEST" "$MEDIA_VERIFIED_DIGEST"; do
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "builder exported an invalid digest"
done

SUBJECTS_JSON="$(
  jq -cn \
    --arg core_digest "$CORE_VERIFIED_DIGEST" \
    --arg media_digest "$MEDIA_VERIFIED_DIGEST" \
    '[{
      name: "core",
      quarantine_repository: "teamagent-mcp-quarantine",
      candidate_repository: "teamagent-mcp-verified-candidates",
      release_repository: "teamagent-mcp",
      digest: $core_digest
    },{
      name: "media",
      quarantine_repository: "teamagent-media-worker-quarantine",
      candidate_repository: "teamagent-media-worker-verified-candidates",
      release_repository: "teamagent-media-worker",
      digest: $media_digest
    }]'
)"
ATTESTOR_ENV="$TMP_DIR/attestor-env.json"
environment_json "$ATTESTOR_ENV" \
  "PIPELINE=mcp" \
  "PROMOTION_CHANNEL=verified-candidate" \
  "SOURCE_COMMIT=$COMMIT" \
  "CONTRACT_SHA256=$RELEASE_CONTRACT_SHA256" \
  "SOURCE_EVIDENCE_BUCKET=$EVIDENCE_BUCKET" \
  "SOURCE_EVIDENCE_KEY=$DECLARATION_KEY" \
  "SOURCE_EVIDENCE_VERSION_ID=$DECLARATION_VERSION" \
  "SOURCE_EVIDENCE_SHA256=$DECLARATION_SHA256" \
  "SOURCE_EVIDENCE_SIGNATURE_KEY=$DECLARATION_SIGNATURE_KEY" \
  "SOURCE_EVIDENCE_SIGNATURE_VERSION_ID=$DECLARATION_SIGNATURE_VERSION" \
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
  "PIPELINE=mcp" \
  "PROMOTION_CHANNEL=verified-candidate" \
  "SOURCE_COMMIT=$COMMIT" \
  "CONTRACT_SHA256=$RELEASE_CONTRACT_SHA256" \
  "RECEIPT_KEY=$RECEIPT_KEY" \
  "RECEIPT_VERSION_ID=$RECEIPT_VERSION" \
  "RECEIPT_SIGNATURE_KEY=$RECEIPT_SIGNATURE_KEY" \
  "RECEIPT_SIGNATURE_VERSION_ID=$RECEIPT_SIGNATURE_VERSION"
PROMOTER_BUILD_ID="$(start_build "$PROMOTER_PROJECT" "$PROMOTER_ENV")"
[[ "$PROMOTER_BUILD_ID" == "$PROMOTER_PROJECT:"* ]] || die "invalid promoter build ID"
wait_build "$PROMOTER_BUILD_ID"

for subject in core media; do
  case "$subject" in
    core)
      candidate_repository="$VERIFIED_CANDIDATE_REPOSITORY"
      expected_digest="$CORE_VERIFIED_DIGEST"
      ;;
    media)
      candidate_repository="$MEDIA_VERIFIED_CANDIDATE_REPOSITORY"
      expected_digest="$MEDIA_VERIFIED_DIGEST"
      ;;
  esac
  verified_candidate_digest="$(
    AWS_PAGER="" aws ecr describe-images \
      --region "$REGION" \
      --registry-id "$ACCOUNT_ID" \
      --repository-name "$candidate_repository" \
      --image-ids "imageTag=verified-$COMMIT-$subject" \
      --query 'imageDetails[0].imageDigest' \
      --output text
  )"
  [ "$verified_candidate_digest" = "$expected_digest" ] \
    || die "$subject verified-candidate digest differs from signed quarantine"
done

echo "Build-only signed candidate publication complete:"
echo "  pipeline=mcp"
echo "  commit=$COMMIT"
echo "  core_quarantine_digest=$CORE_VERIFIED_DIGEST"
echo "  core_verified_candidate_digest=$CORE_VERIFIED_DIGEST"
echo "  media_quarantine_digest=$MEDIA_VERIFIED_DIGEST"
echo "  media_verified_candidate_digest=$MEDIA_VERIFIED_DIGEST"
echo "  receipt_key=$RECEIPT_KEY"
echo "  receipt_version_id=$RECEIPT_VERSION"
echo "  receipt_signature_key=$RECEIPT_SIGNATURE_KEY"
echo "  receipt_signature_version_id=$RECEIPT_SIGNATURE_VERSION"
echo "No ECS, EventBridge, task definition, service, schedule, or Terraform change was made."
