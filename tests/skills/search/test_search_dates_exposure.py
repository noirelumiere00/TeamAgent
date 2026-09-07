"""便A-3: 資料検索の更新日露出（SearchHitOut.updated_at / title_date / date_basis）。

再現する利用者の失敗（09-02 徳野「最新の更新日からちゃんと出して」）:
検索結果に日付が無く、Aico は「更新日フィールドが無い」と答えるか、本文中の日付を
更新日として書いてしまう（根拠不明の日付）。本テストは

- adapter が詰めた ``updated_at`` がヒットに露出する
- 資料名の日付 ``title_date`` が純関数で付き ``date_basis`` は title_date 優先
- どちらも無いヒットは ``date_basis='none'`` で日付が両方 None（推測しない）
- 要約 LLM に渡す chunk ヘッダに 更新日 / 資料名の日付 が載る（無ければ従来ヘッダ）
- prompt v2d / v2e に「ヘッダの日付だけを書く」契約がある

を、実 DB 0・実 Bedrock 0 のフェイクで固定する。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.prompts.loader import load_prompt
from teamagent.skills.base import SkillContext
from teamagent.skills.search.schema import SearchHitOut, SearchInput
from teamagent.skills.search.skill import SearchSkill


class _FakeEmbedder:
    def embed(self, text: str) -> list[float]:
        return [0.1] * 1024


def _fake_bedrock() -> MagicMock:
    mock = MagicMock()
    mock.converse.return_value = ConverseResponse(
        text="要約",
        usage=TokenUsage(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.0001,
        ),
        model_id="m",
        latency_ms=1,
        stop_reason="end_turn",
    )
    return mock


def _pgvector(hits: list[SearchHit]) -> MagicMock:
    mock = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock())
    cm.__exit__ = MagicMock(return_value=False)
    mock.connection.return_value = cm
    mock.search_similar_new_schema.return_value = hits
    return mock


def _build(pg: MagicMock, bedrock: MagicMock | None = None) -> SearchSkill:
    return SearchSkill(
        bedrock=bedrock or _fake_bedrock(),
        pgvector=pg,
        embedder=_FakeEmbedder(),
        use_new_schema=True,
        use_cohere_rerank=False,
        use_client_boost=False,
    )


def _hit(chunk_id: int, title: str, meta: dict[str, Any] | None = None) -> SearchHit:
    base: dict[str, Any] = {
        "source_type": "gdrive",
        "source_uri": f"gdrive://FILE{chunk_id}",
        "document_id": f"doc-{chunk_id}",
        "title": title,
    }
    if meta:
        base.update(meta)
    return SearchHit(chunk_id=chunk_id, content=f"本文 {chunk_id}", score=0.9, metadata=base)


# ── SearchHitOut への露出 ─────────────────────────────────────────────


def test_updated_at_from_adapter_lands_in_hit() -> None:
    """adapter が詰めた updated_at（modified_at・JST）がヒットに露出し basis=modified_at。"""
    pg = _pgvector(
        [_hit(1, "提案書.pdf", {"updated_at": "2026-08-28", "date_basis": "modified_at"})]
    )
    out = _build(pg).run(
        input=SearchInput(query="提案書", include_answer=False), ctx=SkillContext()
    )
    assert len(out.hits) == 1
    h = out.hits[0]
    assert h.updated_at == "2026-08-28"
    assert h.title_date is None
    assert h.date_basis == "modified_at"


def test_title_date_takes_priority_over_modified_at() -> None:
    """資料名に日付があれば title_date を返し basis=title_date（updated_at も併記）。

    Drive の modifiedTime（誰かが開いて保存した日）より、資料名の日付が提案日に近い。
    """
    pg = _pgvector(
        [
            _hit(
                1,
                "【NewsTV】SABON様_ご説明資料_20260227.pptx",
                {"updated_at": "2026-09-01", "date_basis": "modified_at"},
            )
        ]
    )
    out = _build(pg).run(input=SearchInput(query="SABON", include_answer=False), ctx=SkillContext())
    h = out.hits[0]
    assert h.title_date == "2026-02-27"
    assert h.updated_at == "2026-09-01"
    assert h.date_basis == "title_date"


def test_no_evidence_yields_none_basis_and_blank_dates() -> None:
    """modified_at NULL かつ資料名に日付が無い → basis=none・日付は両方 None（推測しない）。"""
    pg = _pgvector([_hit(1, "提案書v2.pdf")])
    out = _build(pg).run(
        input=SearchInput(query="提案書", include_answer=False), ctx=SkillContext()
    )
    h = out.hits[0]
    assert h.date_basis == "none"
    assert h.updated_at is None
    assert h.title_date is None


def test_title_date_falls_back_to_file_name_meta() -> None:
    """title に日付が無くても file_name（旧スキーマ meta）から拾う。"""
    pg = _pgvector([_hit(1, "飲料提案", {"file_name": "proposal_20240315_drink.pdf"})])
    out = _build(pg).run(input=SearchInput(query="飲料", include_answer=False), ctx=SkillContext())
    h = out.hits[0]
    assert h.title_date == "2024-03-15"
    assert h.date_basis == "title_date"


def test_dates_are_serialized_for_mcp_consumers() -> None:
    """model_dump（MCP / Slack / Web が受け取る JSON）に 3 フィールドが載る。"""
    pg = _pgvector(
        [
            _hit(
                1,
                "20250820花王様限定.pdf",
                {"updated_at": "2026-01-10", "date_basis": "modified_at"},
            )
        ]
    )
    out = _build(pg).run(input=SearchInput(query="花王", include_answer=False), ctx=SkillContext())
    dumped = out.hits[0].model_dump()
    assert dumped["updated_at"] == "2026-01-10"
    assert dumped["title_date"] == "2025-08-20"
    assert dumped["date_basis"] == "title_date"


# ── スキーマの整合契約 ────────────────────────────────────────────────


def test_schema_blanks_dates_when_basis_none() -> None:
    """updated_at / title_date が無いのに basis を与えても none に正規化される。"""
    h = SearchHitOut(chunk_id=1, content="x", score=0.5, date_basis="modified_at")
    assert h.date_basis == "none"
    assert h.updated_at is None
    assert h.title_date is None


def test_schema_derives_basis_from_values() -> None:
    """basis 未指定でも値から根拠が決まる（title_date 優先）。"""
    h = SearchHitOut(chunk_id=1, content="x", score=0.5, updated_at="2026-01-01")
    assert h.date_basis == "modified_at"
    h2 = SearchHitOut(
        chunk_id=1, content="x", score=0.5, updated_at="2026-01-01", title_date="2025-12-01"
    )
    assert h2.date_basis == "title_date"


def test_schema_defaults_keep_backward_compat() -> None:
    """既存の SearchHitOut(...) 構築（日付未指定）は none / None のまま。"""
    h = SearchHitOut(chunk_id=1, content="x", score=0.5)
    assert h.date_basis == "none"
    assert h.updated_at is None
    assert h.title_date is None


# ── 要約 LLM のコンテキスト（chunk ヘッダ） ───────────────────────────


def _converse_text(bedrock: MagicMock) -> str:
    msgs = bedrock.converse.call_args.kwargs["messages"]
    text = msgs[0]["content"][0]["text"]
    assert isinstance(text, str)
    return text


def test_summarize_header_carries_updated_at_and_title_date() -> None:
    """要約 LLM に渡す参考資料ヘッダに 更新日 と 資料名の日付 が入る。"""
    bedrock = _fake_bedrock()
    pg = _pgvector(
        [
            _hit(
                7,
                "【NewsTV】SABON様_ご説明資料_20260227.pptx",
                {"updated_at": "2026-09-01", "date_basis": "modified_at"},
            )
        ]
    )
    _build(pg, bedrock).run(input=SearchInput(query="SABON"), ctx=SkillContext())
    text = _converse_text(bedrock)
    assert "[chunk_id: 7, score: 0.900, 更新日: 2026-09-01, 資料名の日付: 2026-02-27]" in text


def test_summarize_header_has_no_date_when_no_evidence() -> None:
    """日付根拠の無いヒットは従来ヘッダのまま（LLM に空の日付欄を見せない）。"""
    bedrock = _fake_bedrock()
    pg = _pgvector([_hit(7, "提案書v2.pdf")])
    _build(pg, bedrock).run(input=SearchInput(query="提案書"), ctx=SkillContext())
    text = _converse_text(bedrock)
    assert "[chunk_id: 7, score: 0.900]" in text
    assert "更新日" not in text.split("# 参考資料", 1)[1]


def test_summarize_related_drive_header_carries_updated_at() -> None:
    """関連 Drive 資料（is_related_drive）のヘッダにも更新日が載る。"""
    bedrock = _fake_bedrock()
    related = SearchHit(
        chunk_id=99,
        content="関連資料本文",
        score=1.0,
        metadata={
            "source_type": "gdrive",
            "title": "関連提案書.pdf",
            "is_related_drive": True,
            "updated_at": "2026-05-05",
            "date_basis": "modified_at",
        },
    )
    pg = _pgvector([_hit(7, "主ヒット.pdf"), related])
    _build(pg, bedrock).run(input=SearchInput(query="q"), ctx=SkillContext())
    text = _converse_text(bedrock)
    assert "[chunk_id: 99, 更新日: 2026-05-05] 関連提案書.pdf" in text


# ── prompt 契約 ───────────────────────────────────────────────────────


def test_prompts_instruct_dates_only_from_header() -> None:
    """v2d / v2e ともに「日付はヘッダの更新日・資料名の日付だけ」「無ければ付けない」を持つ。"""
    for version in ("v2d", "v2e"):
        system = load_prompt("search", version, "system")
        assert "「更新日」「資料名の日付」だけを書く" in system, version
        assert "日付が無い資料には日付を付けない" in system, version
