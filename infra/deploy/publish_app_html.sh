#!/usr/bin/env bash
# Stage or verify one immutable connect-web /app object version.
#
# This script deliberately never changes ECS, task definitions, Terraform state,
# or the mutable S3 "latest" pointer during rollback. A staged VersionId becomes
# serving state only after its exact four anchors are bound into freshly signed
# core+media subjects and a one-use full saved Terraform plan is applied.
set -euo pipefail
umask 077

REGION="ap-northeast-1"
ACCOUNT_ID="718959508629"
BUCKET="teamagent-dev-raw-files"
KEY="codebuild/connect-web-app.html"

die() {
  echo "FATAL: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
usage:
  publish_app_html.sh stage \
    --src HTML \
    --manifest-sha256 SHA256 \
    --build-inputs-sha256 SHA256

  publish_app_html.sh rollback \
    --version-id VERSION_ID \
    --sha256 SHA256 \
    --manifest-sha256 SHA256 \
    --build-inputs-sha256 SHA256

stage uploads one candidate object, captures its immutable S3 VersionId, and
downloads that exact version again to verify its full SHA-256. rollback only
verifies an existing exact VersionId. Neither mode updates ECS or applies
Terraform. Use the emitted anchors to build and sign both final core+media
images, issue a fresh active/rollback receipt, and apply one new full saved plan.
EOF
}

require_value() {
  [ "$#" -ge 2 ] && [ -n "${2-}" ] || die "$1 requires a value"
}

case "${1-}" in
  stage|rollback) MODE="$1"; shift ;;
  -h|--help) usage; exit 0 ;;
  "") usage >&2; exit 2 ;;
  *) usage >&2; die "unknown mode: $1" ;;
esac

SRC=""
VERSION_ID=""
EXPECTED_SHA256=""
MANIFEST_SHA256=""
BUILD_INPUTS_SHA256=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --src) require_value "$@"; SRC="$2"; shift 2 ;;
    --version-id) require_value "$@"; VERSION_ID="$2"; shift 2 ;;
    --sha256) require_value "$@"; EXPECTED_SHA256="$2"; shift 2 ;;
    --manifest-sha256) require_value "$@"; MANIFEST_SHA256="$2"; shift 2 ;;
    --build-inputs-sha256) require_value "$@"; BUILD_INPUTS_SHA256="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done

case "$MODE" in
  stage)
    [ -n "$SRC" ] || die "stage requires --src"
    [ -z "$VERSION_ID" ] || die "stage does not accept --version-id"
    [ -z "$EXPECTED_SHA256" ] || die "stage derives --sha256 from --src"
    [ -s "$SRC" ] || die "HTML source is missing or empty: $SRC"
    ;;
  rollback)
    [ -z "$SRC" ] || die "rollback does not accept --src"
    [ -n "$VERSION_ID" ] || die "rollback requires --version-id"
    [ -n "$EXPECTED_SHA256" ] || die "rollback requires --sha256"
    ;;
esac
[ -n "$MANIFEST_SHA256" ] || die "$MODE requires --manifest-sha256"
[ -n "$BUILD_INPUTS_SHA256" ] || die "$MODE requires --build-inputs-sha256"

SHA256_RE='^[0-9a-f]{64}$'
VERSION_ID_RE='^[-A-Za-z0-9._~+/=]+$'
[[ "$MANIFEST_SHA256" =~ $SHA256_RE ]] || die "manifest SHA-256 is invalid"
[[ "$BUILD_INPUTS_SHA256" =~ $SHA256_RE ]] || die "build-inputs SHA-256 is invalid"
if [ "$MODE" = "rollback" ]; then
  [ "${#VERSION_ID}" -le 1024 ] && [[ "$VERSION_ID" =~ $VERSION_ID_RE ]] \
    || die "rollback VersionId is invalid"
  [[ "$EXPECTED_SHA256" =~ $SHA256_RE ]] || die "rollback SHA-256 is invalid"
fi

for tool in aws git jq python3 sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done
export AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true
unset AWS_ENDPOINT_URL AWS_ENDPOINT_URL_STS AWS_ENDPOINT_URL_S3 AWS_ENDPOINT_URL_ECS
while IFS= read -r AWS_ENDPOINT_VARIABLE; do
  unset "$AWS_ENDPOINT_VARIABLE"
done < <(compgen -A variable AWS_ENDPOINT_URL)
unset AWS_ENDPOINT_VARIABLE

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)" \
  || die "script is not inside the TeamAgent worktree"
BUNDLE_PROVENANCE="$REPO_ROOT/infra/codebuild/teamagent_bundle_provenance.py"
RELEASE_CONTRACT="$REPO_ROOT/infra/codebuild/teamagent_core_media_release_contract.json"
DEPLOY_LOG="$REPO_ROOT/infra/deploy_log.md"
for trusted_file in "$BUNDLE_PROVENANCE" "$RELEASE_CONTRACT" "$DEPLOY_LOG"; do
  [ -f "$trusted_file" ] || die "trusted release input is missing: $trusted_file"
done

