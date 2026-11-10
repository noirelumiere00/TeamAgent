#!/usr/bin/env bash
# Offline plan by default; only --apply contacts GitHub/S3.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/mirror_build_tools.py" "$@"
