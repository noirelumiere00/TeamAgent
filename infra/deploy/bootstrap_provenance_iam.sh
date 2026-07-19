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

`run` is an AWS-mutating, one-time operation. It requires the exact account
root identity to be an MFA-authenticated temporary session. Root creates only
the temporary seed stack and assumes its one-hour role. The seed role is
explicitly denied build, release, evidence-object, image, long-lived
credential, and runtime writes.

The bootstrap applies one fixed create/no-op-only saved plan directly to the
existing main Terraform backend, verifies main-state ownership, burns a
conditional one-use ledger row, revokes the seed session, and deletes the seed
stack. It never calls a build or release launcher.
EOF
}

for tool in python3; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)" \
  || die "bootstrap repository root cannot be resolved"
HELPER="$REPO_ROOT/infra/bootstrap/provenance_iam_bootstrap.py"
CONTRACT="$REPO_ROOT/infra/bootstrap/bootstrap_contract.json"
[ -f "$HELPER" ] && [ -f "$CONTRACT" ] || die "bootstrap controls are incomplete"

[ "$#" -ge 1 ] || {
  usage >&2
  exit 2
}
COMMAND="$1"
shift
case "$COMMAND" in
  validate-contract)
    [ "$#" -eq 0 ] || die "validate-contract accepts no additional arguments"
    exec python3 -I "$HELPER" validate-contract \
      --repo-root "$REPO_ROOT" \
      --contract "$CONTRACT"
    ;;
  run)
    for tool in aws git terraform; do
      command -v "$tool" >/dev/null 2>&1 || die "$tool is required for run"
    done
    exec python3 -I "$HELPER" run \
      --repo-root "$REPO_ROOT" \
      --contract "$CONTRACT" \
      "$@"
    ;;
  -h|--help)
    [ "$#" -eq 0 ] || die "--help accepts no additional arguments"
    usage
    ;;
  *)
    usage >&2
    die "unknown command: $COMMAND"
    ;;
esac
