#!/usr/bin/env bash
# Run one existing provenance launcher through its exact short-lived launcher
# role. Root calls STS only and is never accepted by the launcher itself.
set -euo pipefail
umask 077

REGION="ap-northeast-1"
ACCOUNT_ID="718959508629"
ROOT_ARN="arn:aws:iam::718959508629:root"

die() {
  echo "FATAL: $*" >&2
  exit 2
}

usage() {
  cat <<'EOF'
usage:
  bootstrap_provenance_session.sh teamagent [build options]
  bootstrap_provenance_session.sh openclaw [build options]
  bootstrap_provenance_session.sh tiktok [build options]
  bootstrap_provenance_session.sh release [authorize_image_release options]

The selected release contract is validated locally before the first AWS call.
The current root credentials must already be an MFA-authenticated temporary
session; role trust enforces MFA, the exact role-session name, and source
identity. The child launcher accepts only its former dedicated IAM caller or
the exact pre-assumed launcher session, never root.
EOF
}

[ "$#" -ge 1 ] || {
  usage >&2
  exit 2
}
MODE="$1"
shift

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)" \
  || die "session wrapper repository root cannot be resolved"

PIPELINE=""
CONTRACT_EXPECTED_COMMIT=""
RELEASE_RECEIPT_KEY=""
case "$MODE" in
  teamagent)
    CONTRACT="$REPO_ROOT/infra/codebuild/teamagent_core_media_release_contract.json"
    CONTRACT_HELPER="$REPO_ROOT/infra/codebuild/teamagent_bundle_provenance.py"
    ROLE_ARN="arn:aws:iam::718959508629:role/teamagent-dev-codebuild-launcher"
    SESSION_NAME="teamagent-build-launcher"
    SOURCE_IDENTITY="teamagent-production-build"
    EXPECTED_SESSION_ARN="arn:aws:sts::718959508629:assumed-role/teamagent-dev-codebuild-launcher/teamagent-build-launcher"
    LAUNCHER="$SCRIPT_DIR/build_teamagent_image.sh"
    ;;
  openclaw)
    CONTRACT="$REPO_ROOT/infra/codebuild/openclaw_bundle_contract.json"
    CONTRACT_HELPER="$REPO_ROOT/infra/codebuild/openclaw_provenance.py"
    ROLE_ARN="arn:aws:iam::718959508629:role/teamagent-dev-openclaw-build-publisher"
    SESSION_NAME="openclaw-build-publisher"
    SOURCE_IDENTITY="teamagent-production-openclaw-build"
    EXPECTED_SESSION_ARN="arn:aws:sts::718959508629:assumed-role/teamagent-dev-openclaw-build-publisher/openclaw-build-publisher"
    LAUNCHER="$SCRIPT_DIR/build_openclaw_image.sh"
    ;;
  tiktok)
    CONTRACT="$REPO_ROOT/infra/codebuild/tiktok_release_contract.json"
    CONTRACT_HELPER=""
    ROLE_ARN="arn:aws:iam::718959508629:role/teamagent-dev-tiktok-build-launcher"
    SESSION_NAME="teamagent-tiktok-build"
    SOURCE_IDENTITY="teamagent-production-tiktok-build"
    EXPECTED_SESSION_ARN="arn:aws:sts::718959508629:assumed-role/teamagent-dev-tiktok-build-launcher/teamagent-tiktok-build"
    LAUNCHER="$SCRIPT_DIR/build_tiktok_image.sh"
    ;;
  release)
    pipeline_count=0
    receipt_key_count=0
    index=1
    while [ "$index" -le "$#" ]; do
      argument="${!index}"
      if [ "$argument" = "--pipeline" ]; then
        value_index=$((index + 1))
        [ "$value_index" -le "$#" ] || die "--pipeline requires a value"
        PIPELINE="${!value_index}"
        pipeline_count=$((pipeline_count + 1))
        index=$((index + 2))
        continue
      fi
      if [ "$argument" = "--receipt-key" ]; then
        value_index=$((index + 1))
        [ "$value_index" -le "$#" ] || die "--receipt-key requires a value"
        RELEASE_RECEIPT_KEY="${!value_index}"
        receipt_key_count=$((receipt_key_count + 1))
        index=$((index + 2))
        continue
      fi
      index=$((index + 1))
    done
    [ "$pipeline_count" -eq 1 ] ||
      die "release requires exactly one --pipeline"
    [ "$receipt_key_count" -eq 1 ] ||
      die "release requires exactly one --receipt-key"
    unset pipeline_count receipt_key_count index argument value_index
    case "$PIPELINE" in
      mcp)
        CONTRACT="$REPO_ROOT/infra/codebuild/teamagent_core_media_release_contract.json"
        CONTRACT_HELPER="$REPO_ROOT/infra/codebuild/teamagent_bundle_provenance.py"
        [[ "$RELEASE_RECEIPT_KEY" =~ ^release-receipts/mcp/([0-9a-f]{40})/[0-9a-f]{64}\.json$ ]] \
          || die "MCP receipt key does not bind one full source commit"
        CONTRACT_EXPECTED_COMMIT="${BASH_REMATCH[1]}"
        ;;
      openclaw)
        CONTRACT="$REPO_ROOT/infra/codebuild/openclaw_bundle_contract.json"
        CONTRACT_HELPER="$REPO_ROOT/infra/codebuild/openclaw_provenance.py"
        ;;
      tiktok)
        CONTRACT="$REPO_ROOT/infra/codebuild/tiktok_release_contract.json"
        CONTRACT_HELPER=""
        ;;
      *)
        die "release requires --pipeline mcp|openclaw|tiktok"
        ;;
    esac
    ROLE_ARN="arn:aws:iam::718959508629:role/teamagent-dev-release-launcher"
    SESSION_NAME="teamagent-release-authorization"
    SOURCE_IDENTITY="teamagent-production-release"
    EXPECTED_SESSION_ARN="arn:aws:sts::718959508629:assumed-role/teamagent-dev-release-launcher/teamagent-release-authorization"
    LAUNCHER="$SCRIPT_DIR/authorize_image_release.sh"
    ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    die "unknown provenance mode: $MODE"
    ;;
