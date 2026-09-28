"""学習ジョブの子プロセス（Hermes イメージの中でだけ動く）。

cwd と HERMES_HOME は runner が作った使い捨ての作業場。input.json の発話を読み、
memory ツールだけを持つ Hermes の AIAgent を 1 回動かし、
結果（成否と理由コードだけ）を result.json に書く。
記憶そのものは本家の memory ツールが HERMES_HOME/memories/ に書き、runner がそこから読む。

モデルを呼ぶ前に次をすべて確かめ、1 つでも外れたら止める（fail-closed）:
- Hermes が実際に読んだ config の有効値が学習係の前提どおりか（解釈の失敗も含む）
- 読み込まれたツールが memory だけか
- memory が本当に有効で、裏の review・skill の自動作成が止まっているか
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from .config import NEVER, bedrock_base_url, lint_config
from .prompt import LEARN_SYSTEM_PROMPT, render_utterances
from .runner import INPUT_FILE, RESULT_FILE

ALLOWED_TOOLS: Final = frozenset({"memory"})
MAX_ITERATIONS: Final = 6
RUN_BUDGET_S: Final = 90.0


def _write_result(home: Path, payload: dict[str, Any]) -> None:
    (home / RESULT_FILE).write_text(json.dumps(payload), encoding="utf-8")


def effective_config_problems() -> list[str]:
    """Hermes が実際に読んだ config を検査する（Hermes イメージの中でだけ呼べる）。"""
    from hermes_cli.config import (  # type: ignore[import-not-found]
        get_active_config_parse_failure,
        load_config_readonly,
    )

    config = load_config_readonly()
    problems = lint_config(config)
    if get_active_config_parse_failure() is not None:
        problems.append("config_parse_failure")
    return problems


def build_agent(model: str) -> Any:
    # Hermes 本体。Hermes イメージの中でだけ import できる
    from run_agent import AIAgent  # type: ignore[import-not-found]

    return AIAgent(
        provider="bedrock",
        # AIAgent を直接作ると Bedrock は Converse 経路になり、jp. 推論プロファイルの Claude が
        # 「model identifier is invalid」で弾かれる（09-25 実測）。
        # mcp と同じ AnthropicBedrock 経路を明示する
        api_mode="anthropic_messages",
        base_url=bedrock_base_url(os.environ["AWS_REGION"]),
        model=model,
        enabled_toolsets=["memory"],
        quiet_mode=True,
        skip_context_files=True,
        load_soul_identity=False,
        skip_background_review=True,
        max_iterations=MAX_ITERATIONS,
        run_budget_seconds=RUN_BUDGET_S,
        checkpoints_enabled=False,
        save_trajectories=False,
        ephemeral_system_prompt=LEARN_SYSTEM_PROMPT,
    )


def agent_state_ok(agent: Any) -> bool:
    """memory が本当に有効で、裏で勝手に書く経路が止まっているか。"""
    if getattr(agent, "_memory_store", None) is None:
        return False
    for attr in ("_memory_nudge_interval", "_skill_nudge_interval"):
        value = getattr(agent, attr, None)
        if not isinstance(value, int) or value < NEVER:
            return False
    return True


def succeeded(outcome: Any) -> bool:
    """Hermes は API が失敗しても例外を投げず、エラー文を最終応答にして返す（09-25 実測）。"""
    if not isinstance(outcome, dict):
        return False
    if outcome.get("failed") is True or outcome.get("interrupted") is True:
        return False
    if outcome.get("partial") or outcome.get("error") or outcome.get("failure_reason"):
        return False
    return outcome.get("completed") is True


def run(
    home: Path,
    agent_factory: Callable[[str], Any] = build_agent,
    config_checker: Callable[[], list[str]] = effective_config_problems,
) -> int:
    model = os.environ.get("AICO_HERMES_MODEL", "")
    if not model or Path(os.environ.get("HERMES_HOME", "")) != home:
        _write_result(home, {"ok": False, "code": "bad_env"})
        return 2
    try:
        data = json.loads((home / INPUT_FILE).read_text(encoding="utf-8"))
        utterances = data["utterances"]
        if not isinstance(utterances, list) or not all(isinstance(u, str) for u in utterances):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        _write_result(home, {"ok": False, "code": "bad_input"})
        return 2

    if config_checker():
        _write_result(home, {"ok": False, "code": "config_lint"})
        return 6
    agent = agent_factory(model)
    names = set(getattr(agent, "valid_tool_names", set()) or set())
    if names != ALLOWED_TOOLS:
        _write_result(home, {"ok": False, "code": "unexpected_tools"})
        return 3
    if not agent_state_ok(agent):
        _write_result(home, {"ok": False, "code": "bad_agent_state"})
        return 7
    try:
        outcome = agent.run_conversation(user_message=render_utterances(utterances))
    except Exception:
        # 例外文に発話が含まれうるので中身は出さない
        _write_result(home, {"ok": False, "code": "agent_error"})
        return 4
    if not succeeded(outcome):
        _write_result(home, {"ok": False, "code": "agent_failed"})
        return 5
    _write_result(home, {"ok": True})
    return 0


def main() -> int:
    return run(Path.cwd())


if __name__ == "__main__":
    sys.exit(main())
