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
CODEBUILD_PROJECT="teamagent-dev-image-builder"
ECR_REPOSITORY="teamagent-mcp"
IMAGE_TAG=""
WITH_SCRAPE_TOOLS=""
POLL_SECONDS=15
TIMEOUT_SECONDS=7200

usage() {
  cat <<'EOF'
usage: build_teamagent_image.sh --image-tag <tag> --with-scrape-tools true|false [options]

Required:
  --image-tag <tag>                 Immutable candidate tag (safe ECR characters only)
  --with-scrape-tools true|false    Explicit image profile; there is no implicit default

Options:
  --region <region>                 AWS region (default: ap-northeast-1)
  --source-bucket <bucket>          Versioned S3 source bucket
  --project-name <name>             CodeBuild project name
  --repository-name <name>          ECR repository name
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
    --region)
      require_value "$1" "${2-}"
      REGION="$2"
      shift 2
      ;;
    --source-bucket)
      require_value "$1" "${2-}"
      SOURCE_BUCKET="$2"
      shift 2
      ;;
    --project-name)
      require_value "$1" "${2-}"
      CODEBUILD_PROJECT="$2"
      shift 2
      ;;
    --repository-name)
      require_value "$1" "${2-}"
      ECR_REPOSITORY="$2"
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
[ -n "$WITH_SCRAPE_TOOLS" ] || die "--with-scrape-tools true|false is required"
[[ "$IMAGE_TAG" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] \
  || die "unsafe image tag; use 1-128 characters from [A-Za-z0-9._-] and start alphanumeric"
[[ "$WITH_SCRAPE_TOOLS" == "true" || "$WITH_SCRAPE_TOOLS" == "false" ]] \
  || die "--with-scrape-tools must be exactly true or false"
[[ "$REGION" =~ ^[a-z]{2}(-gov)?-[a-z]+-[0-9]+$ ]] || die "invalid AWS region"
[[ "$SOURCE_BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || die "invalid S3 bucket"
[[ "$CODEBUILD_PROJECT" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{1,254}$ ]] \
  || die "invalid CodeBuild project name"
[[ "$ECR_REPOSITORY" =~ ^[a-z0-9]+([._/-][a-z0-9]+)*$ ]] \
  || die "invalid ECR repository name"
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
[ -f "$PROVENANCE" ] || die "source provenance verifier is missing"

if [ -n "$(git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=all --ignore-submodules=none)" ]; then
  die "Git worktree is dirty (tracked or untracked changes); commit or remove them first"
fi
COMMIT="$(git -C "$REPO_ROOT" rev-parse --verify HEAD^{commit})"
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || die "HEAD is not a full SHA-1 commit"
BRANCH="$(git -C "$REPO_ROOT" symbolic-ref --quiet --short HEAD)" \
  || die "detached HEAD is not allowed; check out the branch being built"
[ -n "$BRANCH" ] || die "current Git branch is empty"

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/teamagent-codebuild.XXXXXXXX")"
cleanup() {
  rm -rf -- "$TMP_DIR"
}
trap cleanup EXIT

MANIFEST="$TMP_DIR/.teamagent-source-manifest.json"
SOURCE_ZIP="$TMP_DIR/source.zip"
EXTRACTED="$TMP_DIR/extracted"
ENV_OVERRIDES="$TMP_DIR/codebuild-env.json"
BATCH_RESPONSE="$TMP_DIR/ecr-batch-get-image.json"
OCI_CONFIG="$TMP_DIR/oci-config.json"

echo "Preparing Git archive for $COMMIT ($BRANCH), WITH_SCRAPE_TOOLS=$WITH_SCRAPE_TOOLS"
python3 "$PROVENANCE" create-manifest \
  --repo-root "$REPO_ROOT" \
  --commit "$COMMIT" \
  --branch "$BRANCH" \
  --with-scrape-tools "$WITH_SCRAPE_TOOLS" \
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
  --expected-with-scrape-tools "$WITH_SCRAPE_TOOLS"

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
    --query VersionId \
    --output text
)"
if [ -z "$VERSION_ID" ] || [ "$VERSION_ID" = "None" ] || [ "$VERSION_ID" = "null" ] \
  || [[ "$VERSION_ID" == *$'\n'* ]] || [[ "$VERSION_ID" == *$'\r'* ]]; then
  die "S3 did not return a usable VersionId; bucket versioning is required"
fi

python3 - "$ENV_OVERRIDES" "$COMMIT" "$BRANCH" "$IMAGE_TAG" "$WITH_SCRAPE_TOOLS" <<'PY'
import json
import sys

path, commit, branch, image_tag, with_scrape_tools = sys.argv[1:]
values = {
    "GIT_COMMIT": commit,
    "GIT_BRANCH": branch,
    "IMAGE_TAG": image_tag,
    "WITH_SCRAPE_TOOLS": with_scrape_tools,
}
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
    --buildspec-override infra/codebuild/buildspec.yml \
    --environment-variables-override "file://$ENV_OVERRIDES" \
    --query build.id \
    --output text
)"
[ -n "$BUILD_ID" ] && [ "$BUILD_ID" != "None" ] && [ "$BUILD_ID" != "null" ] \
  || die "CodeBuild did not return a build ID"
[[ "$BUILD_ID" != *$'\n'* && "$BUILD_ID" != *$'\r'* ]] || die "invalid CodeBuild build ID"

DEADLINE=$((SECONDS + TIMEOUT_SECONDS))
BUILD_STATUS=""
RESOLVED_SOURCE_VERSION=""
while :; do
  BUILD_STATE="$(
    AWS_PAGER="" aws codebuild batch-get-builds \
      --region "$REGION" \
      --ids "$BUILD_ID" \
      --query 'builds[0].[buildStatus,resolvedSourceVersion]' \
      --output text
  )"
  [[ "$BUILD_STATE" != *$'\n'* && "$BUILD_STATE" != *$'\r'* ]] \
    || die "CodeBuild returned a multi-line build state"
  IFS=$'\t' read -r BUILD_STATUS RESOLVED_SOURCE_VERSION EXTRA_STATE <<<"$BUILD_STATE"
  [ -z "${EXTRA_STATE:-}" ] || die "CodeBuild returned an unexpected build state"
  [ -n "$BUILD_STATUS" ] && [ "$BUILD_STATUS" != "None" ] \
    || die "CodeBuild build state is missing"
  if [ -n "${RESOLVED_SOURCE_VERSION:-}" ] && [ "$RESOLVED_SOURCE_VERSION" != "None" ] \
    && [ "$RESOLVED_SOURCE_VERSION" != "$VERSION_ID" ]; then
    die "CodeBuild resolvedSourceVersion does not match the uploaded S3 VersionId"
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

