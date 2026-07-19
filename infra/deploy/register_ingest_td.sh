#!/usr/bin/env bash
# Retired: direct ingest task-definition registration bypassed release evidence.
set -euo pipefail

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'EOF'
usage: register_ingest_td.sh

This legacy task-definition deployer is permanently disabled. Deploy the MCP
digest with the guarded full saved-plan flow in infra/terraform/README.md.
After that deployment, scripts/aws/run_ingest_task.sh may start ingest work.
EOF
  exit 0
fi

cat >&2 <<'EOF'
FATAL: register_ingest_td.sh is permanently disabled.

The ingest task definition is Terraform-owned and depends on the production
release gate. Use authorize_image_release.sh, plan_image_release.sh, and
apply_image_release_plan.sh as documented in infra/terraform/README.md.
EOF
exit 64
