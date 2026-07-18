#!/usr/bin/env bash
# Independently authorized, contract-only control-plane Terraform stage.
set -euo pipefail
umask 077

REGION="ap-northeast-1"
ACCOUNT_ID="718959508629"
EXPECTED_BRANCH="dev"
EXPECTED_ORIGIN="git@github.com:noirelumiere00/TeamAgent.git"
EXPECTED_CALLER_ARN="arn:aws:iam::718959508629:user/teamagent-release-control-update-caller"
UPDATER_ROLE_ARN="arn:aws:iam::718959508629:role/teamagent-dev-release-control-updater"
SESSION_NAME="teamagent-contract-control-update"
EXPECTED_SESSION_ARN="arn:aws:sts::718959508629:assumed-role/teamagent-dev-release-control-updater/$SESSION_NAME"

die() {
  echo "FATAL: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
usage: update_image_release_controls.sh plan|apply SAVED_PLAN

Creates or applies a saved Terraform plan that may only update the embedded
release-contract buildspec/hash fields of the five allowlisted CodeBuild
projects. The companion SAVED_PLAN.control-update.json binds the exact plan,
control commit, contract hashes, consumers, and in-place update addresses.
This independently authorized stage has no build-start, ECR, ECS, EventBridge,
Scheduler, task-definition, service, or production deployment authority.
EOF
}

reject_terraform_environment() {
  local variable
  while IFS= read -r variable; do
    [ -z "$variable" ] || die "pre-existing Terraform environment is forbidden: $variable"
  done < <(compgen -A variable TF_)
  unset variable
}

case "${1:-}" in -h|--help) usage; exit 0 ;; esac
case "$#" in 2) ;; *) usage >&2; exit 2 ;; esac
case "$1" in plan|apply) action="$1" ;; *) usage >&2; exit 2 ;; esac
reject_terraform_environment
for tool in aws git python3 terraform; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done

case "$2" in
  */*) plan_parent="${2%/*}"; plan_name="${2##*/}" ;;
  *) plan_parent="."; plan_name="$2" ;;
esac
[ -n "$plan_name" ] && [ "$plan_name" != "." ] && [ "$plan_name" != ".." ] \
  || die "saved plan path is invalid"
plan_parent="$(cd -- "$plan_parent" && pwd -P)" \
  || die "saved plan parent directory does not exist"
plan_path="$plan_parent/$plan_name"
authorization_path="$plan_path.control-update.json"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
control_root="$(git -C "$script_dir" rev-parse --show-toplevel 2>/dev/null)" \
  || die "control updater is not inside the TeamAgent worktree"
case "$plan_path" in "$control_root"/*) die "saved plans must be outside the worktree" ;; esac
[ -z "$(git -C "$control_root" status --porcelain=v1 --untracked-files=all --ignore-submodules=none)" ] \
  || die "TeamAgent control worktree is dirty"
[ "$(git -C "$control_root" symbolic-ref --quiet --short HEAD)" = "$EXPECTED_BRANCH" ] \
  || die "release controls must be updated from local dev"
[ "$(git -C "$control_root" config --get remote.origin.url)" = "$EXPECTED_ORIGIN" ] \
  || die "TeamAgent origin is not allowlisted"
git -C "$control_root" fetch --quiet --no-tags origin \
  "refs/heads/dev:refs/remotes/origin/dev" \
  || die "could not refresh origin/dev"
control_commit="$(git -C "$control_root" rev-parse HEAD)"
[ "$control_commit" = "$(git -C "$control_root" rev-parse refs/remotes/origin/dev)" ] \
  || die "local dev HEAD must exactly equal origin/dev"

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
[ -z "${extra:-}" ] && [ "$initial_account" = "$ACCOUNT_ID" ] \
  && [ "$initial_arn" = "$EXPECTED_CALLER_ARN" ] \
  || die "control update must start as the exact independent caller"
unset initial initial_account initial_arn extra
session="$(
  AWS_PAGER="" aws sts assume-role \
    --region "$REGION" \
    --role-arn "$UPDATER_ROLE_ARN" \
    --role-session-name "$SESSION_NAME" \
    --duration-seconds 10800 \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
    --output text
)" || die "could not assume the release-control updater role"
IFS=$'\t' read -r AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN extra \
  <<<"$session"
[ -z "${extra:-}" ] || die "malformed control-update credentials"
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
export AWS_DEFAULT_REGION="$REGION" AWS_REGION="$REGION"
export AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null
unset session extra
pinned="$(identity)"
IFS=$'\t' read -r pinned_account pinned_arn extra <<<"$pinned"
[ -z "${extra:-}" ] && [ "$pinned_account" = "$ACCOUNT_ID" ] \
  && [ "$pinned_arn" = "$EXPECTED_SESSION_ARN" ] \
  || die "unexpected release-control updater session"
unset pinned pinned_account pinned_arn extra
export TF_IN_AUTOMATION=1

validator="$script_dir/image_release_control_update.py"
[ -f "$validator" ] || die "release-control plan validator is missing"
cd "$script_dir"
if [ "$action" = "plan" ]; then
  [ ! -e "$plan_path" ] && [ ! -e "$authorization_path" ] \
    || die "saved plan or control authorization already exists"
  terraform plan \
    -input=false \
    -lock=true \
    -lock-timeout=5m \
    -out="$plan_path" \
    -target=aws_codebuild_project.image \
    -target=aws_codebuild_project.tiktok_image \
    -target=aws_codebuild_project.mcp_source_publisher \
    -target=aws_codebuild_project.image_attestor \
    -target=aws_codebuild_project.openclaw_provenance
  python3 "$validator" authorize \
    --terraform-dir "$script_dir" \
    --repo-root "$control_root" \
    --plan "$plan_path" \
    --control-commit "$control_commit" \
    --authorization "$authorization_path"
  echo "Saved independently authorized release-control plan:"
  echo "  plan=$plan_path"
  echo "  authorization=$authorization_path"
  echo "Review both files, then run this script with: apply $plan_path"
else
  [ -f "$plan_path" ] && [ -f "$authorization_path" ] \
    || die "saved plan and control authorization are both required"
  python3 "$validator" verify \
    --terraform-dir "$script_dir" \
    --repo-root "$control_root" \
    --plan "$plan_path" \
    --control-commit "$control_commit" \
    --authorization "$authorization_path"
  terraform apply \
    -input=false \
    -lock=true \
    -lock-timeout=5m \
    "$plan_path"
  echo "Embedded release contracts installed; no runtime deployment was authorized."
fi
