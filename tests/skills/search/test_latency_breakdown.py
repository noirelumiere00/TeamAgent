"""search_latency_breakdown（区間別レイテンシの 1 行ログ）のテスト。

「残り約1.9秒が分解不能」だった状態を潰すための計器。挙動は変えず、
**本文・クエリ原文をログに出さない**（G8）ことまで固定する。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from teamagent.adapters.bedrock_client import (
    ConverseResponse,
    RerankResponse,
    RerankResult,
    TokenUsage,
)
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.search.schema import SearchInput
from teamagent.skills.search.skill import SearchSkill

_SECRET_QUERY = "花王の新製品リリースの社外秘スケジュール"

_BREAKDOWN_KEYS = {
    "embed_ms",
    "retrieve_ms",
    "rerank_ms",
    "resolve_urls_ms",
    "converse_ms",
    "total_ms",
    "hit_count",
    "deferred",
}


@pytest.fixture
def fake_bedrock() -> MagicMock:
    mock = MagicMock()
    mock.converse.return_value = ConverseResponse(
        text="要約テキスト",
        usage=TokenUsage(
            input_tokens=200,
            output_tokens=80,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.0018,
        ),
        model_id="jp.anthropic.claude-haiku-4-5",
        latency_ms=300,
        stop_reason="end_turn",
    )
    return mock


@pytest.fixture
def fake_pgvector() -> MagicMock:
    mock = MagicMock()
    cm_mock = MagicMock()
    cm_mock.__enter__ = MagicMock(return_value=MagicMock())
    cm_mock.__exit__ = MagicMock(return_value=False)
    mock.connection.return_value = cm_mock
    mock.search_similar.return_value = [
        SearchHit(chunk_id=1, content="本文", score=0.91, metadata={"title": "資料"}),
    ]
    mock.search_similar_new_schema.return_value = [
        SearchHit(chunk_id=1, content="本文", score=0.91, metadata={"title": "資料"}),
    ]
    return mock


class FakeEmbedder:
    def embed(self, text: str) -> list[float]:
        return [0.1] * 1024


def _breakdown(logs: list[dict[str, Any]]) -> dict[str, Any]:
    events = [e for e in logs if e.get("event") == "search_latency_breakdown"]
    assert len(events) == 1, f"search_latency_breakdown が 1 行出ていない: {len(events)}"
    return events[0]


def test_breakdown_is_logged_with_all_sections(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock
) -> None:
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector,
        embedder=FakeEmbedder(),
        target_table="proposal_chunks",
    )
    with capture_logs() as logs:
        skill.run(input=SearchInput(query=_SECRET_QUERY), ctx=SkillContext())

    event = _breakdown(logs)
    assert _BREAKDOWN_KEYS <= set(event)
    assert event["hit_count"] == 1
    assert event["deferred"] is False
    # converse を通った回なので converse_ms は計測されている（ms 丸めで 0 もあり得るためキー存在で固定）
    assert isinstance(event["converse_ms"], int)
    assert event["total_ms"] >= 0
    # search_skill_done にも総 latency が乗る（ダッシュボードの空カラム解消）
    done = [e for e in logs if e.get("event") == "search_skill_done"]
    assert done and isinstance(done[0]["latency_ms"], int)


def test_breakdown_never_logs_query_or_content(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock
) -> None:
    """G8: クエリ原文・チャンク本文をログに出さない。"""
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector,
        embedder=FakeEmbedder(),
        target_table="proposal_chunks",
    )
    with capture_logs() as logs:
        skill.run(input=SearchInput(query=_SECRET_QUERY), ctx=SkillContext())

    blob = repr(logs)
    assert _SECRET_QUERY not in blob
    assert "本文" not in blob


def test_rerank_ms_is_measured_when_rerank_runs(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock
) -> None:
    """rerank 区間が独立して見える（retrieve_ms に埋もれさせない）。"""
    fake_bedrock.rerank.return_value = RerankResponse(
        results=[RerankResult(index=0, relevance_score=0.8)],
        model_arn="arn:aws:bedrock:ap-northeast-1::foundation-model/cohere.rerank-v3-5:0",
        latency_ms=120,
        query_count=1,
    )
    skill = SearchSkill(
        bedrock=fake_bedrock,
        pgvector=fake_pgvector,
        embedder=FakeEmbedder(),
        target_table="chunks",
        use_new_schema=True,
        use_cohere_rerank=True,
    )
    with capture_logs() as logs:
        skill.run(input=SearchInput(query=_SECRET_QUERY), ctx=SkillContext())

    event = _breakdown(logs)
    fake_bedrock.rerank.assert_called_once()
    assert "rerank_ms" in event
    assert event["retrieve_ms"] >= event["rerank_ms"]  # retrieve は rerank を内包する
