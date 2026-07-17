#!/usr/bin/env bash
# Build-only launcher for the TeamAgent MCP image.
#
# The script archives a clean Git commit, pins the exact S3 VersionId into
# CodeBuild, waits for the vulnerability-gated build, then verifies the remote
# OCI config labels.  It deliberately performs no ECS/Fargate deployment.
set -euo pipefail
umask 077

REGION="ap-northeast-1"
EXPECTED_ACCOUNT_ID="718959508629"
EXPECTED_CALLER_ARN="arn:aws:iam::718959508629:user/AIIAdev"
LAUNCHER_ROLE_ARN="arn:aws:iam::718959508629:role/teamagent-dev-codebuild-launcher"
LAUNCHER_SESSION_NAME="teamagent-build-launcher"
EXPECTED_SESSION_ARN="arn:aws:sts::718959508629:assumed-role/teamagent-dev-codebuild-launcher/teamagent-build-launcher"
EXPECTED_BRANCH="dev"
EXPECTED_ORIGIN_URL="git@github.com:noirelumiere00/TeamAgent.git"
SOURCE_BUCKET="teamagent-dev-raw-files"
SOURCE_KEY="codebuild/source.zip"
APP_HTML_BUCKET="teamagent-dev-raw-files"
APP_HTML_KEY="codebuild/connect-web-app.html"
CODEBUILD_PROJECT="teamagent-dev-image-builder"
ECR_REPOSITORY="teamagent-mcp"
ECR_QUARANTINE_REPOSITORY="teamagent-mcp-quarantine"
IMAGE_TAG=""
WITH_SCRAPE_TOOLS=""
POLL_SECONDS=15
TIMEOUT_SECONDS=7200

usage() {
  cat <<'EOF'
usage: build_teamagent_image.sh --image-tag <tag> --with-scrape-tools true [options]

Required:
  --image-tag <tag>                 Immutable candidate tag (safe ECR characters only)
  --with-scrape-tools true          Required production profile; there is no implicit default

Options:
  --poll-seconds <seconds>          Build status polling interval (default: 15)
  --timeout-seconds <seconds>       Overall CodeBuild wait timeout (default: 7200)
  -h, --help                        Show this help

This command builds and verifies a candidate only. It never updates a service,
task definition, schedule, or any other deployment target.
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
    --image-tag)
      require_value "$1" "${2-}"
      IMAGE_TAG="$2"
      shift 2
      ;;
    --with-scrape-tools)
      require_value "$1" "${2-}"
      WITH_SCRAPE_TOOLS="$2"
      shift 2
      ;;
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

