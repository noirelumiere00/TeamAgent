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
# env 未設定なら焼き込み済み allowFrom が fallback（後方互換）。
# §T2b(2026-06-22): dmPolicy は "open"。OpenClaw は open でも allowFrom に "*" が無いと列挙者のみ
# 通す allowlist として無音 gating する（実コード確認）ため、全社内開放は SLACK_DM_ALLOWLIST="*"。
if [ -n "${SLACK_DM_ALLOWLIST:-}" ]; then
  node -e "const fs=require('fs');const p=process.env.OPENCLAW_CONFIG_PATH;const c=JSON.parse(fs.readFileSync(p,'utf8'));c.channels=c.channels||{};c.channels.slack=c.channels.slack||{};c.channels.slack.allowFrom=process.env.SLACK_DM_ALLOWLIST.split(',').map(s=>s.trim()).filter(Boolean);fs.writeFileSync(p,JSON.stringify(c,null,2));console.error('[entrypoint] allowFrom injected: '+c.channels.slack.allowFrom.length+' user(s)');" \
    || echo "[entrypoint] WARN: allowFrom 注入失敗（焼き込み値を使用）" >&2
fi

# 柱1（2026-06-22 事故対策）: 注入後の「実効 config」の不変条件を起動時に fail-loud 検査する。
# dmPolicy:open ⇒ allowFrom に "*" 必須 / allowFrom 空[]=全拒否 を禁止。違反なら構造化 error event を
# 出して exit 非0（タスクが可視的に crash）＝無音 gating で動き続けるより遥かにマシ。OK なら実効値を echo。
node -e '
const fs=require("fs");
const c=JSON.parse(fs.readFileSync(process.env.OPENCLAW_CONFIG_PATH,"utf8"));
const s=(c.channels&&c.channels.slack)||{};
const af=Array.isArray(s.allowFrom)?s.allowFrom:[];
function fail(reason){console.error(JSON.stringify({event:"openclaw_config_invariant_violation",level:"error",dmPolicy:s.dmPolicy,allowFromCount:af.length,reason:reason}));process.exit(1);}
if(s.dmPolicy==="open" && af.indexOf("*")<0) fail("dmPolicy open but allowFrom missing wildcard - would silently gate non-admins");
if(Array.isArray(s.allowFrom) && s.allowFrom.length===0) fail("allowFrom is empty array = deny-all");
console.error(JSON.stringify({event:"openclaw_config_ok",dmPolicy:s.dmPolicy,groupPolicy:s.groupPolicy,allowFromCount:af.length}));
'

exec "$@"
