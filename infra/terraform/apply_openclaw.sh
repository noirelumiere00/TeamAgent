#!/usr/bin/env bash
# Retired: direct task-definition registration bypassed runtime and release evidence.
set -euo pipefail

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  cat <<'EOF'
usage: apply_openclaw.sh

This legacy deployer is permanently disabled. Use the single guarded saved-plan
workflow documented in infra/terraform/README.md.
EOF
  exit 0
fi

cat >&2 <<'EOF'
FATAL: apply_openclaw.sh is permanently disabled.

OpenClaw image changes require exact runtime migration/preflight evidence,
fresh signed release evidence, a unique full saved plan, one-use intent, and
application under the shared lock. Follow infra/terraform/README.md.
EOF
exit 64
