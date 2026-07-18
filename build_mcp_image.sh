#!/usr/bin/env bash
# Compatibility entry point. The provenance-gated launcher owns all build logic.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SAFE_LAUNCHER="$SCRIPT_DIR/infra/deploy/build_teamagent_image.sh"

if [ ! -x "$SAFE_LAUNCHER" ]; then
  echo "FATAL: safe TeamAgent image launcher is missing or not executable: $SAFE_LAUNCHER" >&2
  exit 1
fi

# The target accepts no mutable image tag, source path, project, repository, or
# endpoint override. It performs the complete build-only signed candidate flow.
exec "$SAFE_LAUNCHER" "$@"