esac

for tool in git python3; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done
[ -f "$CONTRACT" ] && [ -f "$LAUNCHER" ] || die "trusted launcher controls are missing"

CONTRACT_RELATIVE="${CONTRACT#"$REPO_ROOT"/}"
LAUNCHER_RELATIVE="${LAUNCHER#"$REPO_ROOT"/}"
if [ -n "$CONTRACT_HELPER" ]; then
  CONTRACT_HELPER_RELATIVE="${CONTRACT_HELPER#"$REPO_ROOT"/}"
else
  CONTRACT_HELPER_RELATIVE=""
fi
[ "$CONTRACT_RELATIVE" != "$CONTRACT" ] && [ "$LAUNCHER_RELATIVE" != "$LAUNCHER" ] \
  || die "launcher controls escape the repository"

# Keep root credentials only in non-exported shell variables while Git,
# transitive child hashes, the protected remote, and release readiness are
# verified.
SAVED_AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID-}"
SAVED_AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY-}"
SAVED_AWS_SESSION_TOKEN="${AWS_SESSION_TOKEN-}"
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN
unset AWS_PROFILE AWS_DEFAULT_PROFILE AWS_CONFIG_FILE AWS_SHARED_CREDENTIALS_FILE
unset CURL_CA_BUNDLE GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_ASKPASS
unset GIT_CEILING_DIRECTORIES GIT_COMMON_DIR GIT_CONFIG GIT_CONFIG_COUNT
unset GIT_CONFIG_PARAMETERS GIT_DIR GIT_DISCOVERY_ACROSS_FILESYSTEM
unset GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_PROXY_COMMAND GIT_REPLACE_REF_BASE
unset GIT_SSH GIT_SSH_COMMAND GIT_SSL_CAINFO GIT_SSL_CAPATH GIT_SSL_NO_VERIFY
unset GIT_WORK_TREE SSH_ASKPASS SSH_AUTH_SOCK SSL_CERT_DIR SSL_CERT_FILE
unset BASH_ENV ENV CDPATH
while IFS= read -r git_variable; do
  unset "$git_variable"
