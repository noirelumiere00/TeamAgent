#!/usr/bin/env bash
# Retired: direct task-definition registration bypassed release evidence.
set -euo pipefail

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  cat <<'EOF'
usage: apply_openclaw.sh

This legacy deployer is permanently disabled. Use authorize_image_release.sh,
plan_image_release.sh, and apply_image_release_plan.sh as documented in
infra/terraform/README.md.
EOF
  exit 0
fi

cat >&2 <<'EOF'
FATAL: apply_openclaw.sh is permanently disabled.

OpenClaw image changes require a fresh signed release receipt, a unique saved
plan prepared by plan_image_release.sh, and one-time application through
apply_image_release_plan.sh. Follow infra/terraform/README.md.
EOF
exit 64
