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
      index=$((index + 1))
    done
    [ "$pipeline_count" -eq 1 ] ||
      die "release requires exactly one --pipeline"
    unset pipeline_count index argument value_index
    case "$PIPELINE" in
      mcp)
        CONTRACT="$REPO_ROOT/infra/codebuild/teamagent_core_media_release_contract.json"
        CONTRACT_HELPER="$REPO_ROOT/infra/codebuild/teamagent_bundle_provenance.py"
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

for tool in python3; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done
[ -f "$CONTRACT" ] && [ -f "$LAUNCHER" ] || die "trusted launcher controls are missing"

# This check deliberately precedes command discovery for aws and every AWS
# invocation. A blocked contract cannot even mint a launcher session.
if [ -n "$CONTRACT_HELPER" ]; then
  python3 -I "$CONTRACT_HELPER" assert-release-ready --contract "$CONTRACT" ||
    die "selected release contract is not ready"
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
for credential_name in \
  AWS_ACCESS_KEY_ID \
  AWS_SECRET_ACCESS_KEY \
  AWS_SESSION_TOKEN; do
  [ -n "${!credential_name:-}" ] ||
    die "root must be supplied as an explicit temporary STS credential set"
done
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

exec bash "$LAUNCHER" "$@"
