"""SearchSkill._retrieve への重複資料（duplicate）除外（env-gate）の統合テスト。

契約:
- env OFF（既定 / 未設定）: search_similar_new_schema は exclude_duplicates=False で呼ばれる
  ＝現行と完全一致（重複除外は一切起きない）
- env ON（DOC_DEDUP_EXCLUDE_SEARCH=1/true/yes）: exclude_duplicates=True で呼ばれる
- env 読み取りは skill 側（__init__）で 1 回（boilerplate / dedup の env と同じ流儀）
- 渡すだけ。重複判定（metadata.suppressed=true の WHERE 除外）は pgvector 側（SQL）の責務。

呼び出し引数は monkeypatch（MagicMock の call_args）で捕捉する。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.search.schema import SearchInput
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
    return [
        _hit(1, "本文 A", 0.95, 1),
        _hit(2, "本文 B", 0.90, 2),
    ]


def _exclude_kwarg(pg: MagicMock) -> bool:
    """最初の search_similar_new_schema 呼び出しに渡った exclude_duplicates を返す。

    OFF 時は False（明示渡し）であることを確認するため、kwargs から取り出す。
    """
    call = pg.search_similar_new_schema.call_args
    assert call is not None, "search_similar_new_schema が呼ばれていない"
    return bool(call.kwargs.get("exclude_duplicates", False))


def test_env_off_passes_false(monkeypatch: pytest.MonkeyPatch) -> None:
    # 未設定 → exclude_duplicates=False（現行と完全一致）
    monkeypatch.delenv("DOC_DEDUP_EXCLUDE_SEARCH", raising=False)
    pg = _pgvector(_hits())
    _skill(pg).run(input=SearchInput(query="ユニーの提案", top_k=5), ctx=SkillContext())
    assert _exclude_kwarg(pg) is False
    # 全呼び出しが False であること（fallback 経路含め一貫）
    for call in pg.search_similar_new_schema.call_args_list:
        assert call.kwargs.get("exclude_duplicates", False) is False


def test_env_on_passes_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOC_DEDUP_EXCLUDE_SEARCH", "1")
    pg = _pgvector(_hits())
    _skill(pg).run(input=SearchInput(query="ユニーの提案", top_k=5), ctx=SkillContext())
    assert _exclude_kwarg(pg) is True
    for call in pg.search_similar_new_schema.call_args_list:
        assert call.kwargs.get("exclude_duplicates") is True


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "Yes"])
def test_env_truthy_variants_enable(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("DOC_DEDUP_EXCLUDE_SEARCH", raw)
    pg = _pgvector(_hits())
    _skill(pg).run(input=SearchInput(query="提案", top_k=5), ctx=SkillContext())
    assert _exclude_kwarg(pg) is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", ""])
def test_env_falsy_variants_disable(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("DOC_DEDUP_EXCLUDE_SEARCH", raw)
    pg = _pgvector(_hits())
    _skill(pg).run(input=SearchInput(query="提案", top_k=5), ctx=SkillContext())
    assert _exclude_kwarg(pg) is False


def test_env_read_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    # __init__ で 1 回読んで self に保持（boilerplate の env と同じ流儀）
    monkeypatch.setenv("DOC_DEDUP_EXCLUDE_SEARCH", "yes")
    skill = _skill(_pgvector(_hits()))
    assert skill._exclude_duplicates is True
    # 構築後に env を変えても保持値は不変（再読込しない）
    monkeypatch.setenv("DOC_DEDUP_EXCLUDE_SEARCH", "0")
    assert skill._exclude_duplicates is True


def test_env_default_construction_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOC_DEDUP_EXCLUDE_SEARCH", raising=False)
    skill = _skill(_pgvector(_hits()))
    assert skill._exclude_duplicates is False


def test_fallback_call_also_carries_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # _pool_search の fallback（filter_industry 指定で 0 件 → 業界外して再検索）経路にも
    # flag が伝播すること。1 回目 0 件 → 2 回目ヒットで両方とも True。
    monkeypatch.setenv("DOC_DEDUP_EXCLUDE_SEARCH", "true")
    pg = _pgvector(_hits())
    pg.search_similar_new_schema.side_effect = [[], _hits()]
    _skill(pg).run(
        input=SearchInput(query="アパレルの提案", top_k=5, filter_industry="アパレル"),
        ctx=SkillContext(),
    )
    assert pg.search_similar_new_schema.call_count >= 2
    for call in pg.search_similar_new_schema.call_args_list:
        assert call.kwargs.get("exclude_duplicates") is True


def test_client_boost_call_also_carries_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # client boost 経路の search_similar_new_schema にも flag が伝播すること。
    monkeypatch.setenv("DOC_DEDUP_EXCLUDE_SEARCH", "true")
    pg = _pgvector(_hits())
    pg.list_client_names.return_value = ["ユニー"]
    skill = SearchSkill(
        bedrock=_fake_bedrock(),
        pgvector=pg,
        embedder=_FakeEmbedder(),
        use_new_schema=True,
        use_client_boost=True,
    )
    skill.run(input=SearchInput(query="ユニーの2回目提案", top_k=5), ctx=SkillContext())
    # primary + client boost で複数回呼ばれる。全て True。
    assert pg.search_similar_new_schema.call_count >= 2
    for call in pg.search_similar_new_schema.call_args_list:
        assert call.kwargs.get("exclude_duplicates") is True


def test_both_flags_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    # boilerplate と duplicates は独立な env。片方 ON でも他方は既定 False。
    monkeypatch.setenv("DOC_DEDUP_EXCLUDE_SEARCH", "1")
    monkeypatch.delenv("BOILERPLATE_EXCLUDE_SEARCH", raising=False)
    pg = _pgvector(_hits())
    skill = _skill(pg)
    assert skill._exclude_duplicates is True
    assert skill._exclude_boilerplate is False
    skill.run(input=SearchInput(query="提案", top_k=5), ctx=SkillContext())
    for call in pg.search_similar_new_schema.call_args_list:
        assert call.kwargs.get("exclude_duplicates") is True
        assert call.kwargs.get("exclude_boilerplate") is False


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
