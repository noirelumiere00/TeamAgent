#!/usr/bin/env bash
# Deprecated unsafe entry point: intentionally incapable of building or pushing.
set -euo pipefail

cat >&2 <<'EOF'
FATAL: build_tiktok_image.sh is disabled.
TikTok worker images belong to the separate tiktok-data-service repository.
Use tiktok-data-service/scripts/build_acquire_image.sh through its dedicated,
commit-pinned CodeBuild path after that successor is merged and reviewed.
This TeamAgent helper must never package or upload TikTok source.
EOF
exit 64
