#!/bin/sh
# OpenClaw 外殻の起動前 seed: 書込み可能 workspace にレビュー済みペルソナを配置してから gateway 起動。
# read-only config(OPENCLAW_CONFIG_PATH)はそのまま参照。secrets は env(ECS secrets)から。
set -eu

WS="${OPENCLAW_WORKSPACE_DIR:-/home/node/.openclaw/workspace}"
mkdir -p "$WS"
# 既存があれば上書きしない（-n）。state は触らない。
cp -n /opt/teamagent/workspace-seed/SOUL.md "$WS/SOUL.md" 2>/dev/null || true
cp -n /opt/teamagent/workspace-seed/HEARTBEAT.md "$WS/HEARTBEAT.md" 2>/dev/null || true

exec "$@"
