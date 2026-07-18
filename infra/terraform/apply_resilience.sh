#!/usr/bin/env bash
# Retired: targeted plans/applies could bypass the production image gate.
set -euo pipefail

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  cat <<'EOF'
usage: apply_resilience.sh

This targeted deployer is permanently disabled. Use a full saved plan through
plan_image_release.sh and apply_image_release_plan.sh; see
infra/terraform/README.md.
EOF
  exit 0
fi

cat >&2 <<'EOF'
FATAL: apply_resilience.sh is permanently disabled.

Production image-bearing resources may only be changed by a full saved plan
whose unique intent and signed receipts are consumed immediately before apply.
Follow infra/terraform/README.md.
EOF
exit 64
