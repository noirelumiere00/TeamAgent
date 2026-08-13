"""SearchSkill へのテンプレ/定期報告除外（TEMPLATE_EXCLUDE_SEARCH env-gate）の統合テスト。

契約:
- env OFF（既定 / 未設定）: search_similar_new_schema は exclude_templates=False /
  exclude_recurring=False で呼ばれる＝現行と完全一致（除外は一切起きない）
- env ON: exclude_templates=True を**全経路**（初回 / fail-open / client boost）で渡す
- exclude_recurring は「提案書 intent」（明示 filter_doc_type=提案書 or 自動抽出
  cls_doc_type=提案書）のときだけ True。「上期報告を見たい」等の定期報告クエリは殺さない
- 明示 filter_doc_type が提案書以外なら、自動抽出が提案事例でも recurring は立てない（明示優先）
- exclusion_rescue（0 件時の最後の再検索）では新フラグも False に倒して救済する
- env 読み取りは skill 側（__init__）で 1 回（boilerplate / dedup の env と同じ流儀）

呼び出し引数は MagicMock の call_args で捕捉する（test_boilerplate_exclude.py と同型）。
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


def _hits() -> list[SearchHit]:
    return [
        _hit(1, "本文 A", 0.95, 1),
        _hit(2, "本文 B", 0.90, 2),
    ]


def _pgvector(hits: list[SearchHit]) -> MagicMock:
    mock = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock())
    cm.__exit__ = MagicMock(return_value=False)
    mock.connection.return_value = cm
    mock.search_similar_new_schema.return_value = hits
    return mock


def _skill(pg: MagicMock, **kw: object) -> SearchSkill:
    return SearchSkill(
        bedrock=_fake_bedrock(),
        pgvector=pg,
        embedder=_FakeEmbedder(),
        use_new_schema=True,
        use_client_boost=False,
        **kw,  # type: ignore[arg-type]
    )


def _flags(call: object) -> tuple[bool, bool]:
    kwargs = call.kwargs  # type: ignore[attr-defined]
    return (
        bool(kwargs.get("exclude_templates", False)),
        bool(kwargs.get("exclude_recurring", False)),
    )


def test_env_off_passes_false_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    # 未設定 → 両フラグ False（現行と完全一致・後方互換）
    monkeypatch.delenv("TEMPLATE_EXCLUDE_SEARCH", raising=False)
    pg = _pgvector(_hits())
    _skill(pg).run(
        input=SearchInput(query="出光興産の提案事例", top_k=5, filter_doc_type="提案書"),
        ctx=SkillContext(),
    )
    assert pg.search_similar_new_schema.call_count >= 1
    for call in pg.search_similar_new_schema.call_args_list:
        assert _flags(call) == (False, False)


def test_env_on_templates_always_recurring_off_without_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 提案書 intent が無い通常クエリ: templates=True / recurring=False
    monkeypatch.setenv("TEMPLATE_EXCLUDE_SEARCH", "1")
    pg = _pgvector(_hits())
    _skill(pg).run(input=SearchInput(query="出光興産について", top_k=5), ctx=SkillContext())
    for call in pg.search_similar_new_schema.call_args_list:
        assert _flags(call) == (True, False)


def test_env_on_recurring_query_not_killed(monkeypatch: pytest.MonkeyPatch) -> None:
    # 「上期報告を見たい」: recurring を立てない（定期報告そのものを探すクエリを殺さない）
    monkeypatch.setenv("TEMPLATE_EXCLUDE_SEARCH", "true")
    pg = _pgvector(_hits())
    _skill(pg, use_knowledge_filters=True).run(
        input=SearchInput(query="上期の売上報告を見たい", top_k=5), ctx=SkillContext()
    )
    for call in pg.search_similar_new_schema.call_args_list:
        assert _flags(call) == (True, False)


def test_env_on_explicit_proposal_filter_sets_recurring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEMPLATE_EXCLUDE_SEARCH", "1")
    pg = _pgvector(_hits())
    _skill(pg).run(
        input=SearchInput(query="出光興産", top_k=5, filter_doc_type="提案書"),
        ctx=SkillContext(),
    )
    for call in pg.search_similar_new_schema.call_args_list:
        assert _flags(call) == (True, True)


def test_env_on_auto_proposal_intent_sets_recurring(monkeypatch: pytest.MonkeyPatch) -> None:
    # 自動抽出（extract_knowledge_filters: 「提案事例」→ cls_doc_type=提案書）でも intent 成立
    monkeypatch.setenv("TEMPLATE_EXCLUDE_SEARCH", "1")
    pg = _pgvector(_hits())
    _skill(pg, use_knowledge_filters=True).run(
        input=SearchInput(query="出光興産の提案事例を教えて", top_k=5), ctx=SkillContext()
    )
    for call in pg.search_similar_new_schema.call_args_list:
        assert _flags(call) == (True, True)


def test_env_on_auto_intent_needs_knowledge_filters_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # USE_KNOWLEDGE_FILTERS 相当が OFF なら自動抽出は走らず recurring は立たない
    monkeypatch.setenv("TEMPLATE_EXCLUDE_SEARCH", "1")
    pg = _pgvector(_hits())
    _skill(pg, use_knowledge_filters=False).run(
        input=SearchInput(query="出光興産の提案事例を教えて", top_k=5), ctx=SkillContext()
    )
    for call in pg.search_similar_new_schema.call_args_list:
        assert _flags(call) == (True, False)


def test_env_on_explicit_non_proposal_overrides_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    # クエリに「提案事例」があっても明示 filter_doc_type=議事録 が優先 → recurring False
    monkeypatch.setenv("TEMPLATE_EXCLUDE_SEARCH", "1")
    pg = _pgvector(_hits())
    _skill(pg, use_knowledge_filters=True).run(
        input=SearchInput(query="提案事例の議事録", top_k=5, filter_doc_type="議事録"),
        ctx=SkillContext(),
    )
    for call in pg.search_similar_new_schema.call_args_list:
        assert _flags(call) == (True, False)


def test_rescue_path_drops_new_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    # 0 件時の exclusion_rescue では新フラグも False に倒して救済する
    monkeypatch.setenv("TEMPLATE_EXCLUDE_SEARCH", "1")
    pg = _pgvector([])  # 常に 0 件 → rescue 経路まで到達
    _skill(pg).run(
        input=SearchInput(query="出光興産", top_k=5, filter_doc_type="提案書"),
        ctx=SkillContext(),
    )
    calls = pg.search_similar_new_schema.call_args_list
    assert len(calls) >= 2, "rescue 再検索が走っていない"
    # 最後の呼び出し（rescue）は全 exclude が False
    last = calls[-1]
    assert _flags(last) == (False, False)
    assert last.kwargs.get("exclude_boilerplate") is False
    assert last.kwargs.get("exclude_duplicates") is False
    # rescue 以外（初回）は ON のまま
    assert _flags(calls[0]) == (True, True)


def test_rescue_triggers_even_when_only_new_flags_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # boilerplate/duplicates が OFF でも、新フラグだけで rescue 経路に入ること
    monkeypatch.delenv("BOILERPLATE_EXCLUDE_SEARCH", raising=False)
    monkeypatch.delenv("DOC_DEDUP_EXCLUDE_SEARCH", raising=False)
    monkeypatch.setenv("TEMPLATE_EXCLUDE_SEARCH", "1")
    pg = _pgvector([])
    _skill(pg).run(input=SearchInput(query="出光興産", top_k=5), ctx=SkillContext())
    calls = pg.search_similar_new_schema.call_args_list
    assert len(calls) == 2  # 初回（フィルタ無し→fail-open 再検索なし）+ rescue
    assert _flags(calls[0]) == (True, False)
    assert _flags(calls[1]) == (False, False)


def test_client_boost_call_also_carries_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    # client boost 経路の search_similar_new_schema にも両フラグが伝播すること
    # （過去 2 回レビューで検出された boost 経路の配線漏れの regression 防止）。
    monkeypatch.setenv("TEMPLATE_EXCLUDE_SEARCH", "1")
    pg = _pgvector(_hits())
    pg.list_client_names.return_value = ["ユニー"]
    skill = SearchSkill(
        bedrock=_fake_bedrock(),
        pgvector=pg,
        embedder=_FakeEmbedder(),
        use_new_schema=True,
        use_client_boost=True,
        use_knowledge_filters=True,
    )
    skill.run(input=SearchInput(query="ユニーの提案事例", top_k=5), ctx=SkillContext())
    calls = pg.search_similar_new_schema.call_args_list
    assert len(calls) >= 2  # primary + client boost
    # boost は __client__（cls_project / client_name / title の OR-ILIKE）で絞る。
    # 本検索は filter_client 未指定なので __client__ を持たず、2 本目以降で一意に特定できる。
    boost_calls = [
        c for c in calls[1:] if (c.kwargs.get("metadata_contains") or {}).get("__client__")
    ]
    assert boost_calls, "client boost の検索が走っていない"
    for call in calls:
        assert _flags(call) == (True, True)


def test_env_read_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    # __init__ で 1 回読んで self に保持（boilerplate / dedup の env と同じ流儀）
    monkeypatch.setenv("TEMPLATE_EXCLUDE_SEARCH", "yes")
    skill = _skill(_pgvector(_hits()))
    assert skill._exclude_templates is True
    monkeypatch.setenv("TEMPLATE_EXCLUDE_SEARCH", "0")
    assert skill._exclude_templates is True  # 構築後の env 変更は反映しない


def test_env_default_construction_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEMPLATE_EXCLUDE_SEARCH", raising=False)
    skill = _skill(_pgvector(_hits()))
    assert skill._exclude_templates is False


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", ""])
def test_env_falsy_variants_disable(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("TEMPLATE_EXCLUDE_SEARCH", raw)
    pg = _pgvector(_hits())
    _skill(pg).run(input=SearchInput(query="提案", top_k=5), ctx=SkillContext())
    for call in pg.search_similar_new_schema.call_args_list:
        assert _flags(call) == (False, False)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
