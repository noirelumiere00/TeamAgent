"""ジョブごとの HERMES_HOME に置く config.yaml の生成と検査。

YAML は JSON の上位集合なので、JSON で書いて Hermes の YAML ローダにそのまま読ませる
（依存を増やさない）。学習係に要らない機能（裏の review・skill の自動作成・記憶の通知・
書き込みの承認待ち）はすべて止める。
検査に通らない config ではジョブを動かさない（fail-closed）。子プロセスの中でも、
Hermes が実際に読んだ有効値を同じ関数で検査する。
"""

from __future__ import annotations

import json
from typing import Any, Final

from .schema import MEMORY_CHAR_LIMIT, USER_CHAR_LIMIT

# 裏の review を実質止めるための大きな間隔（review 自体も enabled=false にする。二重に止める）
NEVER: Final = 1_000_000
# Claude Haiku 4.5 の context 長。明示しないと Hermes は初期化のたびに約 700 万字のダミー文を
# Bedrock に送って探る（Hermes の agent/bedrock_adapter.py probe_bedrock_context_length）
CONTEXT_LENGTH: Final = 200_000


def bedrock_base_url(region: str) -> str:
    return f"https://bedrock-runtime.{region}.amazonaws.com"


def build_config(*, model: str, region: str) -> dict[str, Any]:
    if not model or not region:
        raise ValueError("model と region は必須")
    return {
        "model": {
            "default": model,
            "provider": "bedrock",
            # context_length を効かせるには、実行時の経路（base_url）と一致している必要がある
            "base_url": bedrock_base_url(region),
            "context_length": CONTEXT_LENGTH,
            # IAM は InvokeModel だけを許すので、
            # ストリーミング（InvokeModelWithResponseStream）は使わない
            "streaming": False,
        },
        "bedrock": {"region": region},
        "memory": {
            "memory_enabled": True,
            "user_profile_enabled": True,
            # 承認待ちにすると使い捨て HOME と一緒に消えるので、学習係では即時反映にする
            # （保存してよいかは MCP 側の guard が決める）
            "write_approval": False,
            "memory_char_limit": MEMORY_CHAR_LIMIT,
            "user_char_limit": USER_CHAR_LIMIT,
            "nudge_interval": NEVER,
        },
        "display": {"memory_notifications": "off"},
        "auxiliary": {"background_review": {"enabled": False}},
        "skills": {"write_approval": True, "creation_nudge_interval": NEVER},
        "security": {"allow_lazy_installs": False},
        "curator": {"enabled": False},
    }


def render_config(config: dict[str, Any]) -> str:
    return json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def lint_config(config: Any) -> list[str]:
    """学習係の前提から外れている箇所を返す。空なら合格。"""
    problems: list[str] = []

    def get(path: str) -> Any:
        node: Any = config
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    expected: dict[str, Any] = {
        "model.provider": "bedrock",
        "model.context_length": CONTEXT_LENGTH,
        "model.streaming": False,
        "memory.memory_enabled": True,
        "memory.user_profile_enabled": True,
        "memory.write_approval": False,
        "memory.memory_char_limit": MEMORY_CHAR_LIMIT,
        "memory.user_char_limit": USER_CHAR_LIMIT,
        "display.memory_notifications": "off",
        "auxiliary.background_review.enabled": False,
        "skills.write_approval": True,
        "security.allow_lazy_installs": False,
        "curator.enabled": False,
    }
    for path, want in expected.items():
        if get(path) != want:
            problems.append(path)
    for path in ("memory.nudge_interval", "skills.creation_nudge_interval"):
        value = get(path)
        if not isinstance(value, int) or value < NEVER:
            problems.append(path)
    for path in ("model.default", "bedrock.region"):
        if not isinstance(get(path), str) or not get(path):
            problems.append(path)
    region = get("bedrock.region")
    if not isinstance(region, str) or get("model.base_url") != bedrock_base_url(region):
        problems.append("model.base_url")
    # 外部 memory provider は使わない（保存先の資格情報を Hermes に持たせないため）
    if get("memory.provider"):
        problems.append("memory.provider")
    return problems
