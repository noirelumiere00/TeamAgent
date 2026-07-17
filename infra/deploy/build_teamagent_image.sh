#!/usr/bin/env bash
# Build-only launcher for the TeamAgent MCP image.
#
# The script archives a clean Git commit, pins the exact S3 VersionId into
# CodeBuild, waits for the vulnerability-gated build, then verifies the remote
# OCI config labels.  It deliberately performs no ECS/Fargate deployment.
set -euo pipefail
umask 077

REGION="ap-northeast-1"
SOURCE_BUCKET="teamagent-dev-raw-files"
SOURCE_KEY="codebuild/source.zip"
APP_HTML_BUCKET="teamagent-dev-raw-files"
APP_HTML_KEY="codebuild/connect-web-app.html"
CODEBUILD_PROJECT="teamagent-dev-image-builder"
ECR_REPOSITORY="teamagent-mcp"
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

RUNTIME_VALUES="$(python3 "$PROVENANCE" runtime-values --contract "$RUNTIME_CONTRACT")" \
  || die "TeamAgent runtime contract is invalid"
[[ "$RUNTIME_VALUES" != *$'\n'* && "$RUNTIME_VALUES" != *$'\r'* ]] \
  || die "TeamAgent runtime contract returned multiple lines"
IFS=$'\t' read -r \
  E5_MODEL_REVISION \
  NODE_IMAGE_DIGEST \
  NODE_VERSION \
  NODE_BINARY_SHA256 \
  PLAYWRIGHT_VERSION \
  PLAYWRIGHT_CHROMIUM_REVISION \
  PLAYWRIGHT_CHROMIUM_VERSION \
  PLAYWRIGHT_CHROMIUM_ARCHIVE_SHA256 \
  PLAYWRIGHT_CHROMIUM_SHA256 \
  EXTRA_RUNTIME_VALUE <<<"$RUNTIME_VALUES"
[ -z "${EXTRA_RUNTIME_VALUE:-}" ] || die "TeamAgent runtime contract returned extra values"
for runtime_value in \
  "$E5_MODEL_REVISION" \
  "$NODE_IMAGE_DIGEST" \
  "$NODE_VERSION" \
  "$NODE_BINARY_SHA256" \
  "$PLAYWRIGHT_VERSION" \
  "$PLAYWRIGHT_CHROMIUM_REVISION" \
  "$PLAYWRIGHT_CHROMIUM_VERSION" \
  "$PLAYWRIGHT_CHROMIUM_ARCHIVE_SHA256" \
  "$PLAYWRIGHT_CHROMIUM_SHA256"; do
  [ -n "$runtime_value" ] || die "TeamAgent runtime contract returned an empty value"
done
unset RUNTIME_VALUES runtime_value

RUNTIME_EXPECTED_ARGS=(
  --expected-e5-model-revision "$E5_MODEL_REVISION"
  --expected-node-image-digest "$NODE_IMAGE_DIGEST"
  --expected-node-version "$NODE_VERSION"
  --expected-node-binary-sha256 "$NODE_BINARY_SHA256"
  --expected-playwright-version "$PLAYWRIGHT_VERSION"
  --expected-playwright-chromium-revision "$PLAYWRIGHT_CHROMIUM_REVISION"
  --expected-playwright-chromium-version "$PLAYWRIGHT_CHROMIUM_VERSION"
  --expected-playwright-chromium-archive-sha256 "$PLAYWRIGHT_CHROMIUM_ARCHIVE_SHA256"
  --expected-playwright-chromium-sha256 "$PLAYWRIGHT_CHROMIUM_SHA256"
)

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/teamagent-codebuild.XXXXXXXX")"
cleanup() {
  rm -rf -- "$TMP_DIR"
}
trap cleanup EXIT

MANIFEST="$TMP_DIR/.teamagent-source-manifest.json"
SOURCE_ZIP="$TMP_DIR/source.zip"
EXTRACTED="$TMP_DIR/extracted"
ENV_OVERRIDES="$TMP_DIR/codebuild-env.json"
PARENT_BATCH_RESPONSE="$TMP_DIR/ecr-parent-batch-get-image.json"
CHILD_BATCH_RESPONSE="$TMP_DIR/ecr-child-batch-get-image.json"
OCI_CONFIG="$TMP_DIR/oci-config.json"
APP_HTML_FILE="$TMP_DIR/connect-web-app.html"

[ "$SOURCE_BUCKET" = "$APP_HTML_BUCKET" ] \
  || die "source and app.html must use the same fixed versioned bucket"
