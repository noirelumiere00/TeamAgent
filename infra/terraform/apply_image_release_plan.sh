#!/usr/bin/env bash
# Apply one prepared saved plan; Terraform consumes its intent/receipts first.
set -euo pipefail
umask 077

EXPECTED_BRANCH="dev"
EXPECTED_ORIGIN="git@github.com:noirelumiere00/TeamAgent.git"
EXPECTED_AUTOMATION_ARN="arn:aws:sts::718959508629:assumed-role/teamagent-dev-terraform-automation/teamagent-terraform-worker"
EXPECTED_REGION="ap-northeast-1"

die() {
  echo "FATAL: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
usage: apply_image_release_plan.sh SAVED_PLAN

The production release gate revalidates receipt expiry, signatures, promoted
subjects, and the complete referrer graph, then atomically consumes the unique
intent and every exact receipt before any image-bearing task definition.
The same plan, intent, or receipt cannot be applied twice.
EOF
}

reject_terraform_environment() {
  local variable
  while IFS= read -r variable; do
    [ -z "$variable" ] || die "pre-existing Terraform environment is forbidden: $variable"
  done < <(compgen -A variable TF_)
  unset variable
}

reject_terraform_environment
export TF_IN_AUTOMATION=1
export AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true
while IFS= read -r endpoint_variable; do
  unset "$endpoint_variable"
done < <(compgen -A variable AWS_ENDPOINT_URL)
unset endpoint_variable AWS_PROFILE AWS_DEFAULT_PROFILE
export AWS_DEFAULT_REGION="$EXPECTED_REGION" AWS_REGION="$EXPECTED_REGION"
export AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null

case "$#" in
  1) ;;
  *) usage >&2; exit 2 ;;
esac
case "$1" in -h|--help) usage; exit 0 ;; esac
for tool in aws git python3 terraform; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done
caller_arn="$(AWS_PAGER="" aws sts get-caller-identity --query Arn --output text)"
[ "$caller_arn" = "$EXPECTED_AUTOMATION_ARN" ] \
  || die "image release apply requires the exact trusted Terraform automation session"
unset caller_arn

case "$1" in
  */*) plan_parent="${1%/*}"; plan_name="${1##*/}" ;;
  *) plan_parent="."; plan_name="$1" ;;
esac
[ -n "$plan_name" ] && [ "$plan_name" != "." ] && [ "$plan_name" != ".." ] \
  || die "saved plan path is invalid"
plan_parent="$(cd -- "$plan_parent" && pwd -P)" \
  || die "saved plan parent directory does not exist"
plan_path="$plan_parent/$plan_name"
[ -f "$plan_path" ] || die "saved plan does not exist"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
control_root="$(git -C "$script_dir" rev-parse --show-toplevel 2>/dev/null)" \
  || die "apply launcher is not inside the TeamAgent worktree"
