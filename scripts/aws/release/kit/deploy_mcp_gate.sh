#!/usr/bin/env bash
# usage: deploy_mcp_gate.sh <PR...> -- rNNN "<説明>" <marker_path> <marker_pattern>
set -euo pipefail
# shellcheck source=common.sh
source "$(cd "$(dirname "$0")" && pwd)/common.sh" || exit 1
# shortcut: 読み取り確認なので開始直後の競合は防げない。厳密な排他が必要なら共有ロックへ。
python3 "$KIT_DIR/check_release_idle.py"

PRS=()
while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do
  [[ "$1" =~ ^[0-9]+$ ]] || { echo "FATAL: PR 番号が不正"; exit 2; }
  PRS+=("$1"); shift
done
[ "${#PRS[@]}" -gt 0 ] && [ "$#" -eq 5 ] && [ "$1" = "--" ] || {
  echo "usage: $0 <PR...> -- rNNN '<説明>' <確認するファイル> <確認する文字列>"; exit 2;
}
shift
RN="$1"; DESC="$2"; MP="$3"; MPAT="$4"
[[ "$RN" =~ ^r[0-9]+$ ]] || { echo "FATAL: 便名は r と数字"; exit 2; }
[ -n "$DESC" ] && [ -n "$MP" ] && [ -n "$MPAT" ] || { echo "FATAL: 説明・確認対象は空にできません"; exit 2; }
for n in "${PRS[@]}"; do
  [ "$(gh pr view "$n" -R noirelumiere00/TeamAgent --json state -q .state)" = "MERGED" ] || {
    echo "FATAL: PR #$n をマージしてから実行してください"; exit 1;
  }
done

OUT="$RELEASE_WORK_DIR/$RN"
mkdir "$OUT" || { echo "FATAL: $OUT が既にある場合はログを確認し、同じ便を撃ち直さないでください"; exit 1; }
if [ ! -d "$RELEASE_PAD" ]; then
  git clone --branch dev "$(git -C "$REPO_ROOT" remote get-url origin)" "$RELEASE_PAD"
fi
[ -z "$(git -C "$RELEASE_PAD" status --porcelain=v1 --untracked-files=all --ignore-submodules=none)" ] || {
  echo "FATAL: 発射台が汚れています: $RELEASE_PAD"; exit 1;
}
git -C "$RELEASE_PAD" fetch -q origin
git -C "$RELEASE_PAD" checkout -q dev
git -C "$RELEASE_PAD" merge --ff-only -q origin/dev
(
  cd "$RELEASE_PAD"
  LC_ALL=C PYTHONPATH=src uv run --locked --extra dev --extra mcp python infra/deploy/verify_generation_inputs.py \
    --manifest infra/deploy/buildspec_generation_inputs.json --repo-root .
) || { echo "STOP: generations stale。持ち主に世代の確認を依頼してください"; exit 1; }
LIVE=$(aws ecs describe-task-definition --task-definition "$(aws ecs describe-services \
  --cluster teamagent-dev --services teamagent-dev-mcp --query 'services[0].taskDefinition' --output text)" \
  --query 'taskDefinition.containerDefinitions[0].image' --output text | sed 's/.*@//')
python3 - "$KIT_DIR/releases/template.json" "$OUT/release.json" "$RN" "$DESC" "$MP" "$MPAT" "$LIVE" <<'PY'
import json, sys
src, dst, name, desc, path, pattern, live = sys.argv[1:]
d = json.load(open(src)); d['name'] = name; d['description'] = desc; d['OLD_CORE'] = live
d['checks'].append({'label': f'HEAD に {name} の変更が無い', 'kind': 'grep_fixed', 'path': path, 'pattern': pattern})
with open(dst, 'w') as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
PY
# 発射直前にも読み直す（PR/世代確認の間に始まった別便を止める）。
python3 "$KIT_DIR/check_release_idle.py"
bash "$KIT_DIR/fire_and_authorize.sh" "$OUT/release.json" "APPROVED: $RN $DESC" > "$OUT/fire.log" 2>&1 || {
  echo "FATAL: 発射・承認で停止。$OUT/fire.log を確認してください"; exit 1;
}
cmd=$(grep -E '^[[:space:]]*bash .*apply_mcp_tds.sh sha256:[0-9a-f]{64} sha256:[0-9a-f]{64}$' "$OUT/fire.log" | tail -1)
[ -n "$cmd" ] || { echo "FATAL: 適用する digest がログにありません"; exit 1; }
# ログのコマンドは実行せず、検証済みの末尾2引数だけを渡す。
MEDIA=${cmd##* }; cmd=${cmd% *}; CORE=${cmd##* }
bash "$KIT_DIR/answer_gate_compare.sh" "$CORE" > "$OUT/gate.log" 2>&1 || {
  echo "FATAL: 答え試験の関門で停止。$OUT/gate.log を確認してください"; exit 1;
}
bash "$KIT_DIR/apply_mcp_tds.sh" "$CORE" "$MEDIA" > "$OUT/apply.log" 2>&1 || {
  echo "FATAL: 差し替えで停止。$OUT/apply.log を確認してください"; exit 1;
}
echo "🏁 $RN 完了。ログ: $OUT。次は DM で実機確認してください。"
