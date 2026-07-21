#!/usr/bin/env bash
# One-time provenance/IAM bootstrap entrypoint. All policy lives in the pinned
# Python validator and CloudFormation seed under infra/bootstrap.
set -euo pipefail
umask 077

die() {
  echo "FATAL: $*" >&2
  exit 2
}

usage() {
  cat <<'EOF'
usage:
  bootstrap_provenance_iam.sh validate-contract
  bootstrap_provenance_iam.sh run \
    --var-file /secure/teamagent.tfvars \
    --artifact-dir /secure/new-bootstrap-artifacts
  bootstrap_provenance_iam.sh reconcile-retire \
    --artifact-dir /secure/existing-bootstrap-artifacts

`run` is an AWS-mutating, one-time operation. It requires the exact account
root identity to be an MFA-authenticated temporary session. Root creates only
the temporary seed stack and assumes its one-hour role. The seed role is
explicitly denied build, release, evidence-object, image, long-lived
credential, and runtime writes.

The bootstrap applies one fixed create/no-op-only saved plan directly to the
existing main Terraform backend, verifies main-state ownership, burns a
conditional one-use ledger row, revokes the seed session, and deletes the seed
stack. It never calls a build or release launcher.

`reconcile-retire` is idempotent. It validates durable handoff artifacts and
live main-state ownership, never executes an infrastructure apply, never reapplies a
consumed plan, and retires only the nonce-owned seed stack.
EOF
}

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)" \
  || die "bootstrap repository root cannot be resolved"

[ "$#" -ge 1 ] || {
  usage >&2
  exit 2
}
COMMAND="$1"
shift
if [ "$COMMAND" = "-h" ] || [ "$COMMAND" = "--help" ]; then
  [ "$#" -eq 0 ] || die "--help accepts no additional arguments"
  usage
  exit 0
fi
case "$COMMAND" in
  validate-contract|run|reconcile-retire) ;;
  *)
    usage >&2
    die "unknown command: $COMMAND"
    ;;
esac

for tool in git python3; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done

# Preserve root's explicit temporary credentials only in non-exported shell
# variables. No Git, hash, contract, or materialization child receives them.
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

REVIEW_TMP="$(mktemp -d "${TMPDIR:-/tmp}/teamagent-bootstrap-review.XXXXXXXX")"
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
  --profile bootstrap-iam \
  || die "bootstrap wrapper provenance validation failed"
HELPER="$REVIEW_ROOT/infra/bootstrap/provenance_iam_bootstrap.py"
CONTRACT="$REVIEW_ROOT/infra/bootstrap/bootstrap_contract.json"
[ -f "$HELPER" ] && [ -f "$CONTRACT" ] || die "reviewed bootstrap controls are incomplete"

restore_root_credentials() {
  [ -n "$SAVED_AWS_ACCESS_KEY_ID" ] \
    && [ -n "$SAVED_AWS_SECRET_ACCESS_KEY" ] \
    && [ -n "$SAVED_AWS_SESSION_TOKEN" ] \
    || die "root must be supplied as an explicit temporary STS credential set"
  export AWS_ACCESS_KEY_ID="$SAVED_AWS_ACCESS_KEY_ID"
  export AWS_SECRET_ACCESS_KEY="$SAVED_AWS_SECRET_ACCESS_KEY"
  export AWS_SESSION_TOKEN="$SAVED_AWS_SESSION_TOKEN"
  unset SAVED_AWS_ACCESS_KEY_ID SAVED_AWS_SECRET_ACCESS_KEY SAVED_AWS_SESSION_TOKEN
}

case "$COMMAND" in
  validate-contract)
    [ "$#" -eq 0 ] || die "validate-contract accepts no additional arguments"
    python3 -I "$HELPER" validate-contract \
      --repo-root "$REVIEW_ROOT" \
      --contract "$CONTRACT"
    ;;
  run)
    for tool in aws git terraform; do
      command -v "$tool" >/dev/null 2>&1 || die "$tool is required for run"
    done
    restore_root_credentials
    python3 -I "$HELPER" run \
      --repo-root "$REVIEW_ROOT" \
      --contract "$CONTRACT" \
      "$@"
    ;;
  reconcile-retire)
    for tool in aws git terraform; do
      command -v "$tool" >/dev/null 2>&1 || die "$tool is required for reconcile-retire"
    done
    restore_root_credentials
    python3 -I "$HELPER" reconcile-retire \
      --repo-root "$REVIEW_ROOT" \
      --contract "$CONTRACT" \
      "$@"
    ;;
  *)
    usage >&2
    die "unknown command: $COMMAND"
    ;;
esac
