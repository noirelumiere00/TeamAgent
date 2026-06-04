"""Phase 4 ライブ評価: ゴールドセットを実 SDK on Bedrock で回し、ツール選択を採点する。

⚠️ **課金あり**（1 ケース ≈ $0.1）。手動/nightly 用。CI では回さない（CI は
`tests/orchestrator/test_orchestration_eval.py` の決定的採点のみ・課金0）。

各ケースについて run_sdk_agent を実行し、`result.tool_calls` と `num_turns` を
`score_case` で採点、最後に合格率と総コストを表示する。

needs_flags（例: USE_MAIL_TOOLS）が env で満たされないケースは **スキップして明示ログ**
（暗黙の打ち切りをしない）。mail 系は 6c の人間ゲート承認後にのみ env を立てて評価する。

実行（非 mail の 8 ケースのみ・約 $1）:
  # run_orchestrator_prod.py と同じ env（us. プロファイル / RLS email / トンネル / SSL / Node）
  PYTHONPATH=src python scripts/eval_orchestration.py
  PYTHONPATH=src python scripts/eval_orchestration.py 3   # 先頭3ケースだけ（コスト抑制）
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))  # `scripts.*` を import 可能にする（system_prompt 共有）

# run_orchestrator_prod.py と同じ system_prompt を使う（評価対象は本番と同じ挙動）。
from scripts.run_orchestrator_prod import _build_system_prompt  # noqa: E402
from teamagent.orchestrator.eval import (  # noqa: E402
    GOLD_CASES,
    CaseScore,
    GoldCase,
    score_case,
    summarize,
)
from teamagent.orchestrator.factory import build_production_tools  # noqa: E402
from teamagent.orchestrator.faithfulness import (  # noqa: E402
    FaithfulnessScore,
    score_faithfulness,
)
from teamagent.orchestrator.sdk_runner import run_sdk_agent  # noqa: E402


def _preflight() -> list[str]:
    missing: list[str] = []
    if os.environ.get("CLAUDE_CODE_USE_BEDROCK") != "1":
        missing.append("CLAUDE_CODE_USE_BEDROCK=1")
    if not os.environ.get("AWS_REGION"):
        missing.append("AWS_REGION")
    if not os.environ.get("DATABASE_URL"):
        missing.append("DATABASE_URL（SSMトンネル + load_secrets.sh）")
    return missing


def _flags_satisfied(case: GoldCase) -> bool:
    return all(os.environ.get(f, "").lower() in ("1", "true", "yes") for f in case.needs_flags)


async def _run_one(
    case: GoldCase, model: str, user_email: str
) -> tuple[CaseScore, float, FaithfulnessScore]:
    result = await run_sdk_agent(
        goal=case.goal,
        request_id=f"eval-{case.id}",
        specs=build_production_tools(),
        model=model,
        system_prompt=_build_system_prompt(),
        ctx_metadata={"user_email": user_email} if user_email else {},
        require_rls=bool(user_email),
        max_turns=case.max_turns + 2,  # 採点は max_turns 基準。実行は少し余裕を持たせる
        cost_cap_usd=0.5,
        tool_timeout_s=90.0,
    )
    score = score_case(case, result.tool_calls, result.num_turns)
    cost = result.session_total_cost_usd or result.total_cost_usd
    # ⑤-g: 引用忠実性も常設指標化（捏造引用＝最終回答が実取得 chunk_id 以外を引用）。
    faith = score_faithfulness(result.answer, result.available_chunk_ids)
    return score, float(cost), faith


async def _main() -> int:
    missing = _preflight()
    if missing:
        print("⛔ 実行前提が未設定:")
        for m in missing:
            print(f"   - {m}")
        return 2

    model = os.environ.get("TEAMAGENT_BEDROCK_MODEL") or os.environ.get(
        "BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6"
    )
    user_email = os.environ.get("TEAMAGENT_USER_EMAIL", "")
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else len(GOLD_CASES)

    scores: list[CaseScore] = []
    faiths: list[FaithfulnessScore] = []
    skipped: list[str] = []
    total_cost = 0.0
    print(f"==== Phase 4 orchestration eval（{min(limit, len(GOLD_CASES))} ケース上限）====\n")
    for case in GOLD_CASES[:limit]:
        if not _flags_satisfied(case):
            skipped.append(case.id)
            print(f"⏭️  SKIP {case.id}（needs_flags 未設定: {', '.join(case.needs_flags)}）")
            continue
        score, cost, faith = await _run_one(case, model, user_email)
        total_cost += cost
        mark = "✅ PASS" if score.passed else "❌ FAIL"
        fab = f" ⚠️捏造{list(faith.fabricated)}" if faith.fabricated else ""
        print(
            f"{mark} {case.id}  tools={list(score.tools_called)} turns={score.num_turns} "
            f"${cost:.4f}  cite={len(faith.valid)}/{len(faith.cited)}{fab}"
        )
        if not score.passed:
            print(f"        理由: {' / '.join(score.reasons)}")
        scores.append(score)
        faiths.append(faith)

    print("\n==== サマリ ====")
    summary = summarize(scores)
    print(f"採点: {summary}")
    # ⑤-g: 忠実性サマリ（捏造引用は 0 が望ましい＝回帰監視）。
    total_fab = sum(len(f.fabricated) for f in faiths)
    fab_cases = [s.case_id for s, f in zip(scores, faiths, strict=False) if f.fabricated]
    print(f"忠実性: 捏造引用 {total_fab} 件（{len(fab_cases)}/{len(faiths)} ケース・0が望ましい）")
    if fab_cases:
        print(f"  ⚠️ 捏造のあったケース: {fab_cases}")
    if skipped:
        print(f"スキップ（needs_flags 未充足）: {skipped}")
    print(f"総コスト（SDK実コスト）: ${round(total_cost, 4)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
