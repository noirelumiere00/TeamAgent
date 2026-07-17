#!/usr/bin/env bash
# Compatibility entry point. The provenance-gated launcher owns all build logic.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SAFE_LAUNCHER="$SCRIPT_DIR/infra/deploy/build_teamagent_image.sh"

if [ ! -x "$SAFE_LAUNCHER" ]; then
  echo "FATAL: safe TeamAgent image launcher is missing or not executable: $SAFE_LAUNCHER" >&2
  exit 1
fi

# The target requires --image-tag and an explicit --with-scrape-tools true.
# An old argument-free invocation therefore stops before any AWS operation.
exec "$SAFE_LAUNCHER" "$@"
