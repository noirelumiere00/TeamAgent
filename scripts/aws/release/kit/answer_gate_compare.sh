#!/bin/bash
# 便の関門: 候補イメージと今の本番イメージで答え試験を回し、「本番で合格していた問題が候補で不合格」なら止める（exit 1）。
# 使い方: answer_gate_compare.sh sha256:<candidate core digest>
set -uo pipefail
# shellcheck source=common.sh
source "$(cd "$(dirname "$0")" && pwd)/common.sh" || exit 1
T="$(cd "$(dirname "$0")" && pwd)"
CAND="${1:-}"; [[ "$CAND" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "usage: $0 sha256:<digest>"; exit 2; }
LIVE=$(aws ecs describe-task-definition --task-definition "$(aws ecs describe-services --cluster teamagent-dev --services teamagent-dev-mcp --query 'services[0].taskDefinition' --output text)" --query 'taskDefinition.containerDefinitions[0].image' --output text | sed 's/.*@//')
[[ "$LIVE" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "FATAL: 本番 digest が読めない"; exit 1; }
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
echo "== 候補 ${CAND:0:19}…"; bash "$T/run_answer_gate.sh" "$CAND" | tee "$W/cand.txt"
if [ "$LIVE" = "$CAND" ]; then echo "候補＝本番（比較なし）"; grep -q "🏁 答え試験 合格" "$W/cand.txt"; exit $?; fi
echo "== 本番 ${LIVE:0:19}…（対照）"; bash "$T/run_answer_gate.sh" "$LIVE" | tee "$W/live.txt"
grep -q "ANSWER_GATE_EXIT" "$W/cand.txt" || { echo "FATAL: 候補の試験が完走していない"; exit 1; }
grep -q "ANSWER_GATE_EXIT" "$W/live.txt" || { echo "FATAL: 本番の試験が完走していない"; exit 1; }
# 本物の LLM は 1 回だと揺れる（11/10 r113 で draft が候補だけ 1 回落ちた）。
# 本番より悪く見えたら候補をもう 1 回回し、2 回とも落ちた問題だけを悪化と数える。
if python3 - "$W/live.txt" "$W/cand.txt" <<'PY'
import re, sys
r = lambda p: {m[1]: m[2] for m in re.finditer(r"ANSWER_GATE (\S+) (PASS|FAIL)", open(p).read())}
live, cand = r(sys.argv[1]), r(sys.argv[2])
sys.exit(0 if any(v == "FAIL" and live.get(k) == "PASS" for k, v in cand.items()) else 1)
PY
then echo "== 候補を再試験（本番より悪く見えた問題があるため）"; bash "$T/run_answer_gate.sh" "$CAND" | tee "$W/cand2.txt"; else : > "$W/cand2.txt"; fi
python3 - "$W/live.txt" "$W/cand.txt" "$W/cand2.txt" <<'PY'
import re, sys
def read(p):
    return {m[1]: m[2] for m in re.finditer(r"ANSWER_GATE (\S+) (PASS|FAIL)", open(p).read())}
live, cand, cand2 = read(sys.argv[1]), read(sys.argv[2]), read(sys.argv[3])
# 2 回目で通った問題は揺れとして合格扱い
cand = {k: ("PASS" if v == "FAIL" and cand2.get(k) == "PASS" else v) for k, v in cand.items()}
if not live or not cand: print("FATAL: 候補の結果が読めない"); sys.exit(1)
worse = sorted(k for k, v in cand.items() if v == "FAIL" and live.get(k) == "PASS")
better = sorted(k for k, v in cand.items() if v == "PASS" and live.get(k) == "FAIL")
missing = sorted(set(live) - set(cand))
print(f"答え試験の比較: 候補 合格 {sum(v=='PASS' for v in cand.values())}/{len(cand)}・本番 合格 {sum(v=='PASS' for v in live.values())}/{len(live)}")
if better: print("  良くなった:", ", ".join(better))
if worse or missing:
    print("FATAL: 本番より悪くなった:", ", ".join(worse + [m + "（未実行）" for m in missing]), "→ 本番反映しない"); sys.exit(1)
print("🏁 答え試験の関門 通過（本番より悪くなった問題なし）")
PY
