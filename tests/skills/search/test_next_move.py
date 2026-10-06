"""search の結果に添える次の一手（suggested_next・1 個だけ）のテスト。

- 受け皿のツールが今 ON のものだけを勧める（出来ない約束をしない）。
- 本文に「📎 実ファイルをお送りしますか？」が付いていれば、それと同じ一手にそろえる。
- ツールの説明（description＝固定トークン）は増やさない。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.bedrock_client import (
    ConverseResponse,
    RerankResponse,
    RerankResult,
    TokenUsage,
)
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import SkillContext, SkillRegistry
from teamagent.skills.search.composite import COMPOSITE_ENV
from teamagent.skills.search.next_move import (
    DELIVER_NEXT,
    DRAFT_NEXT,
    KARTE_NEXT,
    SLACK_NEXT,
    WEB_NEXT,
    suggest_next_move,
)
from teamagent.skills.search.schema import SearchInput, SearchOutput
from teamagent.skills.search.skill import SearchSkill
from teamagent.skills.search.two_stage import TWO_STAGE_CTX_KEY
from tests.skills.slack_search.test_slack_search import _ReaderFactory, _slack_client, _Store

ME = "me@vectorinc.co.jp"
# 変更前の search の description（ツール定義＝毎リクエストの固定トークン）。増やさない。
_DESCRIPTION_CHARS_BEFORE = 187


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "SUGGEST_NEXT_STEP",
        "USE_SLACK_SEARCH_TOOL",
        "USE_WEB_RESEARCH_TOOL",
        "WEB_RESEARCH_ALLOWED_EMAILS",
        "USE_KNOWLEDGE_DELIVER",
        COMPOSITE_ENV,
        "SEARCH_NOT_FOUND_ANSWER",
    ):
        monkeypatch.delenv(name, raising=False)


def _call(**kw: Any) -> str | None:
    base: dict[str, Any] = {
        "query": "何か",
        "found": True,
        "slack_status": None,
        "slack_shown": 0,
        "delivery_offered": False,
        "query_client": None,
        "requester": ME,
    }
    base.update(kw)
    return suggest_next_move(**base)


# ── 規則（純関数）───────────────────────────────────────────────────────────


def test_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUGGEST_NEXT_STEP", "false")
    assert _call(delivery_offered=True) is None


def test_delivery_offer_in_answer_is_the_one_next_move() -> None:
    assert _call(delivery_offered=True, query_client="花王") == DELIVER_NEXT


def test_not_found_without_slack_suggests_slack_search(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_SLACK_SEARCH_TOOL", "1")
    monkeypatch.setenv("USE_WEB_RESEARCH_TOOL", "1")
    assert _call(found=False, slack_status=None) == SLACK_NEXT
    assert _call(found=False, slack_status="error") == SLACK_NEXT


def test_not_found_anywhere_suggests_web_research(monkeypatch: pytest.MonkeyPatch) -> None:
    """金庫にも Slack にも無い → 公開情報（web_research）を調べるか聞く。"""
    monkeypatch.setenv("USE_SLACK_SEARCH_TOOL", "1")
    monkeypatch.setenv("USE_WEB_RESEARCH_TOOL", "1")
    assert _call(found=False, slack_status="ok", slack_shown=0) == WEB_NEXT
    assert _call(found=False, slack_status="not_connected") == WEB_NEXT


def test_web_research_respects_rollout_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_WEB_RESEARCH_TOOL", "1")
    monkeypatch.setenv("WEB_RESEARCH_ALLOWED_EMAILS", "someone@vectorinc.co.jp")
    assert _call(found=False, slack_status="ok") is None


def test_disabled_tools_are_never_promised() -> None:
    assert _call(found=False, slack_status=None) is None
    assert _call(found=False, slack_status="ok") is None


def test_slack_answered_needs_no_next_move(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_WEB_RESEARCH_TOOL", "1")
    assert _call(found=False, slack_status="ok", slack_shown=2) is None


def test_found_with_client_suggests_karte() -> None:
    assert _call(query_client="日本ガイシ") == KARTE_NEXT.format(client="日本ガイシ")


def test_found_case_query_suggests_draft() -> None:
    assert _call(query="UGCのTTOで成功した事例") == DRAFT_NEXT
    assert _call(query="値引き規定") is None


def test_every_suggestion_names_one_registered_tool() -> None:
    from teamagent.skills.clientkarte.skill import ClientKarteSkill
    from teamagent.skills.knowledge_deliver.skill import KnowledgeDeliverSkill
    from teamagent.skills.proposal.skill import ProposalDraftSkill
    from teamagent.skills.slack_search.skill import SlackSearchSkill
    from teamagent.skills.web_research.skill import WebResearchSkill

    real = {
        cls.name
        for cls in (
            ClientKarteSkill,
            KnowledgeDeliverSkill,
            ProposalDraftSkill,
            SlackSearchSkill,
            WebResearchSkill,
        )
    }
    registered = set(SkillRegistry.list_all())
    for text in (DELIVER_NEXT, SLACK_NEXT, WEB_NEXT, KARTE_NEXT, DRAFT_NEXT):
        tool = text.split(":", 1)[0]
        assert tool in real
        if registered:  # 他テストが registry を空にしていなければ、登録名でも確かめる
            assert tool in registered
        assert "\n" not in text and len(text) <= 90


def test_tool_description_is_not_longer() -> None:
    assert len(SearchSkill.description) <= _DESCRIPTION_CHARS_BEFORE


# ── skill 配線 ──────────────────────────────────────────────────────────────


def _converse() -> ConverseResponse:
    return ConverseResponse(
        text="要約",
        usage=TokenUsage(
            input_tokens=1,
            output_tokens=1,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.001,
        ),
        model_id="m",
        latency_ms=1,
        stop_reason="end_turn",
    )


class _Embedder:
    def embed(self, text: str) -> list[float]:
        return [0.1] * 8


def _skill(hits: list[SearchHit], score: float, vocab: list[str]) -> SearchSkill:
    pg = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock())
    cm.__exit__ = MagicMock(return_value=False)
    pg.connection.return_value = cm
    pg.search_similar_new_schema.return_value = hits
    pg.search_drive_by_client_names.return_value = []
    pg.list_client_names.return_value = vocab
    pg.resolve_file_urls_by_titles.return_value = {}
    bedrock = MagicMock()
    bedrock.converse.return_value = _converse()
    bedrock.rerank.side_effect = lambda **kw: RerankResponse(
        results=[RerankResult(index=i, relevance_score=score) for i in range(len(kw["documents"]))],
        model_arn="arn",
        latency_ms=1,
        query_count=1,
    )
    return SearchSkill(
        bedrock=bedrock,
        pgvector=pg,
        embedder=_Embedder(),
        use_new_schema=True,
        use_cohere_rerank=True,
        drive_pool_floor=0,
        campaign_pool_floor=0,
        deal_pool_floor=0,
        min_relevance=0.4,
        min_relevance_fallback=0.05,
        slack_store=_Store(),
        slack_reader_factory=_ReaderFactory(_slack_client([])),
    )


def _hit(**meta: Any) -> SearchHit:
    return SearchHit(chunk_id=1, content="日本ガイシ ケイパ提案", score=0.9, metadata=meta)


def test_run_not_found_everywhere_carries_web_research(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(COMPOSITE_ENV, "1")
    monkeypatch.setenv("USE_WEB_RESEARCH_TOOL", "1")
    monkeypatch.setenv("USE_SLACK_SEARCH_TOOL", "1")
    out = _skill([_hit(title="花王")], 0.1, []).run(
        SearchInput(query="2030年のメタバース広告戦略"),
        SkillContext(metadata={TWO_STAGE_CTX_KEY: True, "user_email": ME, "channel_id": "D1"}),
    )
    assert out.found is False and out.slack_status == "ok"
    assert out.suggested_next == WEB_NEXT
    assert out.answer.count("？") <= 1  # 本文に提案を重ねない（次の一手は結果の欄に 1 個）


def test_run_found_named_client_carries_karte() -> None:
    out = _skill([_hit(client_name="日本ガイシ")], 0.86, ["日本ガイシ"]).run(
        SearchInput(query="日本ガイシのケイパ提案"), SkillContext(metadata={"user_email": ME})
    )
    assert out.found is True
    assert out.suggested_next == KARTE_NEXT.format(client="日本ガイシ")
    assert json.loads(json.dumps(out.model_dump()))["suggested_next"].startswith("clientkarte:")


def test_run_without_suggestion_omits_the_key() -> None:
    out = _skill([_hit()], 0.86, []).run(
        SearchInput(query="値引き規定はどこ"), SkillContext(metadata={"user_email": ME})
    )
    assert out.suggested_next is None
    assert "suggested_next" not in out.model_dump()


def test_schema_omits_none_suggestion() -> None:
    assert "suggested_next" not in SearchOutput(answer="", total_cost_usd=0).model_dump()
