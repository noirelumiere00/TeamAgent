"""ローカル CLI エントリポイント。

Usage:
    python -m teamagent.runtime.local search '{"query": "PR代行の業界別実績は？", "top_k": 3}'

CLAUDE.md 6-bis 3層分離の Runtime 層。
Lambda 実行用エントリポイントは別途 runtime/lambda_handler.py で実装する。
"""

from __future__ import annotations

import argparse
import json
import sys

import structlog

# Skill を import すると register デコレータが走り Registry に登録される
import teamagent.skills.search.skill  # noqa: F401  # ensure registration
from teamagent.skills.base import SkillContext, SkillRegistry

logger = structlog.get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    """ローカル実行のメイン関数。"""
    parser = argparse.ArgumentParser(description="TeamAgent local runner")
    parser.add_argument("skill", help="実行する Skill 名（例: search）")
    parser.add_argument(
        "input_json",
        help='JSON 文字列の入力（例: \'{"query": "..."}\'）',
    )
    parser.add_argument(
        "--user-id",
        default=None,
        help="トレース用の user_id（省略可）",
    )
    args = parser.parse_args(argv)

    try:
        skill_cls = SkillRegistry.get(args.skill)
    except KeyError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    try:
        input_dict = json.loads(args.input_json)
    except json.JSONDecodeError as e:
        print(f"Error: input_json が不正です: {e}", file=sys.stderr)
        return 2

    input_obj = skill_cls.input_schema.model_validate(input_dict)
    ctx = SkillContext(user_id=args.user_id)
    logger.info("local_runtime_start", skill=args.skill, request_id=ctx.request_id)

    skill = skill_cls()
    output = skill.run(input_obj, ctx)

    print(output.model_dump_json(indent=2))
    logger.info("local_runtime_done", skill=args.skill, request_id=ctx.request_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