done < <(compgen -A variable GIT_CONFIG_KEY_)
while IFS= read -r git_variable; do
  unset "$git_variable"
done < <(compgen -A variable GIT_CONFIG_VALUE_)
unset git_variable
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1
export GIT_NO_REPLACE_OBJECTS=1
export GIT_TERMINAL_PROMPT=0
unset -f aws git python3 terraform 2>/dev/null || true

PROVENANCE_HELPER="$REPO_ROOT/infra/bootstrap/wrapper_provenance.py"
[ -f "$PROVENANCE_HELPER" ] || die "wrapper provenance helper is missing"
HEAD_COMMIT="$(git -C "$REPO_ROOT" rev-parse --verify HEAD^{commit})" \
  || die "wrapper HEAD cannot be resolved"
if [ "$MODE" = "teamagent" ]; then
  CONTRACT_EXPECTED_COMMIT="$HEAD_COMMIT"
fi
EXPECTED_HELPER_BLOB="$(
  git -C "$REPO_ROOT" rev-parse "$HEAD_COMMIT:infra/bootstrap/wrapper_provenance.py"
)" || die "wrapper provenance helper is not tracked"
ACTUAL_HELPER_BLOB="$(
  git -C "$REPO_ROOT" hash-object --no-filters -- "$PROVENANCE_HELPER"
)" || die "wrapper provenance helper cannot be hashed"
[ "$EXPECTED_HELPER_BLOB" = "$ACTUAL_HELPER_BLOB" ] \
  || die "wrapper provenance helper differs from detached HEAD"
unset EXPECTED_HELPER_BLOB ACTUAL_HELPER_BLOB

REVIEW_TMP="$(mktemp -d "${TMPDIR:-/tmp}/teamagent-provenance-review.XXXXXXXX")"
chmod 700 "$REVIEW_TMP"
cleanup() {
  chmod -R u+w "$REVIEW_TMP" 2>/dev/null || true
  rm -rf -- "$REVIEW_TMP"
}
trap cleanup EXIT
REVIEW_ROOT="$REVIEW_TMP/checkout"
python3 -I "$PROVENANCE_HELPER" \
  --repo-root "$REPO_ROOT" \
  --checkout-dir "$REVIEW_ROOT" \
  --receipt "$REVIEW_TMP/provenance.json" \
  --profile provenance-session \
  || die "provenance session wrapper validation failed"
CONTRACT="$REVIEW_ROOT/$CONTRACT_RELATIVE"
LAUNCHER="$REVIEW_ROOT/$LAUNCHER_RELATIVE"
if [ -n "$CONTRACT_HELPER_RELATIVE" ]; then
  CONTRACT_HELPER="$REVIEW_ROOT/$CONTRACT_HELPER_RELATIVE"
fi
[ -f "$CONTRACT" ] && [ -f "$LAUNCHER" ] || die "reviewed launcher controls are missing"

# This check deliberately precedes command discovery for aws and every AWS
# invocation. A blocked contract cannot even mint a launcher session.
if [ -n "$CONTRACT_HELPER" ]; then
  if [ -n "$CONTRACT_EXPECTED_COMMIT" ]; then
    python3 -I "$CONTRACT_HELPER" assert-release-ready \
      --contract "$CONTRACT" \
      --expected-commit "$CONTRACT_EXPECTED_COMMIT" ||
      die "selected release contract is not ready"
  else
    python3 -I "$CONTRACT_HELPER" assert-release-ready --contract "$CONTRACT" ||
      die "selected release contract is not ready"
  fi
