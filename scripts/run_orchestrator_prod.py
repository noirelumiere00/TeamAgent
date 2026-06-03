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
あなたは営業の調査・提案を支援するエージェントです。利用可能なツールを使い、結果を見て次の手を
適応的に変えてください。代表的な流れ:
1) clientkarte でクライアントの過去提案履歴・温度感・次アクションを把握
2) search で関連する過去事例・勝ち筋を調べる
3) proposal_draft で施策ドラフトを作る
4) proposal_review でドラフトを過去の勝ち筋/失注理由と照合して診断し、弱ければ作り直す
5) 最後は必ず文章で最終提案（根拠・過去の踏まえ・想定リスク）をまとめる
同じツールを同一入力で繰り返さないこと。ツールが使えない時は別手段か、得た情報でまとめること。

【グラウンディング厳守】
- 事実・事例・数値・出典は、必ず **ツールが返した結果（search の hits 等）のみ** を根拠にすること。
- 出典を書くときは search が返した file_name / score など **実際に返ってきた値** を引用する。
- ファイルパス・Slackチャンネル名・「Day○記録」・スクリプト名などを **推測で創作してはならない**。
  自分の事前知識やこの作業環境の話を持ち込まない。ツール結果に無いものは「無い」と書く。
- search が hits を返したら、それを使って具体的に答える。「DB未参照」「暫定版」と暈さない。
- 本当に hits が 0 件のときだけ「該当データ無し（データ未取込の可能性）」と明記する。
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