[ -n "$IMAGE_TAG" ] || die "--image-tag is required"
[ -n "$WITH_SCRAPE_TOOLS" ] || die "--with-scrape-tools true is required"
[[ "$IMAGE_TAG" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] \
  || die "unsafe image tag; use 1-128 characters from [A-Za-z0-9._-] and start alphanumeric"
[ "$WITH_SCRAPE_TOOLS" = "true" ] \
  || die "--with-scrape-tools must be explicitly set to true for production candidate builds"
[[ "$POLL_SECONDS" =~ ^[1-9][0-9]*$ ]] || die "--poll-seconds must be a positive integer"
[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || die "--timeout-seconds must be positive"
[ "$TIMEOUT_SECONDS" -ge "$POLL_SECONDS" ] || die "timeout must be at least one poll interval"

for tool in git python3 aws curl; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done

# Refuse configured or environment-provided service endpoints before the first
# identity call. Fixed account/region/resource values below are not CLI inputs.
export AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true
unset AWS_ENDPOINT_URL AWS_ENDPOINT_URL_STS AWS_ENDPOINT_URL_S3 AWS_ENDPOINT_URL_ECR

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)" \
  || die "script is not inside a Git worktree"
PROVENANCE="$REPO_ROOT/infra/codebuild/source_provenance.py"
IMAGE_RESOLVER="$REPO_ROOT/infra/codebuild/resolve_ecr_image.py"
RUNTIME_CONTRACT="$REPO_ROOT/infra/codebuild/teamagent_runtime_contract.json"
[ -f "$PROVENANCE" ] || die "source provenance verifier is missing"
[ -f "$IMAGE_RESOLVER" ] || die "ECR image resolver is missing"
[ -f "$RUNTIME_CONTRACT" ] || die "TeamAgent runtime contract is missing"

if [ -n "$(git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=all --ignore-submodules=none)" ]; then
  die "Git worktree is dirty (tracked or untracked changes); commit or remove them first"
fi
COMMIT="$(git -C "$REPO_ROOT" rev-parse --verify HEAD^{commit})"
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || die "HEAD is not a full SHA-1 commit"
BRANCH="$(git -C "$REPO_ROOT" symbolic-ref --quiet --short HEAD)" \
  || die "detached HEAD is not allowed; check out the branch being built"
[ -n "$BRANCH" ] || die "current Git branch is empty"
[ "$BRANCH" = "$EXPECTED_BRANCH" ] \
  || die "builds must run from the local dev branch"
ORIGIN_URL="$(git -C "$REPO_ROOT" config --get remote.origin.url)" \
  || die "Git remote origin is missing"
[ "$ORIGIN_URL" = "$EXPECTED_ORIGIN_URL" ] \
  || die "unexpected Git origin URL"
git -C "$REPO_ROOT" fetch --quiet --no-tags origin \
  "refs/heads/dev:refs/remotes/origin/dev" \
  || die "could not refresh origin/dev"
REMOTE_DEV_COMMIT="$(git -C "$REPO_ROOT" rev-parse --verify refs/remotes/origin/dev^{commit})" \
  || die "origin/dev is missing"
[ "$COMMIT" = "$REMOTE_DEV_COMMIT" ] \
  || die "local dev HEAD must exactly equal remote origin/dev"
unset ORIGIN_URL REMOTE_DEV_COMMIT

RUNTIME_CONTRACT_SHA256="$(
  python3 "$PROVENANCE" contract-sha256 --contract "$RUNTIME_CONTRACT"
)" || die "TeamAgent runtime contract is invalid"
[[ "$RUNTIME_CONTRACT_SHA256" =~ ^[0-9a-f]{64}$ ]] \
  || die "TeamAgent runtime contract returned an invalid SHA-256"
python3 "$PROVENANCE" assert-release-ready --contract "$RUNTIME_CONTRACT" \
  || die "TeamAgent runtime contract is not approved for release"

INITIAL_IDENTITY="$(
  AWS_PAGER="" aws sts get-caller-identity \
    --query '[Account,Arn]' \
    --output text
)"
[[ "$INITIAL_IDENTITY" != *$'\n'* && "$INITIAL_IDENTITY" != *$'\r'* ]] \
  || die "AWS returned a malformed initial identity"
IFS=$'\t' read -r ACTUAL_ACCOUNT_ID ACTUAL_CALLER_ARN EXTRA_IDENTITY <<<"$INITIAL_IDENTITY"
[ -z "${EXTRA_IDENTITY:-}" ] || die "AWS returned a malformed initial identity"
[ "$ACTUAL_ACCOUNT_ID" = "$EXPECTED_ACCOUNT_ID" ] \
  || die "refusing to build in AWS account ${ACTUAL_ACCOUNT_ID:-unknown}; expected $EXPECTED_ACCOUNT_ID"
[ "$ACTUAL_CALLER_ARN" = "$EXPECTED_CALLER_ARN" ] \
  || die "launcher must start as $EXPECTED_CALLER_ARN"
unset INITIAL_IDENTITY ACTUAL_ACCOUNT_ID ACTUAL_CALLER_ARN EXTRA_IDENTITY

SESSION_CREDENTIALS="$(
  AWS_PAGER="" aws sts assume-role \
    --region "$REGION" \
    --role-arn "$LAUNCHER_ROLE_ARN" \
    --role-session-name "$LAUNCHER_SESSION_NAME" \
    --duration-seconds 10800 \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken,Expiration]' \
    --output text
)" || die "could not assume the dedicated CodeBuild launcher role"
[[ "$SESSION_CREDENTIALS" != *$'\n'* && "$SESSION_CREDENTIALS" != *$'\r'* ]] \
  || die "STS returned malformed launcher credentials"
