#!/usr/bin/env bash
# RETIRED: targeted plans bypass the complete-plan runtime/drift allowlist.
set -euo pipefail

echo "apply_resilience.sh is retired; use infra/deploy/terraform_runtime_guard.sh." >&2
exit 64
