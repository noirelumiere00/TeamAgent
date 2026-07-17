#!/usr/bin/env bash
# RETIRED: imageだけを差し替えるとUID/readonly/EFS/health/deployment/IAMの
# 一体契約を迂回するため、OpenClawの直接ECS更新経路は恒久的に拒否する。
set -euo pipefail

echo "ERROR: apply_openclaw.sh は退役しました。" >&2
echo "OpenClaw更新は terraform_runtime_guard.sh のexact migration/preflightを使用してください。" >&2
exit 64