IFS=$'\t' read -r \
  AWS_ACCESS_KEY_ID \
  AWS_SECRET_ACCESS_KEY \
  AWS_SESSION_TOKEN \
  AWS_CREDENTIAL_EXPIRATION \
  EXTRA_CREDENTIAL <<<"$SESSION_CREDENTIALS"
[ -z "${EXTRA_CREDENTIAL:-}" ] || die "STS returned malformed launcher credentials"
for credential in \
  "$AWS_ACCESS_KEY_ID" \
  "$AWS_SECRET_ACCESS_KEY" \
  "$AWS_SESSION_TOKEN" \
  "$AWS_CREDENTIAL_EXPIRATION"; do
  [ -n "$credential" ] && [ "$credential" != "None" ] \
    || die "STS returned incomplete launcher credentials"
done
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
export AWS_DEFAULT_REGION="$REGION" AWS_REGION="$REGION"
export AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null
unset AWS_PROFILE AWS_DEFAULT_PROFILE SESSION_CREDENTIALS EXTRA_CREDENTIAL credential

SESSION_IDENTITY="$(
  AWS_PAGER="" aws sts get-caller-identity \
    --query '[Account,Arn]' \
    --output text
)"
[[ "$SESSION_IDENTITY" != *$'\n'* && "$SESSION_IDENTITY" != *$'\r'* ]] \
  || die "AWS returned a malformed launcher role identity"
IFS=$'\t' read -r SESSION_ACCOUNT_ID SESSION_ARN EXTRA_SESSION_IDENTITY <<<"$SESSION_IDENTITY"
[ -z "${EXTRA_SESSION_IDENTITY:-}" ] || die "AWS returned a malformed launcher role identity"
[ "$SESSION_ACCOUNT_ID" = "$EXPECTED_ACCOUNT_ID" ] \
  || die "launcher role session is in the wrong AWS account"
[ "$SESSION_ARN" = "$EXPECTED_SESSION_ARN" ] \
  || die "unexpected launcher role session ARN"
unset SESSION_IDENTITY SESSION_ACCOUNT_ID SESSION_ARN EXTRA_SESSION_IDENTITY

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/teamagent-codebuild.XXXXXXXX")"
cleanup() {
  rm -rf -- "$TMP_DIR"
}
trap cleanup EXIT

MANIFEST="$TMP_DIR/.teamagent-source-manifest.json"
SOURCE_ZIP="$TMP_DIR/source.zip"
EXTRACTED="$TMP_DIR/extracted"
ENV_OVERRIDES="$TMP_DIR/codebuild-env.json"
QUARANTINE_PARENT_BATCH_RESPONSE="$TMP_DIR/ecr-quarantine-parent-batch-get-image.json"
RELEASE_BATCH_RESPONSE="$TMP_DIR/ecr-release-batch-get-image.json"
OCI_CONFIG="$TMP_DIR/oci-config.json"
APP_HTML_FILE="$TMP_DIR/connect-web-app.html"

[ "$SOURCE_BUCKET" = "$APP_HTML_BUCKET" ] \
  || die "source and app.html must use the same fixed versioned bucket"
BUCKET_VERSIONING="$(
  AWS_PAGER="" aws s3api get-bucket-versioning \
    --region "$REGION" \
    --bucket "$SOURCE_BUCKET" \
    --expected-bucket-owner "$EXPECTED_ACCOUNT_ID" \
    --query Status \
    --output text
)"
[ "$BUCKET_VERSIONING" = "Enabled" ] \
  || die "source/app S3 bucket versioning must be Enabled"
APP_HTML_VERSION_ID="$(
  AWS_PAGER="" aws s3api head-object \
    --region "$REGION" \
    --bucket "$APP_HTML_BUCKET" \
    --key "$APP_HTML_KEY" \
    --expected-bucket-owner "$EXPECTED_ACCOUNT_ID" \
    --query VersionId \
    --output text
)"
case "$APP_HTML_VERSION_ID" in
  ""|None|null|*[!A-Za-z0-9._~+/=-]*) \
    die "app.html S3 object did not return a usable VersionId" ;;
esac
[ "${#APP_HTML_VERSION_ID}" -le 1024 ] \
  || die "app.html S3 object did not return a usable VersionId"
