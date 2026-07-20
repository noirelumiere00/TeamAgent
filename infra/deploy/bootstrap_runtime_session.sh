#!/usr/bin/env bash
# Enter the existing runtime guard through the exact short-lived automation
# session. Root is permitted to call STS only; the guard rejects root itself.
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
usage: bootstrap_runtime_session.sh GUARD_COMMAND [GUARD_ARGUMENTS...]

Runs infra/deploy/terraform_runtime_guard.sh under an exact main-owned STS
role. Runtime/plan commands use the three-hour
teamagent-dev-terraform-runtime-automation role. `sign-alarm-ack` alone uses
the one-hour, KMS-Sign-only teamagent-dev-alarm-recipient-ack-signer role.
The current root credentials must already be an MFA-authenticated temporary
session; both role trusts enforce MFA and fixed session/source identities.

This wrapper does not accept an arbitrary command and cannot invoke build or
release launchers.
EOF
}

[ "$#" -ge 1 ] || {
  usage >&2
  exit 2
}
case "$1" in
  sign-alarm-ack)
    ROLE_ARN="arn:aws:iam::718959508629:role/teamagent-dev-alarm-recipient-ack-signer"
    SESSION_NAME="teamagent-alarm-recipient-ack"
    SOURCE_IDENTITY="teamagent-production-alarm-recipient"
    EXPECTED_SESSION_ARN="arn:aws:sts::718959508629:assumed-role/teamagent-dev-alarm-recipient-ack-signer/teamagent-alarm-recipient-ack"
    SESSION_SECONDS=3600
    ;;
  snapshot|attest-log-versioning|issue-alarm-challenge|attest-alarm-delivery|advance-alarm-migration|attest-media-cutover|attest-log-readiness|preflight|plan|verify|apply)
    ROLE_ARN="arn:aws:iam::718959508629:role/teamagent-dev-terraform-runtime-automation"
    SESSION_NAME="teamagent-terraform-worker"
    SOURCE_IDENTITY="teamagent-production-terraform"
    EXPECTED_SESSION_ARN="arn:aws:sts::718959508629:assumed-role/teamagent-dev-terraform-runtime-automation/teamagent-terraform-worker"
    SESSION_SECONDS=10800
    ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    die "runtime guard command is not allowlisted: $1"
    ;;
esac

for tool in git python3; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)" \
  || die "runtime session repository root cannot be resolved"
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
EXPECTED_HELPER_BLOB="$(
  git -C "$REPO_ROOT" rev-parse "$HEAD_COMMIT:infra/bootstrap/wrapper_provenance.py"
)" || die "wrapper provenance helper is not tracked"
ACTUAL_HELPER_BLOB="$(
  git -C "$REPO_ROOT" hash-object --no-filters -- "$PROVENANCE_HELPER"
)" || die "wrapper provenance helper cannot be hashed"
[ "$EXPECTED_HELPER_BLOB" = "$ACTUAL_HELPER_BLOB" ] \
  || die "wrapper provenance helper differs from detached HEAD"
unset EXPECTED_HELPER_BLOB ACTUAL_HELPER_BLOB

REVIEW_TMP="$(mktemp -d "${TMPDIR:-/tmp}/teamagent-runtime-review.XXXXXXXX")"
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
  --profile runtime-session \
  || die "runtime session wrapper provenance validation failed"
GUARD="$REVIEW_ROOT/infra/deploy/terraform_runtime_guard.sh"
[ -f "$GUARD" ] || die "reviewed runtime guard is missing"

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
  || die "runtime session wrapper must start as the exact account root"
unset initial initial_account initial_arn extra

session="$(
  AWS_PAGER="" aws sts assume-role \
    --region "$REGION" \
    --role-arn "$ROLE_ARN" \
    --role-session-name "$SESSION_NAME" \
    --source-identity "$SOURCE_IDENTITY" \
    --duration-seconds "$SESSION_SECONDS" \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken,Expiration]' \
    --output text
)" || die "could not assume the trusted runtime automation role"
[[ "$session" != *$'\n'* && "$session" != *$'\r'* ]] ||
  die "malformed runtime automation credentials"
IFS=$'\t' read -r \
  AWS_ACCESS_KEY_ID \
  AWS_SECRET_ACCESS_KEY \
  AWS_SESSION_TOKEN \
  expiration \
  extra <<<"$session"
[ -z "${extra:-}" ] || die "malformed runtime automation credentials"
for credential in \
  "$AWS_ACCESS_KEY_ID" \
  "$AWS_SECRET_ACCESS_KEY" \
  "$AWS_SESSION_TOKEN" \
  "$expiration"; do
  [ -n "$credential" ] && [ "$credential" != "None" ] ||
    die "incomplete runtime automation credentials"
done
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
unset session expiration extra credential credential_name

pinned="$(identity)"
IFS=$'\t' read -r pinned_account pinned_arn extra <<<"$pinned"
[ -z "${extra:-}" ] \
  && [ "$pinned_account" = "$ACCOUNT_ID" ] \
  && [ "$pinned_arn" = "$EXPECTED_SESSION_ARN" ] \
  || die "unexpected runtime automation session"
unset pinned pinned_account pinned_arn extra

# The guard deliberately rejects inherited profile/config selectors before it
# pins AWS CLI v2 and installs its own /dev/null config paths. Keep only the
# explicit child STS credential set across exec.
unset AWS_CONFIG_FILE AWS_SHARED_CREDENTIALS_FILE

bash "$GUARD" "$@"
