#!/usr/bin/env bash
# Run one allowlisted gate command under the exact runtime automation session.
# The runtime role receives this helper's exact policy directly and never
# chains into another role.
set -euo pipefail
umask 077

REGION="ap-northeast-1"
ACCOUNT_ID="718959508629"
EXPECTED_CALLER_ARN="arn:aws:sts::718959508629:assumed-role/teamagent-dev-terraform-runtime-automation/teamagent-terraform-worker"

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
unset endpoint_variable AWS_PROFILE AWS_DEFAULT_PROFILE AWS_CA_BUNDLE
unset CURL_CA_BUNDLE REQUESTS_CA_BUNDLE SSL_CERT_DIR SSL_CERT_FILE
export AWS_DEFAULT_REGION="$REGION" AWS_REGION="$REGION"
export AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null

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

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
helper="$script_dir/../codebuild/release_evidence.py"
[ -f "$helper" ] || die "release evidence helper is missing"
exec python3 "$helper" "$@"