DOWNLOADED_APP_HTML_VERSION_ID="$(
  AWS_PAGER="" aws s3api get-object \
    --region "$REGION" \
    --bucket "$APP_HTML_BUCKET" \
    --key "$APP_HTML_KEY" \
    --version-id "$APP_HTML_VERSION_ID" \
    --expected-bucket-owner "$EXPECTED_ACCOUNT_ID" \
    --query VersionId \
    --output text \
    "$APP_HTML_FILE"
)"
[ "$DOWNLOADED_APP_HTML_VERSION_ID" = "$APP_HTML_VERSION_ID" ] \
  || die "downloaded app.html VersionId does not match the resolved version"
[ -s "$APP_HTML_FILE" ] || die "versioned app.html object is empty"
APP_HTML_SHA256="$(
  python3 - "$APP_HTML_FILE" <<'PY'
import hashlib
import sys

digest = hashlib.sha256()
with open(sys.argv[1], "rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
print(digest.hexdigest())
PY
)"
[[ "$APP_HTML_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "could not hash versioned app.html"

echo "Preparing Git archive for $COMMIT ($BRANCH), WITH_SCRAPE_TOOLS=$WITH_SCRAPE_TOOLS"
python3 "$PROVENANCE" create-manifest \
  --repo-root "$REPO_ROOT" \
  --commit "$COMMIT" \
  --branch "$BRANCH" \
  --with-scrape-tools "$WITH_SCRAPE_TOOLS" \
  --app-html-version-id "$APP_HTML_VERSION_ID" \
  --app-html-sha256 "$APP_HTML_SHA256" \
  --output "$MANIFEST"
git -C "$REPO_ROOT" archive \
  --format=zip \
  --output="$SOURCE_ZIP" \
  --add-file="$MANIFEST" \
  "$COMMIT"
mkdir -p "$EXTRACTED"
python3 -m zipfile -e "$SOURCE_ZIP" "$EXTRACTED"
python3 "$PROVENANCE" verify-source \
  --source-root "$EXTRACTED" \
  --manifest "$EXTRACTED/.teamagent-source-manifest.json" \
  --expected-commit "$COMMIT" \
  --expected-branch "$BRANCH" \
  --expected-with-scrape-tools "$WITH_SCRAPE_TOOLS" \
  --expected-app-html-version-id "$APP_HTML_VERSION_ID" \
  --expected-app-html-sha256 "$APP_HTML_SHA256" \
  --expected-runtime-contract-sha256 "$RUNTIME_CONTRACT_SHA256"

if [ "$(git -C "$REPO_ROOT" rev-parse --verify HEAD^{commit})" != "$COMMIT" ] \
  || [ -n "$(git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=all --ignore-submodules=none)" ]; then
  die "Git worktree changed while the source archive was being prepared"
fi

echo "Uploading source archive to fixed key s3://$SOURCE_BUCKET/$SOURCE_KEY"
VERSION_ID="$(
  AWS_PAGER="" aws s3api put-object \
    --region "$REGION" \
    --bucket "$SOURCE_BUCKET" \
    --key "$SOURCE_KEY" \
    --body "$SOURCE_ZIP" \
    --expected-bucket-owner "$EXPECTED_ACCOUNT_ID" \
    --content-type application/zip \
    --server-side-encryption AES256 \
    --query VersionId \
    --output text
)"
if [ -z "$VERSION_ID" ] || [ "$VERSION_ID" = "None" ] || [ "$VERSION_ID" = "null" ] \
  || [[ "$VERSION_ID" == *$'\n'* ]] || [[ "$VERSION_ID" == *$'\r'* ]]; then
  die "S3 did not return a usable VersionId; bucket versioning is required"
fi

GIT_COMMIT="$COMMIT" \
GIT_BRANCH="$BRANCH" \
IMAGE_TAG="$IMAGE_TAG" \
WITH_SCRAPE_TOOLS="$WITH_SCRAPE_TOOLS" \
APP_HTML_VERSION_ID="$APP_HTML_VERSION_ID" \
APP_HTML_SHA256="$APP_HTML_SHA256" \
RUNTIME_CONTRACT_SHA256="$RUNTIME_CONTRACT_SHA256" \
python3 - "$ENV_OVERRIDES" <<'PY'
import json
import os
import sys

path = sys.argv[1]
names = (
    "GIT_COMMIT",
    "GIT_BRANCH",
    "IMAGE_TAG",
    "WITH_SCRAPE_TOOLS",
    "APP_HTML_VERSION_ID",
    "APP_HTML_SHA256",
    "RUNTIME_CONTRACT_SHA256",
)
values = {name: os.environ[name] for name in names}
payload = [{"name": name, "value": value, "type": "PLAINTEXT"} for name, value in values.items()]
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, separators=(",", ":"))
PY

