#!/usr/bin/env bash
# Retired: this script used to mix source upload, StartBuild overrides, and ECS
# mutation. Build and deployment are intentionally separate operations now.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SAFE_LAUNCHER="$SCRIPT_DIR/build_teamagent_image.sh"
GUARDED_DEPLOY_RUNBOOK="$SCRIPT_DIR/../terraform/README.md"

cat >&2 <<EOF
FATAL: deploy_connectweb_unified.sh is permanently disabled.

Build only (clean remote dev HEAD, assumed launcher role, quarantine gates):
  bash $SAFE_LAUNCHER --image-tag <tag> --with-scrape-tools true

Deploy separately from the verified release-repository digest by following:
  $GUARDED_DEPLOY_RUNBOOK

This compatibility stub never uploads source, starts CodeBuild, or changes
ECS/EventBridge resources.
EOF
exit 64
