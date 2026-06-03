"""本番 Skill を SDK オーケストレーターのツールへ束ねる工場（Phase 1）.

fixture → 実 Skill の差し替えは `ToolSpec.factory` に本物の Skill インスタンスを渡すだけ。
重い依存（LocalE5Embedder / boto3 / psycopg）は **関数内 遅延 import** にしてあるので、
本モジュールの import 自体は軽量（heavy deps が無い環境でも import できる）。
実構築は `build_production_tools()` を呼んだ時に初めて起こる。

⚠️ 実行要件: 実 Skill の依存（pgvector(RDS)+SSMトンネル / Bedrock / LocalE5Embedder）が必要。
   env フラグ解決は runtime/slack_bot.py:get_search_skill と一致させている（将来は共通化したい）。
"""

from __future__ import annotations

import os
from typing import Any

from .tools import ToolSpec


def _envflag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes")


def _build_search_skill() -> Any:
    """実 SearchSkill を本番 runtime と同じ env フラグで構築（依存は内部で遅延生成）.

    参照: runtime/slack_bot.py:get_search_skill（同じフラグ・既定値に揃える）。
    """
    from teamagent.adapters.embeddings_client import LocalE5Embedder
    from teamagent.skills.search.skill import SearchSkill

    try:
        summary_max_tokens = int(os.environ.get("SEARCH_MAX_TOKENS", "800"))
    except ValueError:
        summary_max_tokens = 800
    try:
        min_relevance = float(os.environ.get("SEARCH_MIN_RELEVANCE", "0.0"))
    except ValueError:
        min_relevance = 0.0

    return SearchSkill(
        embedder=LocalE5Embedder(),
        use_contextual=_envflag("USE_CONTEXTUAL"),
        use_new_schema=_envflag("USE_NEW_SCHEMA"),
        use_fb_drive_match=_envflag("USE_FB_DRIVE_MATCH"),
        use_cohere_rerank=_envflag("USE_COHERE_RERANK"),
        min_relevance=min_relevance,
        use_aggregation_mode=_envflag("USE_AGGREGATION_MODE"),
        prompt_version=os.environ.get("PROMPT_VERSION", "v2d"),
        summary_max_tokens=summary_max_tokens,
    )


def build_production_tools() -> list[ToolSpec]:
    """本番 Skill を ToolSpec 群へ束ねる（Phase 1 は `search` のみ）.

    SearchSkill は 1 インスタンスを共有して factory から返す（embedder 二重ロード回避）。
    Phase 2 で clientkarte / proposal_draft / proposal_review を追加予定
    （proposal_* には同じ search インスタンスを注入する）。
    """
    from teamagent.skills.search.skill import SearchSkill

    search = _build_search_skill()
    return [
        ToolSpec(
            name=SearchSkill.name,
            description=SearchSkill.description,
            skill_cls=SearchSkill,
            factory=lambda: search,
        ),
    ]


__all__ = ["build_production_tools"]
