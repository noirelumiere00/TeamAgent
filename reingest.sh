#!/usr/bin/env bash
# Retired: this helper registered an ad hoc image-bearing task definition.
set -euo pipefail

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  cat <<'EOF'
usage: reingest.sh

This legacy image-and-run helper is permanently disabled. First deploy the MCP
digest through the guarded full saved-plan flow in infra/terraform/README.md.
Then run scripts/aws/run_ingest_task.sh with the desired --sources option.
EOF
  exit 0
fi

cat >&2 <<'EOF'
FATAL: reingest.sh is permanently disabled.

It may not register an ad hoc task-definition revision. Deploy the signed MCP
release with infra/terraform/plan_image_release.sh and
infra/terraform/apply_image_release_plan.sh, then use
scripts/aws/run_ingest_task.sh to start the Terraform-owned ingest family.
See infra/terraform/README.md for the complete guarded release procedure.
EOF
exit 64
