"""方式B PoC ライブデモ: Agent SDK on Bedrock で適応シナリオ①〜⑥を実Claudeで回す.

前提（揃って初めて動く）:
  export CLAUDE_CODE_USE_BEDROCK=1
  export AWS_REGION=us-east-1            # or ap-northeast-1
  export AWS_ACCESS_KEY_ID=... / AWS_SECRET_ACCESS_KEY=...  (or SSO/Role)
  export TEAMAGENT_BEDROCK_MODEL="us.anthropic.claude-sonnet-4-6"   # inference profile ID
  # SSL: 会社プロキシなら SSL_CERT_FILE=~/.hermes/ca_bundle.pem
  # Node 24 系（SDK が同梱CLIを spawn）

実行:
  cd ~/Documents/teamagent-orchestrator-poc
  PYTHONPATH=src .venv/bin/python scripts/poc_sdk_agent_demo.py

検証ポイント:
  - 実Claudeが「認知が滑った→CVへ方針転換」「MailでNG→別案へ差替」を自分で判断するか
  - bedrock_usage ログが **呼び出し毎** に request_id 付きで出るか（6-bis）
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "tests"))

from orchestrator.fixtures import scenario_tools  # noqa: E402  (tests/orchestrator)

from teamagent.orchestrator.sdk_runner import run_sdk_agent  # noqa: E402

_SYSTEM_PROMPT = """\
あなたは営業の提案を支援するエージェントです。利用可能なツールを使い、結果を見て次の手を
適応的に変えてください。手順の目安:
1) get_client_history でクライアントの過去施策と結果を確認する
2) 過去に不調だった訴求軸があれば、それを避けた軸（例: 認知が滑っていればCV）で draft_measure する
3) check_mail_constraints で手法がMail上のNGに触れないか確認し、NGなら別案に draft_measure し直す
4) search_past_cases で裏付けの成功事例を取得する
5) 過去の失敗を踏まえた方針・NG回避・裏付けを統合して最終提案を述べる

重要:
- 同じツールを2回呼んで同じ結果が返るなら、それ以上繰り返さず、自分で別の具体手法を
  指定して先に進むこと（同じドラフトを延々と作り直さない）。
- 履歴・(NGでない)施策案・裏付け事例が揃ったら、**必ず最後に文章で最終提案を書ききる**こと
  （ツール呼び出しだけで終了しない）。
"""


def _aws_creds_active() -> bool:
    """AWS 資格情報が解決可能か（env / default profile / SSO / IAM Role を網羅）。

    env に直接あれば即 True。なければ `aws sts get-caller-identity` で default チェーンを
    実際に検証する（default profile / 有効な SSO セッション / Role を拾う）。SSO 期限切れも検出。
    """
    if os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_SESSION_TOKEN"):
        return True
    aws = shutil.which("aws")
    if aws is None:
        # CLI 無し＝判定不能。AWS_PROFILE があれば SDK 側に委ねて通す。
        return bool(os.environ.get("AWS_PROFILE"))
    try:
        proc = subprocess.run([aws, "sts", "get-caller-identity"], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


async def _main() -> int:
    model = os.environ.get("TEAMAGENT_BEDROCK_MODEL") or os.environ.get(
        "BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6"
    )

    # --- preflight: 前提が未設定なら、SDKを呼ぶ前に分かりやすく即中止 ---
    missing: list[str] = []
    if os.environ.get("CLAUDE_CODE_USE_BEDROCK") != "1":
        missing.append("CLAUDE_CODE_USE_BEDROCK=1")
    if not os.environ.get("AWS_REGION"):
        missing.append("AWS_REGION（例: us-east-1）")
    if not _aws_creds_active():
        missing.append("有効なAWS資格情報（aws sso login / AWS_PROFILE / アクセスキー）")
    if missing:
        print("⛔ 実行前提が未設定のため中止します。以下を export してください:")
        for m in missing:
            print(f"   - {m}")
        print(
            "\n例:\n"
            "  export CLAUDE_CODE_USE_BEDROCK=1\n"
            "  export AWS_REGION=us-east-1\n"
            "  export TEAMAGENT_BEDROCK_MODEL=us.anthropic.claude-sonnet-4-6\n"
            "  export SSL_CERT_FILE=~/.hermes/ca_bundle.pem\n"
            "  # + AWS 資格情報（プロファイル/キー/SSO）を有効化"
        )
        return 2

    print(f"✅ preflight OK (model={model}, region={os.environ.get('AWS_REGION')})")
    result = await run_sdk_agent(
        goal="クライアントXに次の施策を提案して",
        request_id="req-sdk-demo-001",
        specs=scenario_tools(),
        model=model,
        system_prompt=_SYSTEM_PROMPT,
        max_turns=10,
        cost_cap_usd=0.50,
    )

    print("\n==================== 結果 (方式B / SDK on Bedrock) ====================")
    print(f"model            : {model}")
    print(f"num_turns        : {result.num_turns}")
    print(f"per-call records : {len(result.cost_records)} 件（6-bis ログ）")
    for r in result.cost_records:
        print(
            f"  - in={r.input_tokens} out={r.output_tokens} "
            f"cache_r={r.cache_read_tokens} cache_w={r.cache_creation_tokens} "
            f"cost=${r.cost_usd}"
        )
    print(f"cost (SDK実コスト=正): ${result.session_total_cost_usd}  ← 6-bis はこれを採用")
    print(f"cost (自前推定/参考)  : ${round(result.total_cost_usd, 6)}  ※価格テーブルは概算")
    print(f"answer           :\n{result.answer}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
