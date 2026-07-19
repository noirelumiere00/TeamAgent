#!/usr/bin/env bash
# Retired: runtime and provenance authorization now share one entrypoint.
set -euo pipefail

echo "FATAL: retired launcher; use infra/deploy/terraform_runtime_guard.sh plan" >&2
exit 64
