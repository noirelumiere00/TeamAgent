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
    try:
        # 2段階しきい値の fallback（既定 0.0 = 無効＝従来挙動）。
        min_relevance_fallback = float(os.environ.get("SEARCH_MIN_RELEVANCE_FALLBACK", "0.0"))
    except ValueError:
        min_relevance_fallback = 0.0
    try:
        # Rerank 候補プール（dense retrieval を何件 rerank に渡すか）。既定 30＝従来挙動。
        # 固有名詞クエリのリコール改善を試すための可変ノブ（SEARCH_RERANK_POOL_SIZE）。
        rerank_pool_size = int(os.environ.get("SEARCH_RERANK_POOL_SIZE", "30"))
    except ValueError:
        rerank_pool_size = 30

    return SearchSkill(
        embedder=LocalE5Embedder(),
        use_contextual=_envflag("USE_CONTEXTUAL"),
        use_new_schema=_envflag("USE_NEW_SCHEMA"),
        use_fb_drive_match=_envflag("USE_FB_DRIVE_MATCH"),
        use_cohere_rerank=_envflag("USE_COHERE_RERANK"),
        rerank_pool_size=rerank_pool_size,
        min_relevance=min_relevance,
        min_relevance_fallback=min_relevance_fallback,
        use_client_boost=_envflag("USE_CLIENT_BOOST"),
        use_aggregation_mode=_envflag("USE_AGGREGATION_MODE"),
        prompt_version=os.environ.get("PROMPT_VERSION", "v2d"),
        summary_max_tokens=summary_max_tokens,
    )


def build_production_tools() -> list[ToolSpec]:
    """本番 Skill を ToolSpec 群へ束ねる（Phase 1-2: search + clientkarte + proposal_draft/review）.

    SearchSkill は 1 インスタンスを共有（embedder 二重ロード回避）。proposal_draft / proposal_review
    は内部で SearchSkill.retrieve_hits を再利用するため **同じ search を注入**する
    （runtime/slack_bot.py と同じ共有方針）。clientkarte は pgvector 直で search 非依存。
    """
    from teamagent.skills.clientkarte.skill import ClientKarteSkill
    from teamagent.skills.proposal.skill import ProposalDraftSkill
    from teamagent.skills.proposal_review.skill import ProposalReviewSkill
    from teamagent.skills.search.skill import SearchSkill

    search = _build_search_skill()  # 共有インスタンス
    specs = [
        ToolSpec(SearchSkill.name, SearchSkill.description, SearchSkill, factory=lambda: search),
        ToolSpec(ClientKarteSkill.name, ClientKarteSkill.description, ClientKarteSkill),
        ToolSpec(
            ProposalDraftSkill.name,
            ProposalDraftSkill.description,
            ProposalDraftSkill,
            factory=lambda: ProposalDraftSkill(search=search),
        ),
        ToolSpec(
            ProposalReviewSkill.name,
            ProposalReviewSkill.description,
            ProposalReviewSkill,
            factory=lambda: ProposalReviewSkill(search=search),
        ),
    ]

    # Phase 6 (6d): Mail 制約ツール。**既定 OFF**（USE_MAIL_TOOLS=1 で opt-in）。
    # 実行時に run() が G1 本人受信箱限定 / G2 本人同意（MAIL_CONSENT_EMAILS）を
    # fail-closed で強制。実受信箱接続（6c）の人間ゲート（同意/DWD/CASA）承認後に有効化。
    if _envflag("USE_MAIL_TOOLS"):
        from teamagent.skills.mail_constraints.skill import MailConstraintsSkill

        specs.append(
            ToolSpec(
                MailConstraintsSkill.name,
                MailConstraintsSkill.description,
                MailConstraintsSkill,
                factory=lambda: MailConstraintsSkill(),
            )
        )

    return specs


__all__ = ["build_production_tools"]