case "$plan_path" in
  "$control_root"/*)
    die "saved plans must be stored outside the TeamAgent worktree"
    ;;
esac
[ -z "$(git -C "$control_root" status --porcelain=v1 --untracked-files=all --ignore-submodules=none)" ] \
  || die "TeamAgent control worktree is dirty"
[ "$(git -C "$control_root" symbolic-ref --quiet --short HEAD)" = "$EXPECTED_BRANCH" ] \
  || die "image deployment apply must run from local dev"
[ "$(git -C "$control_root" config --get remote.origin.url)" = "$EXPECTED_ORIGIN" ] \
  || die "TeamAgent origin is not allowlisted"
git -C "$control_root" fetch --quiet --no-tags origin \
  "refs/heads/dev:refs/remotes/origin/dev" \
  || die "could not refresh origin/dev"
[ "$(git -C "$control_root" rev-parse HEAD)" = "$(git -C "$control_root" rev-parse refs/remotes/origin/dev)" ] \
  || die "local dev HEAD must exactly equal origin/dev"

gate_runner="$control_root/infra/deploy/run_image_deployment_gate.sh"
context_helper="$control_root/infra/terraform/image_release_context.py"
[ -f "$context_helper" ] || die "Terraform release context helper is missing"
export TEAMAGENT_SAVED_PLAN_PATH="$plan_path"
export TEAMAGENT_APPLY_ATTEMPT_ID
TEAMAGENT_APPLY_ATTEMPT_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"

cd "$script_dir"
temporary="$(mktemp -d "${TMPDIR:-/tmp}/teamagent-image-apply.XXXXXXXX")"
lock_acquired=false
attempt_started=false
outcome_recorded=false
heartbeat_pid=""
cleanup() {
  if [ -n "$heartbeat_pid" ]; then
    kill "$heartbeat_pid" >/dev/null 2>&1 || true
    wait "$heartbeat_pid" >/dev/null 2>&1 || true
  fi
  if [ "$attempt_started" = "true" ] && [ "$outcome_recorded" != "true" ]; then
    bash "$gate_runner" mark-deployment-intent-outcome \
      --plan "$plan_path" \
      --apply-attempt-id "$TEAMAGENT_APPLY_ATTEMPT_ID" \
      --outcome reconcile-required >/dev/null 2>&1 || true
  fi
  if [ "$lock_acquired" = "true" ]; then
    bash "$gate_runner" release-deployment-lock \
      --plan "$plan_path" \
      --apply-attempt-id "$TEAMAGENT_APPLY_ATTEMPT_ID" >/dev/null 2>&1 || true
  fi
  rm -rf -- "$temporary"
}
trap cleanup EXIT

attempt_started=true
bash "$gate_runner" acquire-deployment-lock \
  --plan "$plan_path" \
  --apply-attempt-id "$TEAMAGENT_APPLY_ATTEMPT_ID" >/dev/null
lock_acquired=true
python3 "$context_helper" capture \
  --terraform-dir "$script_dir" \
  --plan "$plan_path" \
  --output "$temporary/terraform-context.json"
bash "$gate_runner" validate-deployment-preflight \
  --plan "$plan_path" \
  --terraform-context "$temporary/terraform-context.json" \
  --apply-attempt-id "$TEAMAGENT_APPLY_ATTEMPT_ID" >/dev/null

parent_pid="$$"
(
  while :; do
    sleep 30
    if ! bash "$gate_runner" heartbeat-deployment-lock \
      --plan "$plan_path" \
      --apply-attempt-id "$TEAMAGENT_APPLY_ATTEMPT_ID" >/dev/null; then
      echo "FATAL: shared image/Terraform automation lock heartbeat failed" >&2
      kill -TERM "$parent_pid" >/dev/null 2>&1 || true
      exit 1
    fi
  done
) &
heartbeat_pid="$!"
unset parent_pid

set +e
terraform apply \
  -input=false \
  -lock=true \
  -lock-timeout=5m \
  "$plan_path"
apply_status=$?
set -e
if [ "$apply_status" -eq 0 ]; then
  if ! bash "$gate_runner" mark-deployment-intent-outcome \
    --plan "$plan_path" \
    --apply-attempt-id "$TEAMAGENT_APPLY_ATTEMPT_ID" \
    --outcome applied; then
    die "apply completed but outcome recording failed; reconcile state and do not reapply"
  fi
  outcome_recorded=true
  bash "$gate_runner" release-deployment-lock \
    --plan "$plan_path" \
    --apply-attempt-id "$TEAMAGENT_APPLY_ATTEMPT_ID" >/dev/null
  lock_acquired=false
  echo "Saved image deployment plan applied once; intent and receipts are consumed."
  exit 0
fi

if bash "$gate_runner" mark-deployment-intent-outcome \
  --plan "$plan_path" \
  --apply-attempt-id "$TEAMAGENT_APPLY_ATTEMPT_ID" \
  --outcome reconcile-required >/dev/null 2>&1; then
  outcome_recorded=true
fi
bash "$gate_runner" release-deployment-lock \
  --plan "$plan_path" \
  --apply-attempt-id "$TEAMAGENT_APPLY_ATTEMPT_ID" >/dev/null 2>&1 || true
lock_acquired=false
echo "FATAL: apply exited nonzero after a one-time authorization attempt." >&2
echo "Reconcile Terraform/AWS state; never retry this plan, intent, or receipts." >&2
exit "$apply_status"