echo "Starting CodeBuild candidate $IMAGE_TAG with pinned S3 VersionId"
BUILD_ID="$(
  AWS_PAGER="" aws codebuild start-build \
    --region "$REGION" \
    --project-name "$CODEBUILD_PROJECT" \
    --source-version "$VERSION_ID" \
    --environment-variables-override "file://$ENV_OVERRIDES" \
    --query build.id \
    --output text
)"
[ -n "$BUILD_ID" ] && [ "$BUILD_ID" != "None" ] && [ "$BUILD_ID" != "null" ] \
  || die "CodeBuild did not return a build ID"
[[ "$BUILD_ID" != *$'\n'* && "$BUILD_ID" != *$'\r'* ]] || die "invalid CodeBuild build ID"

DEADLINE=$((SECONDS + TIMEOUT_SECONDS))
BUILD_STATUS=""
BUILD_SOURCE_VERSION=""
while :; do
  BUILD_STATE="$(
    AWS_PAGER="" aws codebuild batch-get-builds \
      --region "$REGION" \
      --ids "$BUILD_ID" \
      --query 'builds[0].[buildStatus,sourceVersion]' \
      --output text
  )"
  [[ "$BUILD_STATE" != *$'\n'* && "$BUILD_STATE" != *$'\r'* ]] \
    || die "CodeBuild returned a multi-line build state"
  IFS=$'\t' read -r BUILD_STATUS BUILD_SOURCE_VERSION EXTRA_STATE <<<"$BUILD_STATE"
  [ -z "${EXTRA_STATE:-}" ] || die "CodeBuild returned an unexpected build state"
  [ -n "$BUILD_STATUS" ] && [ "$BUILD_STATUS" != "None" ] \
    || die "CodeBuild build state is missing"
  if [ -n "${BUILD_SOURCE_VERSION:-}" ] && [ "$BUILD_SOURCE_VERSION" != "None" ] \
    && [ "$BUILD_SOURCE_VERSION" != "$VERSION_ID" ]; then
    die "CodeBuild sourceVersion does not match the uploaded S3 VersionId"
  fi
  case "$BUILD_STATUS" in
    IN_PROGRESS)
      [ "$SECONDS" -lt "$DEADLINE" ] || die "timed out waiting for CodeBuild"
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

[ "$BUILD_SOURCE_VERSION" = "$VERSION_ID" ] \
  || die "CodeBuild did not use the exact uploaded S3 VersionId"
[ "$BUILD_STATUS" = "SUCCEEDED" ] \
  || die "CodeBuild candidate failed with status $BUILD_STATUS (build ID: $BUILD_ID)"

