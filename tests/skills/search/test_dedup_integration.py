"""SearchSkill._retrieve への dedup 配線（env-gate）の統合テスト。

- env OFF（既定）: dedup は一切走らず、retrieve は従来挙動のまま（no-op）
- env ON: near-dup 畳み込み + per-doc cap が最終返却の直前に適用される
- env 読み取りは skill 側（__init__）で行うことの確認
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.search.schema import SearchInput
from teamagent.skills.search.skill import SearchSkill

_TEMPLATE_A = "株式会社ベクトル 会社紹介 私たちは100年企業を目指すPR会社です。"
_TEMPLATE_B = "株式会社ベクトル 会社紹介  私たちは100年企業を目指す、PR会社です！"
_BODY = "ユニー様向け第2回提案：縦型動画でZ世代の指名検索を増やす施策。"


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


def _hit(chunk_id: int, content: str, score: float, document_id: int) -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id,
        content=content,
        score=score,
        metadata={"document_id": document_id, "source_uri": f"gdrive://{document_id}"},
    )


def _pgvector(hits: list[SearchHit]) -> MagicMock:
    mock = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock())
    cm.__exit__ = MagicMock(return_value=False)
    mock.connection.return_value = cm
    mock.search_similar_new_schema.return_value = hits
    return mock


def _skill(pg: MagicMock) -> SearchSkill:
    return SearchSkill(
        bedrock=_fake_bedrock(),
        pgvector=pg,
        embedder=_FakeEmbedder(),
        use_new_schema=True,
        use_client_boost=False,
    )


def _hits() -> list[SearchHit]:
    # テンプレ near-dup 2 本（doc1/doc2）+ doc1 本文 3 本
    return [
        _hit(1, _TEMPLATE_A, 0.95, 1),
        _hit(2, _TEMPLATE_B, 0.90, 2),
        _hit(3, _BODY, 0.85, 1),
        _hit(4, _BODY + "（撮影体制）", 0.80, 1),
        _hit(5, _BODY + "（納期と費用）", 0.75, 1),
    ]


def test_env_off_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    # SEARCH_DEDUP_RESULTS 未設定 → dedup は走らず 5 件全て返る（従来挙動）
    monkeypatch.delenv("SEARCH_DEDUP_RESULTS", raising=False)
    pg = _pgvector(_hits())
    out = _skill(pg).run(input=SearchInput(query="ユニーの提案", top_k=10), ctx=SkillContext())
    assert len(out.hits) == 5
    assert {h.chunk_id for h in out.hits} == {1, 2, 3, 4, 5}


def test_env_on_applies_collapse_and_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_DEDUP_RESULTS", "1")
    monkeypatch.setenv("SEARCH_PER_DOC_CAP", "2")
    monkeypatch.setenv("SEARCH_NEARDUP_JACCARD", "0.9")
    pg = _pgvector(_hits())
    out = _skill(pg).run(input=SearchInput(query="ユニーの提案", top_k=10), ctx=SkillContext())
    ids = {h.chunk_id for h in out.hits}
    # collapse: テンプレ chunk2 が chunk1 に畳まれる → 残り {1,3,4,5}
    # cap(doc1, 2): doc1 は score 上位 2 本 → chunk1(0.95), chunk3(0.85)
    assert ids == {1, 3}


def test_env_on_default_cap_is_two(monkeypatch: pytest.MonkeyPatch) -> None:
    # SEARCH_PER_DOC_CAP 未指定なら既定 2
    monkeypatch.setenv("SEARCH_DEDUP_RESULTS", "true")
    monkeypatch.delenv("SEARCH_PER_DOC_CAP", raising=False)
    monkeypatch.delenv("SEARCH_NEARDUP_JACCARD", raising=False)
    pg = _pgvector(_hits())
    out = _skill(pg).run(input=SearchInput(query="ユニーの提案", top_k=10), ctx=SkillContext())
    doc1 = [h for h in out.hits if (h.source_uri or "").endswith("1")]
    assert len(doc1) == 2


def test_env_flags_read_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_DEDUP_RESULTS", "yes")
    monkeypatch.setenv("SEARCH_PER_DOC_CAP", "5")
    monkeypatch.setenv("SEARCH_NEARDUP_JACCARD", "0.7")
    skill = _skill(_pgvector(_hits()))
    assert skill._dedup_results is True
    assert skill._per_doc_cap == 5
    assert skill._neardup_jaccard == 0.7


def test_env_cap_disabled_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    # cap=0 なら per-doc cap は無効。collapse だけ効く（テンプレ 1 本化）。
    monkeypatch.setenv("SEARCH_DEDUP_RESULTS", "1")
    monkeypatch.setenv("SEARCH_PER_DOC_CAP", "0")
    pg = _pgvector(_hits())
    out = _skill(pg).run(input=SearchInput(query="ユニーの提案", top_k=10), ctx=SkillContext())
    # collapse でテンプレ chunk2 のみ落ち、cap 無効 → {1,3,4,5}
    assert {h.chunk_id for h in out.hits} == {1, 3, 4, 5}


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
