#!/bin/sh
# OpenClaw 外殻の起動前 seed: 書込み可能 workspace にレビュー済みペルソナを配置してから gateway 起動。
# read-only config(OPENCLAW_CONFIG_PATH)はそのまま参照。secrets は env(ECS secrets)から。
set -eu

WS="${OPENCLAW_WORKSPACE_DIR:-/home/node/.openclaw/workspace}"
mkdir -p "$WS"
# 既存があれば上書きしない（-n）。state は触らない。
cp -n /opt/teamagent/workspace-seed/SOUL.md "$WS/SOUL.md" 2>/dev/null || true
cp -n /opt/teamagent/workspace-seed/HEARTBEAT.md "$WS/HEARTBEAT.md" 2>/dev/null || true

# §U: DM 許可リスト（allowFrom）を env SLACK_DM_ALLOWLIST（カンマ区切り Slack user_id）から
# 起動時に注入する。OpenClaw 標準は allowFrom の env 展開に非対応のため、ここで config を書き換える。
# これにより「メンバー追加 = env 変更 + 再デプロイ（image rebuild 不要）」で 15名規模まで可動。
# env 未設定なら焼き込み済み allowFrom が fallback（後方互換）。dmPolicy:allowlist は維持＝外部ゲスト排除。
if [ -n "${SLACK_DM_ALLOWLIST:-}" ]; then
  node -e "const fs=require('fs');const p=process.env.OPENCLAW_CONFIG_PATH;const c=JSON.parse(fs.readFileSync(p,'utf8'));c.channels=c.channels||{};c.channels.slack=c.channels.slack||{};c.channels.slack.allowFrom=process.env.SLACK_DM_ALLOWLIST.split(',').map(s=>s.trim()).filter(Boolean);fs.writeFileSync(p,JSON.stringify(c,null,2));console.error('[entrypoint] allowFrom injected: '+c.channels.slack.allowFrom.length+' user(s)');" \
    || echo "[entrypoint] WARN: allowFrom 注入失敗（焼き込み値を使用）" >&2
fi

exec "$@"
