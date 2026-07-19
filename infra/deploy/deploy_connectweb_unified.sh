#!/usr/bin/env bash
# Retired: this script mixed image creation and runtime mutation. Build,
# release authorization, and deployment are intentionally separate now.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SAFE_LAUNCHER="$SCRIPT_DIR/build_teamagent_image.sh"
GUARDED_DEPLOY_RUNBOOK="$SCRIPT_DIR/../terraform/README.md"
GUARDED_RELEASE="$SCRIPT_DIR/authorize_image_release.sh"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<EOF
usage: deploy_connectweb_unified.sh

This legacy build-and-deploy entrypoint is permanently disabled.

Build only:
  bash $SAFE_LAUNCHER

Authorize an immutable release separately:
  bash $GUARDED_RELEASE --help

Deploy only through the one-use saved-plan flow documented in:
  $GUARDED_DEPLOY_RUNBOOK
EOF
  exit 0
fi

cat >&2 <<EOF
FATAL: deploy_connectweb_unified.sh is permanently disabled.

Build only (clean protected remote dev HEAD, assumed launcher role, quarantine gates):
  bash $SAFE_LAUNCHER

Authorize an active/rollback digest separately:
  bash $GUARDED_RELEASE --help

Deploy separately from the verified release-repository digest by following:
  $GUARDED_DEPLOY_RUNBOOK

This compatibility stub never uploads source, starts CodeBuild, or changes
runtime resources. It never registers task definitions or updates services.
EOF
exit 64