BUCKET_VERSIONING="$(
  AWS_PAGER="" aws s3api get-bucket-versioning \
    --region "$REGION" \
    --bucket "$SOURCE_BUCKET" \
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
  "${RUNTIME_EXPECTED_ARGS[@]}"

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
E5_MODEL_REVISION="$E5_MODEL_REVISION" \
NODE_IMAGE_DIGEST="$NODE_IMAGE_DIGEST" \
NODE_VERSION="$NODE_VERSION" \
NODE_BINARY_SHA256="$NODE_BINARY_SHA256" \
PLAYWRIGHT_VERSION="$PLAYWRIGHT_VERSION" \
PLAYWRIGHT_CHROMIUM_REVISION="$PLAYWRIGHT_CHROMIUM_REVISION" \
PLAYWRIGHT_CHROMIUM_VERSION="$PLAYWRIGHT_CHROMIUM_VERSION" \
PLAYWRIGHT_CHROMIUM_ARCHIVE_SHA256="$PLAYWRIGHT_CHROMIUM_ARCHIVE_SHA256" \
PLAYWRIGHT_CHROMIUM_SHA256="$PLAYWRIGHT_CHROMIUM_SHA256" \
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
    "E5_MODEL_REVISION",
    "NODE_IMAGE_DIGEST",
    "NODE_VERSION",
    "NODE_BINARY_SHA256",
    "PLAYWRIGHT_VERSION",
    "PLAYWRIGHT_CHROMIUM_REVISION",
    "PLAYWRIGHT_CHROMIUM_VERSION",
    "PLAYWRIGHT_CHROMIUM_ARCHIVE_SHA256",
    "PLAYWRIGHT_CHROMIUM_SHA256",
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

TAG_DIGEST="$(
  AWS_PAGER="" aws ecr describe-images \
    --region "$REGION" \
    --repository-name "$ECR_REPOSITORY" \
    --image-ids "imageTag=$IMAGE_TAG" \
    --query 'imageDetails[0].imageDigest' \
    --output text
)"
[[ "$TAG_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || die "ECR returned an invalid tag digest"

AWS_PAGER="" aws ecr batch-get-image \
  --region "$REGION" \
  --repository-name "$ECR_REPOSITORY" \
  --image-ids "imageDigest=$TAG_DIGEST" \
  --accepted-media-types \
    application/vnd.docker.distribution.manifest.list.v2+json \
    application/vnd.oci.image.index.v1+json \
    application/vnd.docker.distribution.manifest.v2+json \
    application/vnd.oci.image.manifest.v1+json \
  --output json >"$PARENT_BATCH_RESPONSE"
ARM64_DIGEST="$(
  python3 "$IMAGE_RESOLVER" resolve-platform \
    --batch-response "$PARENT_BATCH_RESPONSE" \
    --expected-image-digest "$TAG_DIGEST" \
    --os linux \
    --architecture arm64
)"
[[ "$ARM64_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || die "ECR image resolver returned an invalid arm64 child digest"

AWS_PAGER="" aws ecr batch-get-image \
  --region "$REGION" \
  --repository-name "$ECR_REPOSITORY" \
  --image-ids "imageDigest=$ARM64_DIGEST" \
  --accepted-media-types \
    application/vnd.docker.distribution.manifest.v2+json \
    application/vnd.oci.image.manifest.v1+json \
  --output json >"$CHILD_BATCH_RESPONSE"
CONFIG_DIGEST="$(
  python3 "$PROVENANCE" ecr-config-digest \
    --batch-response "$CHILD_BATCH_RESPONSE" \
    --expected-image-digest "$ARM64_DIGEST"
)"
[[ "$CONFIG_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || die "ECR returned an invalid OCI config digest"

DOWNLOAD_URL="$(
  AWS_PAGER="" aws ecr get-download-url-for-layer \
    --region "$REGION" \
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
  "${RUNTIME_EXPECTED_ARGS[@]}"

echo "Candidate verified (build only; no deployment performed):"
echo "  repository=$ECR_REPOSITORY"
echo "  tag=$IMAGE_TAG"
echo "  tag_digest=$TAG_DIGEST"
echo "  arm64_digest=$ARM64_DIGEST"
echo "  commit=$COMMIT"
echo "  branch=$BRANCH"
echo "  with_scrape_tools=$WITH_SCRAPE_TOOLS"
echo "  app_html_sha256=$APP_HTML_SHA256"
echo "  app_html_version_id=$APP_HTML_VERSION_ID"
