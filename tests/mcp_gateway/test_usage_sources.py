"""usage_events.metadata の出典 ID・回答の長さ（本文・URL は残さない・記録を落とさない）。"""

from __future__ import annotations

import json
from typing import Any

from teamagent.mcp_gateway.usage_sources import MAX_SOURCES, source_usage_metadata
from teamagent.skills.clientkarte.schema import ClientKarteOutput, KarteEvent
from teamagent.skills.knowledge_deliver.schema import KnowledgeDeliverOutput, KnowledgeRef
from teamagent.skills.search.schema import SearchHitOut, SearchOutput

_SECRET_BODY = "社外秘の本文スニペット"


def _hit(chunk_id: int, **kwargs: Any) -> SearchHitOut:
    return SearchHitOut(chunk_id=chunk_id, content=_SECRET_BODY, score=0.9, **kwargs)


def test_search_result_records_top5_source_ids_and_answer_chars() -> None:
    hits = [
        _hit(
            1,
            source_type="gdrive",
            source_uri="gdrive://1AbCdEfGhIjKlMn",
            url="https://x.example/a",
        ),
        _hit(2, source_type="slack", source_uri="slack://C0123/1700000000.000100"),
        _hit(3, source_type="pdf", source_uri="file:///Users/someone/提案書.pdf"),  # パスは捨てる
        _hit(4, source_type="gsheets"),  # ID が chunk しか無い
        _hit(5, source_type="gdrive", source_uri="gdrive://1AbCdEfGhIjKlMn"),  # 1 件目と同じ資料
        _hit(6, source_type="gdrive", source_uri="gdrive://ZZZ999"),
        _hit(7, source_type="gdrive", source_uri="gdrive://YYY888"),  # 上位 5 件の外
    ]
    output = SearchOutput(answer="回答です（引用付き）", hits=hits, total_cost_usd=0.01)

    metadata = source_usage_metadata("search", output)

    assert metadata["answer_chars"] == len("回答です（引用付き）")
    assert metadata["source_ids"] == [
        {"external_id": "1AbCdEfGhIjKlMn", "source_type": "gdrive"},
        {"external_id": "C0123/1700000000.000100", "source_type": "slack"},
        {"chunk_id": "3", "source_type": "pdf"},
        {"chunk_id": "4", "source_type": "gsheets"},
        {"external_id": "ZZZ999", "source_type": "gdrive"},
    ]
    assert len(metadata["source_ids"]) == MAX_SOURCES
    dumped = json.dumps(metadata, ensure_ascii=False)
    for forbidden in (_SECRET_BODY, "http", "file:", "/Users/", "提案書", "回答です"):
        assert forbidden not in dumped


def test_explicit_doc_id_and_external_id_win_over_derived_ids() -> None:
    output = {
        "answer": "abc",
        "hits": [
            {"doc_id": "d-1", "external_id": "e-1", "chunk_id": 9, "source_type": "gdrive"},
            {"external_id": "e-2", "source_uri": "gdrive://other", "chunk_id": 10},
        ],
    }
    assert source_usage_metadata("search", output) == {
        "source_ids": [
            {"doc_id": "d-1", "source_type": "gdrive"},
            {"external_id": "e-2"},
        ],
        "answer_chars": 3,
    }


def test_knowledge_deliver_records_drive_file_ids_but_not_urls() -> None:
    output = KnowledgeDeliverOutput(
        answer="資料の要約",
        references=[
            KnowledgeRef(title="A社 提案書", url="https://drive.google.com/file/d/FILEID_A/view"),
            KnowledgeRef(title="B社", url="https://docs.google.com/presentation/d/FILEID_B/edit"),
            KnowledgeRef(title="外部", url="https://example.com/d/NOT_DRIVE"),
        ],
    )

    metadata = source_usage_metadata("knowledge_deliver", output)

    assert metadata == {
        "source_ids": [
            {"external_id": "FILEID_A", "source_type": "gdrive"},
            {"external_id": "FILEID_B", "source_type": "gdrive"},
        ],
        "answer_chars": len("資料の要約"),
    }
    assert "提案書" not in json.dumps(metadata, ensure_ascii=False)


def test_clientkarte_records_event_chunk_ids() -> None:
    output = ClientKarteOutput(
        client_name="A社",
        answer="温度感は上昇",
        events=[
            KarteEvent(chunk_id=11, summary=_SECRET_BODY, url="https://slack.example/p1"),
            KarteEvent(chunk_id=12, summary=_SECRET_BODY),
        ],
        event_count=2,
        total_cost_usd=0.0,
    )
    assert source_usage_metadata("clientkarte", output) == {
        "source_ids": [{"chunk_id": "11"}, {"chunk_id": "12"}],
        "answer_chars": len("温度感は上昇"),
    }


def test_no_hits_adds_nothing() -> None:
    output = SearchOutput(answer="該当する資料は見つかりませんでした", hits=[], total_cost_usd=0.0)
    assert source_usage_metadata("search", output) == {}
    # ID が 1 つも取れないヒットだけでも足さない
    assert source_usage_metadata("search", {"answer": "x", "hits": [{"content": "本文"}]}) == {}


def test_other_tools_add_nothing() -> None:
    assert source_usage_metadata("mail_summary", {"answer": "x", "hits": [{"chunk_id": 1}]}) == {}


class _Exploding:
    @property
    def hits(self) -> list[Any]:
        raise RuntimeError("lazy load failed")


def test_unexpected_shapes_never_raise() -> None:
    for weird in (
        None,
        "plain text",
        42,
        {"hits": "not a list"},
        {"hits": None, "answer": "x"},
        {"hits": [None, 1, "s", [], {"chunk_id": True}, {"chunk_id": {"nested": 1}}]},
        {"hits": [{"source_uri": "gdrive://"}, {"source_uri": "://broken"}]},
        {"references": [{"url": 123}]},
        _Exploding(),
    ):
        assert source_usage_metadata("search", weird) == {}
        assert source_usage_metadata("knowledge_deliver", weird) == {}


def test_answer_not_string_is_omitted_but_sources_kept() -> None:
    assert source_usage_metadata("search", {"answer": None, "hits": [{"chunk_id": 1}]}) == {
        "source_ids": [{"chunk_id": "1"}]
    }


def test_long_ids_are_capped() -> None:
    metadata = source_usage_metadata("search", {"hits": [{"external_id": "x" * 1000}]})
    assert len(metadata["source_ids"][0]["external_id"]) == 200
