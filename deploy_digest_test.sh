#!/usr/bin/env bash
# Retired: the former digest test bypassed the provenance launcher and directly
# registered task definitions. Keep this path fail-loud for old operator notes.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SAFE_LAUNCHER="$SCRIPT_DIR/infra/deploy/build_teamagent_image.sh"
GUARDED_DEPLOY_RUNBOOK="$SCRIPT_DIR/infra/terraform/README.md"

cat >&2 <<EOF
FATAL: deploy_digest_test.sh is permanently disabled.

Build only (clean remote dev HEAD, assumed launcher role, quarantine gates):
  bash $SAFE_LAUNCHER --image-tag <tag> --with-scrape-tools true

Deploy separately from the verified release-repository digest by following:
  $GUARDED_DEPLOY_RUNBOOK

This compatibility stub never uploads source, starts CodeBuild, or changes
ECS/EventBridge resources.
EOF
exit 64
