#!/usr/bin/env bash
# 2026-09-24 r28: 本番の参照 6 か所が「新イメージ＋期待どおりの Gemini モデル」になっているかを確かめるだけのスクリプト（読み取りのみ・何度流してもよい）。
# 期待モデルは EXPECT_MODEL / EXPECT_LOC（既定は 09-24 裁定で据え置きの 2.5-flash@us-central1。3.5 へ切り替えた後は gemini-3.5-flash / global を渡す）。
# apply_r28_tds.sh の最後と、DM 実機確認の前に回す。1 つでも違えば exit 1 で、どこが違うかを表示する。
# 使い方: bash verify_r28_live.sh <sha256:core> <sha256:media>
set -euo pipefail
# shellcheck source=common.sh
source "$(cd "$(dirname "$0")" && pwd)/common.sh" || exit 1
CORE="${1:?usage: verify_r28_live.sh <sha256:core> <sha256:media>}"; MEDIA="${2:?usage: verify_r28_live.sh <sha256:core> <sha256:media>}"
MODEL="${EXPECT_MODEL:-gemini-2.5-flash}"; LOC="${EXPECT_LOC:-us-central1}"; CLUSTER=teamagent-dev
NG=0
td_of_service() {  # 09-24 レビュー D-04: IN_PROGRESS は最大 5 分待つ（1 回見て NG にすると r29 と同じ誤検知になる）
  local out
  for _ in $(seq 1 20); do
    out="$(aws ecs describe-services --cluster "$CLUSTER" --services "$1" --query 'services[0].deployments[?status==`PRIMARY`].[taskDefinition,rolloutState] | [0]' --output text)"
    case "$out" in *IN_PROGRESS*) sleep 15 ;; *) break ;; esac
  done
  echo "$out"
}
warn() { echo "  WARN $1: $2（期待: $3）"; }
td_field() { aws ecs describe-task-definition --task-definition "$1" --query "taskDefinition.containerDefinitions[0].$2" --output text; }
td_env() { aws ecs describe-task-definition --task-definition "$1" --query "taskDefinition.containerDefinitions[0].environment[?name=='$2'].value | [0]" --output text; }
check() {  # <label> <actual> <expected>
  if [ "$2" = "$3" ]; then echo "  OK  $1: $2"; else echo "  NG  $1: $2（期待: $3）"; NG=1; fi
}

echo "== mcp（service）"
read -r MCP_TD MCP_RO <<<"$(td_of_service teamagent-dev-mcp)"
check "mcp rollout" "$MCP_RO" COMPLETED
check "mcp image" "$(td_field "$MCP_TD" image | sed 's/.*@//')" "$CORE"
check "mcp GEMINI_MODEL_ID" "$(td_env "$MCP_TD" GEMINI_MODEL_ID)" "$MODEL"
check "mcp GEMINI_VERTEX_LOCATION" "$(td_env "$MCP_TD" GEMINI_VERTEX_LOCATION)" "$LOC"
check "mcp USE_MAIL_DRAFT_TOOL（既存 env が残っているか）" "$(td_env "$MCP_TD" USE_MAIL_DRAFT_TOOL)" "true"

echo "== connect-web（service）"
read -r CW_TD CW_RO <<<"$(td_of_service teamagent-dev-connect-web)"
check "connect-web rollout" "$CW_RO" COMPLETED
check "connect-web image" "$(td_field "$CW_TD" image | sed 's/.*@//')" "$CORE"

echo "== morning-digest / canary（EventBridge target）"
for R in teamagent-dev-morning-digest-weekday teamagent-dev-morning-digest-planner teamagent-dev-canary-hourly; do  # 10-08 planner を追加
  T="$(aws events list-targets-by-rule --rule "$R" --query 'Targets[0].EcsParameters.TaskDefinitionArn' --output text)"
  check "$R image" "$(td_field "$T" image | sed 's/.*@//')" "$CORE"
done

echo "== ingest（dispatch Lambda）"
IN_TD="$(aws lambda get-function-configuration --function-name teamagent-dev-ingest-dispatch --query 'Environment.Variables.TASKDEF_ARN' --output text)"
check "ingest image" "$(td_field "$IN_TD" image | sed 's/.*@//')" "$CORE"
# 09-24 レビュー D-05: ingest は Gemini を呼ばない（env は mcp との見た目合わせ）ので、食い違いは WARN にとどめる
IN_MODEL="$(td_env "$IN_TD" GEMINI_MODEL_ID)"; IN_LOC="$(td_env "$IN_TD" GEMINI_VERTEX_LOCATION)"
if [ "$IN_MODEL" = "$MODEL" ] && [ "$IN_LOC" = "$LOC" ]; then echo "  OK  ingest Gemini env: ${IN_MODEL}@${IN_LOC}"; else warn "ingest Gemini env（機能影響なし）" "${IN_MODEL}@${IN_LOC}" "${MODEL}@${LOC}"; fi

echo "== tiktok-acquire（dispatch Lambda）"
TK_TD="$(aws lambda get-function-configuration --function-name teamagent-dev-tiktok-acquire-dispatch --query 'Environment.Variables.TASKDEF_ARN' --output text)"
check "tiktok-acquire image" "$(td_field "$TK_TD" image | sed 's/.*@//')" "$MEDIA"

echo "== openclaw（service・bounce 後に安定しているか）"
read -r OC_TD OC_RO <<<"$(td_of_service teamagent-dev-openclaw)"
check "openclaw rollout" "$OC_RO" COMPLETED

echo "== 参考: x-buzz worker（30 日呼び出し 0 件・差し替え対象外。古い mcp イメージのまま）"
echo "  $(td_field teamagent-dev-x-buzz-worker image | sed 's/.*@//' | cut -c1-19)…"

if [ "$NG" = "0" ]; then echo "🏁 本番 6 か所すべて期待どおり"; else echo "❌ 期待と違う参照あり（上の NG 行）"; exit 1; fi
