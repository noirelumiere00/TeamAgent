#!/usr/bin/env bash
# Retired: saved plans are applied only by the composed runtime/provenance guard.
set -euo pipefail

echo "FATAL: retired launcher; use infra/deploy/terraform_runtime_guard.sh apply" >&2
exit 64
