#!/usr/bin/env bash
# Deprecated unsafe entry point: intentionally incapable of building or pushing.
set -euo pipefail

cat >&2 <<'EOF'
FATAL: build_openclaw_image.sh is disabled.
Shared/legacy image-only OpenClaw builds are forbidden.
Use infra/deploy/build_openclaw_image.sh only after its checked-in core/media
contract reports release.ready=true. The dedicated launcher pins remote dev,
publishes signed immutable evidence, and invokes a fixed CodeBuild project.
EOF
exit 64