# The existing active record is the only rollback baseline emitted by stage.
# This also rejects a malformed newest production entry before any S3 write.
CURRENT_RECORD="$(
  python3 "$BUNDLE_PROVENANCE" production-record \
    --deploy-log "$DEPLOY_LOG" \
    --format json
)" || die "latest production application record is invalid"
python3 "$BUNDLE_PROVENANCE" app-provenance-sha256 \
  --contract "$RELEASE_CONTRACT" \
  --deploy-log "$DEPLOY_LOG" >/dev/null \
  || die "release contract does not bind the latest production application record"
CURRENT_VERSION_ID="$(jq -er '.app_html_s3_version_id' <<<"$CURRENT_RECORD")"
CURRENT_SHA256="$(jq -er '.app_html_sha256' <<<"$CURRENT_RECORD")"

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/teamagent-app-version.XXXXXXXX")"
cleanup() {
  rm -rf -- "$TMP_DIR"
}
trap cleanup EXIT

if [ "$MODE" = "stage" ]; then
  EXPECTED_SHA256="$(sha256sum "$SRC" | awk '{print $1}')"
  [[ "$EXPECTED_SHA256" =~ $SHA256_RE ]] || die "could not hash staged HTML"
  [ "$EXPECTED_SHA256" != "$CURRENT_SHA256" ] \
    || die "staged HTML is byte-identical to the active production VersionId"
  PUT_RESULT="$(
    AWS_PAGER="" aws s3api put-object \
      --region "$REGION" \
      --bucket "$BUCKET" \
      --key "$KEY" \
      --body "$SRC" \
      --content-type "text/html; charset=utf-8" \
      --expected-bucket-owner "$ACCOUNT_ID" \
      --output json
  )" || die "candidate HTML upload failed"
  VERSION_ID="$(jq -er '.VersionId | strings | select(. != "null" and length > 0)' \
    <<<"$PUT_RESULT")" || die "S3 did not return an immutable VersionId; bucket versioning is required"
  [ "${#VERSION_ID}" -le 1024 ] && [[ "$VERSION_ID" =~ $VERSION_ID_RE ]] \
    || die "S3 returned an invalid VersionId"
  [ "$VERSION_ID" != "$CURRENT_VERSION_ID" ] || die "S3 reused the active VersionId"
fi

DOWNLOADED_VERSION_ID="$(
  AWS_PAGER="" aws s3api get-object \
    --region "$REGION" \
    --bucket "$BUCKET" \
    --key "$KEY" \
    --version-id "$VERSION_ID" \
    --expected-bucket-owner "$ACCOUNT_ID" \
    --query VersionId \
    --output text \
    "$TMP_DIR/app.html"
)" || die "could not read the exact application VersionId"
[ "$DOWNLOADED_VERSION_ID" = "$VERSION_ID" ] || die "downloaded S3 VersionId mismatch"
DOWNLOADED_SHA256="$(sha256sum "$TMP_DIR/app.html" | awk '{print $1}')"
[ "$DOWNLOADED_SHA256" = "$EXPECTED_SHA256" ] \
  || die "exact S3 VersionId bytes do not match the expected SHA-256"

RECORD="$(
  jq -cn \
    --arg version_id "$VERSION_ID" \
    --arg sha256 "$EXPECTED_SHA256" \
    --arg manifest "$MANIFEST_SHA256" \
    --arg build_inputs "$BUILD_INPUTS_SHA256" \
    '{
      schema_version: 1,
      app_html_s3_version_id: $version_id,
      app_html_sha256: $sha256,
      vault_manifest_sha256: $manifest,
      build_inputs_sha256: $build_inputs
    }'
)"

cat <<EOF
Immutable /app ${MODE} input verified.
  bucket=$BUCKET
  key=$KEY
  version_id=$VERSION_ID
  sha256=$EXPECTED_SHA256
  vault_manifest_sha256=$MANIFEST_SHA256
  build_inputs_sha256=$BUILD_INPUTS_SHA256

Canonical application record:
<!-- PRODUCTION_APP_PROVENANCE=$RECORD -->
EOF

if [ "$MODE" = "stage" ]; then
  cat <<EOF

The upload is NOT deployed. Before it may serve:
  1. Review the candidate and record these exact anchors in the trusted release
     contract/deploy record without changing the baked fallback.
  2. Build and attest both final core+media images from the exact approved commit.
  3. Promote both subjects only after C0/H0 scans and signed SBOM/provenance/
     recursive referrer verification.
  4. Issue a fresh active receipt and create/apply one new full saved plan with:
       -var=connect_app_html_s3_version_id=$VERSION_ID
       -var=connect_app_html_sha256=$EXPECTED_SHA256
       -var=connect_app_html_manifest_sha256=$MANIFEST_SHA256
       -var=connect_app_html_build_inputs_sha256=$BUILD_INPUTS_SHA256

Exact rollback baseline (do not copy it to latest):
$CURRENT_RECORD
Use rollback mode with that record's exact four anchors, a fresh rollback
receipt for matching signed core+media subjects, and a new one-use full plan.
EOF
else
  cat <<EOF

The rollback object was NOT copied and ECS was NOT changed. Restore this exact
VersionId only through matching signed core+media subjects, a fresh rollback
receipt, and one new full saved plan with:
  -var=connect_app_html_s3_version_id=$VERSION_ID
  -var=connect_app_html_sha256=$EXPECTED_SHA256
  -var=connect_app_html_manifest_sha256=$MANIFEST_SHA256
  -var=connect_app_html_build_inputs_sha256=$BUILD_INPUTS_SHA256
EOF
fi
