#!/usr/bin/env bash
# Assume the read/verify/intent-only role, then run one allowlisted gate command.
set -euo pipefail
umask 077

REGION="ap-northeast-1"
ACCOUNT_ID="718959508629"
EXPECTED_CALLER_ARN="arn:aws:sts::718959508629:assumed-role/teamagent-dev-terraform-runtime-automation/teamagent-terraform-worker"
GATE_ROLE_ARN="arn:aws:iam::718959508629:role/teamagent-dev-image-deployment-gate"
SESSION_NAME="teamagent-image-deployment-gate"
EXPECTED_SESSION_ARN="arn:aws:sts::718959508629:assumed-role/teamagent-dev-image-deployment-gate/$SESSION_NAME"

die() {
  echo "FATAL: $*" >&2
  exit 1
}

[ "$#" -ge 1 ] || die "a deployment gate command is required"
case "$1" in
  terraform-gate|prepare-deployment-intent|acquire-deployment-lock|validate-deployment-preflight|heartbeat-deployment-lock|release-deployment-lock|consume-deployment-intent|mark-deployment-intent-outcome) ;;
  *) die "deployment gate command is not allowlisted" ;;
esac
for tool in aws python3; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done

export AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true
while IFS= read -r endpoint_variable; do
  unset "$endpoint_variable"
done < <(compgen -A variable AWS_ENDPOINT_URL)
unset endpoint_variable AWS_PROFILE AWS_DEFAULT_PROFILE

identity() {
  AWS_PAGER="" aws sts get-caller-identity --query '[Account,Arn]' --output text
}

initial="$(identity)"
IFS=$'\t' read -r initial_account initial_arn extra <<<"$initial"
[ -z "${extra:-}" ] \
  && [ "$initial_account" = "$ACCOUNT_ID" ] \
  && [ "$initial_arn" = "$EXPECTED_CALLER_ARN" ] \
  || die "deployment gate must start as the exact trusted Terraform automation session"
unset initial initial_account initial_arn extra

session="$(
  AWS_PAGER="" aws sts assume-role \
    --region "$REGION" \
    --role-arn "$GATE_ROLE_ARN" \
    --role-session-name "$SESSION_NAME" \
    --duration-seconds 3600 \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
    --output text
)" || die "could not assume the image deployment gate role"
IFS=$'\t' read -r AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN extra \
  <<<"$session"
[ -z "${extra:-}" ] || die "malformed deployment gate credentials"
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
export AWS_DEFAULT_REGION="$REGION" AWS_REGION="$REGION"
export AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null
unset session extra

pinned="$(identity)"
IFS=$'\t' read -r pinned_account pinned_arn extra <<<"$pinned"
[ -z "${extra:-}" ] \
  && [ "$pinned_account" = "$ACCOUNT_ID" ] \
  && [ "$pinned_arn" = "$EXPECTED_SESSION_ARN" ] \
  || die "unexpected image deployment gate session"
unset pinned pinned_account pinned_arn extra

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
helper="$script_dir/../codebuild/release_evidence.py"
[ -f "$helper" ] || die "release evidence helper is missing"
exec python3 "$helper" "$@"
