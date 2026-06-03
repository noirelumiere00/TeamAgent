"""Phase 1 ライブ: 実 `search` Skill を SDK オーケストレーターで回す（本番依存あり）.

fixture でなく **本物の SearchSkill**（pgvector(RDS) + Bedrock + LocalE5Embedder）をツール化して、
適応ループを実データで動かす。Phase 1 の最初のマイルストーン。

前提（揃って初めて動く）:
  - full env: 実 Skill の依存が入った venv（boto3/psycopg/sentence-transformers 等 + claude-agent-sdk==0.2.87）
  - SSM トンネル稼働（RDS → localhost:15432）。`set -a; source .env.production; set +a` + `source scripts/load_secrets.sh`
  - Bedrock: CLAUDE_CODE_USE_BEDROCK=1 / AWS資格情報（unset AWS_PROFILE で default） / AWS_REGION / TEAMAGENT_BEDROCK_MODEL
  - 会社プロキシ: SSL_CERT_FILE=~/.hermes/ca_bundle.pem / HF: HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1

実行:
  PYTHONPATH=src python scripts/run_orchestrator_prod.py "BtoB SaaS 採用領域の過去提案の勝ち筋を調べて要約して"
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from teamagent.orchestrator.factory import build_production_tools  # noqa: E402
from teamagent.orchestrator.sdk_runner import run_sdk_agent  # noqa: E402

_SYSTEM_PROMPT = """\
あなたは営業の調査・提案を支援するエージェントです。利用可能なツール（社内資料検索 search 等）を
使って、ユーザーの要求に答えてください。ツール結果を踏まえ、最後は必ず文章で結論をまとめること。
同じツールを同一入力で繰り返さないこと。
"""


def _preflight() -> list[str]:
    missing: list[str] = []
    if os.environ.get("CLAUDE_CODE_USE_BEDROCK") != "1":
        missing.append("CLAUDE_CODE_USE_BEDROCK=1")
    if not os.environ.get("AWS_REGION"):
        missing.append("AWS_REGION")
    if not os.environ.get("DATABASE_URL"):
        missing.append("DATABASE_URL（SSMトンネル + load_secrets.sh で設定）")
    return missing


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
    goal = sys.argv[1] if len(sys.argv) > 1 else "過去の提案資料から関連事例を調べて要約して"
    user_email = os.environ.get("TEAMAGENT_USER_EMAIL", "")

    result = await run_sdk_agent(
        goal=goal,
        request_id="req-prod-search-001",
        specs=build_production_tools(),
        model=model,
        system_prompt=_SYSTEM_PROMPT,
        user_id=os.environ.get("TEAMAGENT_USER_ID"),
        ctx_metadata={"user_email": user_email} if user_email else {},
        require_rls=bool(user_email),  # user_email があれば RLS 強制（fail-closed）
        max_turns=8,
        cost_cap_usd=0.5,
        tool_timeout_s=90.0,  # 実 search は embedder ロード + pgvector + Bedrock で重い
    )

    print("\n==================== 結果 (Phase 1 / 実 search) ====================")
    print(f"goal             : {goal}")
    print(f"stopped_reason   : {result.stopped_reason}  (is_error={result.is_error})")
    print(f"num_turns        : {result.num_turns}")
    print(f"cost (SDK実コスト): ${result.session_total_cost_usd}")
    print(f"tools(6-bis log) : {len(result.cost_records)} 呼び出し")
    print(f"answer           :\n{result.answer}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