[ "$RESOLVED_SOURCE_VERSION" = "$VERSION_ID" ] \
  || die "CodeBuild did not resolve the exact uploaded S3 VersionId"
[ "$BUILD_STATUS" = "SUCCEEDED" ] \
  || die "CodeBuild candidate failed with status $BUILD_STATUS (build ID: $BUILD_ID)"

DIGEST="$(
  AWS_PAGER="" aws ecr describe-images \
    --region "$REGION" \
    --repository-name "$ECR_REPOSITORY" \
    --image-ids "imageTag=$IMAGE_TAG" \
    --query 'imageDetails[0].imageDigest' \
    --output text
)"
[[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || die "ECR returned an invalid image digest"

AWS_PAGER="" aws ecr batch-get-image \
  --region "$REGION" \
  --repository-name "$ECR_REPOSITORY" \
  --image-ids "imageDigest=$DIGEST" \
  --accepted-media-types \
    application/vnd.docker.distribution.manifest.v2+json \
    application/vnd.oci.image.manifest.v1+json \
  --output json >"$BATCH_RESPONSE"
CONFIG_DIGEST="$(
  python3 "$PROVENANCE" ecr-config-digest \
    --batch-response "$BATCH_RESPONSE" \
    --expected-image-digest "$DIGEST"
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
  --expected-with-scrape-tools "$WITH_SCRAPE_TOOLS"

echo "Candidate verified (build only; no deployment performed):"
echo "  repository=$ECR_REPOSITORY"
echo "  tag=$IMAGE_TAG"
echo "  digest=$DIGEST"
echo "  commit=$COMMIT"
echo "  branch=$BRANCH"
echo "  with_scrape_tools=$WITH_SCRAPE_TOOLS"