else
  python3 -I - "$CONTRACT" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    contract = json.load(handle)
if contract.get("release") != {"ready": True, "blocked_reason": ""}:
    raise SystemExit("FATAL: release.ready is false")
PY
fi

for tool in aws; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done
[ -n "$SAVED_AWS_ACCESS_KEY_ID" ] \
  && [ -n "$SAVED_AWS_SECRET_ACCESS_KEY" ] \
  && [ -n "$SAVED_AWS_SESSION_TOKEN" ] \
  || die "root must be supplied as an explicit temporary STS credential set"
export AWS_ACCESS_KEY_ID="$SAVED_AWS_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$SAVED_AWS_SECRET_ACCESS_KEY"
export AWS_SESSION_TOKEN="$SAVED_AWS_SESSION_TOKEN"
unset SAVED_AWS_ACCESS_KEY_ID SAVED_AWS_SECRET_ACCESS_KEY SAVED_AWS_SESSION_TOKEN
export AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true
export AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"
unset AWS_PROFILE AWS_DEFAULT_PROFILE AWS_CA_BUNDLE
unset CURL_CA_BUNDLE REQUESTS_CA_BUNDLE SSL_CERT_DIR SSL_CERT_FILE
while IFS= read -r endpoint_variable; do
  unset "$endpoint_variable"
done < <(compgen -A variable AWS_ENDPOINT_URL)
unset endpoint_variable

identity() {
  AWS_PAGER="" aws sts get-caller-identity --query '[Account,Arn]' --output text
}

initial="$(identity)"
[[ "$initial" != *$'\n'* && "$initial" != *$'\r'* ]] ||
  die "malformed initial AWS identity"
IFS=$'\t' read -r initial_account initial_arn extra <<<"$initial"
[ -z "${extra:-}" ] \
  && [ "$initial_account" = "$ACCOUNT_ID" ] \
  && [ "$initial_arn" = "$ROOT_ARN" ] \
  || die "provenance session wrapper must start as the exact account root"
unset initial initial_account initial_arn extra

session="$(
  AWS_PAGER="" aws sts assume-role \
    --region "$REGION" \
    --role-arn "$ROLE_ARN" \
    --role-session-name "$SESSION_NAME" \
    --source-identity "$SOURCE_IDENTITY" \
    --duration-seconds 10800 \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken,Expiration]' \
    --output text
)" || die "could not assume the selected provenance launcher role"
[[ "$session" != *$'\n'* && "$session" != *$'\r'* ]] ||
  die "malformed provenance launcher credentials"
IFS=$'\t' read -r \
  AWS_ACCESS_KEY_ID \
  AWS_SECRET_ACCESS_KEY \
  AWS_SESSION_TOKEN \
  expiration \
  extra <<<"$session"
[ -z "${extra:-}" ] || die "malformed provenance launcher credentials"
for credential in \
  "$AWS_ACCESS_KEY_ID" \
  "$AWS_SECRET_ACCESS_KEY" \
  "$AWS_SESSION_TOKEN" \
  "$expiration"; do
  [ -n "$credential" ] && [ "$credential" != "None" ] ||
    die "incomplete provenance launcher credentials"
done
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
unset session expiration extra credential credential_name

pinned="$(identity)"
IFS=$'\t' read -r pinned_account pinned_arn extra <<<"$pinned"
[ -z "${extra:-}" ] \
  && [ "$pinned_account" = "$ACCOUNT_ID" ] \
  && [ "$pinned_arn" = "$EXPECTED_SESSION_ARN" ] \
  || die "unexpected provenance launcher session"
unset pinned pinned_account pinned_arn extra

bash "$LAUNCHER" "$@"
