#!/usr/bin/env bash
# Retired: targeted control updates cannot bypass the full composed saved plan.
set -euo pipefail

echo "FATAL: retired launcher; use infra/deploy/terraform_runtime_guard.sh plan/apply" >&2
exit 64