QUARANTINE_TAG_DIGEST="$(
  AWS_PAGER="" aws ecr describe-images \
    --region "$REGION" \
    --registry-id "$EXPECTED_ACCOUNT_ID" \
    --repository-name "$ECR_QUARANTINE_REPOSITORY" \
    --image-ids "imageTag=$IMAGE_TAG" \
    --query 'imageDetails[0].imageDigest' \
    --output text
)"
[[ "$QUARANTINE_TAG_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || die "ECR returned an invalid quarantine tag digest"

AWS_PAGER="" aws ecr batch-get-image \
  --region "$REGION" \
  --registry-id "$EXPECTED_ACCOUNT_ID" \
  --repository-name "$ECR_QUARANTINE_REPOSITORY" \
  --image-ids "imageDigest=$QUARANTINE_TAG_DIGEST" \
  --accepted-media-types \
    application/vnd.docker.distribution.manifest.list.v2+json \
    application/vnd.oci.image.index.v1+json \
    application/vnd.docker.distribution.manifest.v2+json \
    application/vnd.oci.image.manifest.v1+json \
  --output json >"$QUARANTINE_PARENT_BATCH_RESPONSE"
VERIFIED_QUARANTINE_DIGEST="$(
  python3 "$IMAGE_RESOLVER" resolve-platform \
    --batch-response "$QUARANTINE_PARENT_BATCH_RESPONSE" \
    --expected-image-digest "$QUARANTINE_TAG_DIGEST" \
    --os linux \
    --architecture arm64
)"
[[ "$VERIFIED_QUARANTINE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || die "ECR image resolver returned an invalid quarantine arm64 digest"

RELEASE_DIGEST="$(
  AWS_PAGER="" aws ecr describe-images \
    --region "$REGION" \
    --registry-id "$EXPECTED_ACCOUNT_ID" \
    --repository-name "$ECR_REPOSITORY" \
    --image-ids "imageTag=$IMAGE_TAG" \
    --query 'imageDetails[0].imageDigest' \
    --output text
)"
[ "$RELEASE_DIGEST" = "$VERIFIED_QUARANTINE_DIGEST" ] \
  || die "release digest does not equal the verified quarantine digest"

AWS_PAGER="" aws ecr batch-get-image \
  --region "$REGION" \
  --registry-id "$EXPECTED_ACCOUNT_ID" \
  --repository-name "$ECR_REPOSITORY" \
  --image-ids "imageDigest=$RELEASE_DIGEST" \
  --accepted-media-types \
    application/vnd.docker.distribution.manifest.v2+json \
    application/vnd.oci.image.manifest.v1+json \
  --output json >"$RELEASE_BATCH_RESPONSE"
CONFIG_DIGEST="$(
  python3 "$PROVENANCE" ecr-config-digest \
    --batch-response "$RELEASE_BATCH_RESPONSE" \
    --expected-image-digest "$RELEASE_DIGEST"
)"
[[ "$CONFIG_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || die "ECR returned an invalid OCI config digest"

DOWNLOAD_URL="$(
  AWS_PAGER="" aws ecr get-download-url-for-layer \
    --region "$REGION" \
    --registry-id "$EXPECTED_ACCOUNT_ID" \
    --repository-name "$ECR_REPOSITORY" \
    --layer-digest "$CONFIG_DIGEST" \
    --query downloadUrl \
    --output text
)"
[[ "$DOWNLOAD_URL" == https://* ]] || die "ECR did not return an HTTPS OCI config URL"
curl --proto '=https' --tlsv1.2 --fail --silent --show-error \
  --output "$OCI_CONFIG" "$DOWNLOAD_URL"
unset DOWNLOAD_URL
python3 "$PROVENANCE" verify-oci-revision \
  --config "$OCI_CONFIG" \
  --expected-config-digest "$CONFIG_DIGEST" \
  --expected-commit "$COMMIT" \
  --expected-with-scrape-tools "$WITH_SCRAPE_TOOLS" \
  --expected-app-html-version-id "$APP_HTML_VERSION_ID" \
  --expected-app-html-sha256 "$APP_HTML_SHA256" \
  --contract "$RUNTIME_CONTRACT" \
  --expected-runtime-contract-sha256 "$RUNTIME_CONTRACT_SHA256"

echo "Candidate verified (build only; no deployment performed):"
echo "  repository=$ECR_REPOSITORY"
echo "  quarantine_repository=$ECR_QUARANTINE_REPOSITORY"
echo "  tag=$IMAGE_TAG"
echo "  quarantine_tag_digest=$QUARANTINE_TAG_DIGEST"
echo "  verified_quarantine_digest=$VERIFIED_QUARANTINE_DIGEST"
echo "  release_digest=$RELEASE_DIGEST"
echo "  commit=$COMMIT"
echo "  branch=$BRANCH"
echo "  with_scrape_tools=$WITH_SCRAPE_TOOLS"
echo "  app_html_sha256=$APP_HTML_SHA256"
echo "  app_html_version_id=$APP_HTML_VERSION_ID"
