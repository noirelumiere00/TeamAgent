#!/usr/bin/env bash
# Deprecated unsafe entry point: intentionally incapable of building or pushing.
set -euo pipefail

cat >&2 <<'EOF'
FATAL: build_openclaw_image.sh is disabled.
No provenance-pinned, vulnerability-gated OpenClaw builder exists yet.
Use the dedicated version-pinned OpenClaw launcher under infra/deploy only after
that successor is implemented and reviewed. Inline buildspec/source overrides
and mutable source archives are forbidden.
EOF
exit 64
