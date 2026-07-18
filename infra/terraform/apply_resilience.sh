#!/usr/bin/env bash
# Retired: targeted plans/applies bypass runtime and production image gates.
set -euo pipefail

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  cat <<'EOF'
usage: apply_resilience.sh

This targeted deployer is permanently disabled. Use the single guarded full
saved-plan workflow documented in infra/terraform/README.md.
EOF
  exit 0
fi

cat >&2 <<'EOF'
FATAL: apply_resilience.sh is retired and permanently disabled.

Production resources may only be changed by the composed runtime and image
release guard using a full saved plan whose unique intent and signed receipts
are consumed under the shared lock. Follow infra/terraform/README.md.
EOF
exit 64
