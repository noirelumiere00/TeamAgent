"""M1（dedup 順序）と L3（除外救済）の SearchSkill 統合テスト。

M1: dedup（near-dup 畳み込み + per-doc cap）は **rerank の前（プール段階）** に走る。
    旧実装は rerank→top_k 後段に dedup を置いていたため、最良 doc が 2 chunk に
    圧縮され最終件数が top_k 未満に痩せた。プール段階で畳んでから rerank が
    top_k を選び直すことで、最終件数が top_k を維持することを固定する。

L3: boilerplate/suppressed の SQL 除外が、フィルタ解除後すら 0 件にしてしまう事故への
    最後の砦。exclude 系 ON かつ全経路 0 件のとき、exclude を全外しで 1 回だけ
    再検索し、救済 hit に is_low_confidence=True を付ける。SEARCH_EXCLUSION_RESCUE で gating。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

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

_TEMPLATE_A = "株式会社ベクトル 会社紹介 私たちは100年企業を目指すPR会社です。"
_TEMPLATE_B = "株式会社ベクトル 会社紹介  私たちは100年企業を目指す、PR会社です！"


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


def _rerank_passthrough(pg: MagicMock, bedrock: MagicMock) -> None:
    """bedrock.rerank を「入力 documents をそのまま score 降順で top_n 返す」passthrough に。

    SearchSkill._apply_cohere_rerank は response.results の index で元 hits を引くため、
    index=0..n-1 / relevance_score=降順 の RerankResult を返せば、入力順を保ったまま
    top_n に絞った rerank をエミュレートできる（DB/Bedrock 不要の決定的テスト）。
    """

    def _do_rerank(*, query: str, documents: list[str], request_id: str, top_n: int):  # type: ignore[no-untyped-def]
        n = min(top_n, len(documents))
        return RerankResponse(
            results=[RerankResult(index=i, relevance_score=1.0 - i * 0.01) for i in range(n)],
            model_arn="arn:stub",
            latency_ms=1,
        )

    bedrock.rerank.side_effect = _do_rerank


# ----------------------------------------------------------------------------
# M1: dedup はプール段階（rerank 前）で走り、最終件数が top_k を維持する
# ----------------------------------------------------------------------------


def test_m1_dedup_before_rerank_keeps_top_k(monkeypatch: pytest.MonkeyPatch) -> None:
    """top_k=5・rerank ON・dedup ON で、dedup 後も最終件数が top_k(=5) を維持する。

    プール: テンプレ near-dup 2 本（doc1/doc2、collapse で 1 本化）+ distinct body 6 本
    （doc10..doc15、各 1 本ずつなので per-doc cap 非該当）。
    旧実装（rerank→top_k→dedup）なら rerank が先に 5 本へ truncate し、その中の
    テンプレ 2 本が 1 本へ畳まれて最終 4 本に痩せていた。新実装はプール段階で先に
    テンプレを畳んでから rerank が 5 本選ぶので、最終 5 本を維持する。
    """
    monkeypatch.setenv("SEARCH_DEDUP_RESULTS", "1")
    monkeypatch.setenv("SEARCH_PER_DOC_CAP", "2")
    monkeypatch.delenv("SEARCH_NEARDUP_JACCARD", raising=False)

    pool = [
        _hit(1, _TEMPLATE_A, 0.99, 1),
        _hit(2, _TEMPLATE_B, 0.98, 2),
        _hit(10, "施策A：縦型動画で指名検索を増やす", 0.97, 10),
        _hit(11, "施策B：UGC を活用した認知拡大", 0.96, 11),
        _hit(12, "施策C：店頭 POP と連動した来店促進", 0.95, 12),
        _hit(13, "施策D：インフルエンサー起用の費用感", 0.94, 13),
        _hit(14, "施策E：保存率を高めるサムネ設計", 0.93, 14),
        _hit(15, "施策F：競合分析と差別化ポイント", 0.92, 15),
    ]
    bedrock = _fake_bedrock()
    pg = _pgvector(pool)
    _rerank_passthrough(pg, bedrock)
    skill = SearchSkill(
        bedrock=bedrock,
        pgvector=pg,
        embedder=_FakeEmbedder(),
        use_new_schema=True,
        use_cohere_rerank=True,
        rerank_pool_size=30,
        use_client_boost=False,
    )
    out = skill.run(input=SearchInput(query="施策の事例", top_k=5), ctx=SkillContext())

    # 最終件数は top_k=5 を維持（旧実装なら 4 に痩せていた）
    assert len(out.hits) == 5
    # テンプレは 1 本に畳まれている（chunk1 が代表 / chunk2 は消える）
    ids = {h.chunk_id for h in out.hits}
    assert 2 not in ids


def test_m1_rerank_off_dedup_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """rerank OFF では dedup プール段階移動でも従来の dedup 結果と一致（後方互換）。

    top_k=10・rerank OFF。pool は doc1 のテンプレ + doc1 本文 3 本。
    collapse 後 cap(doc1,2) で {代表テンプレ, body 上位 1} に絞られる。
    """
    monkeypatch.setenv("SEARCH_DEDUP_RESULTS", "1")
    monkeypatch.setenv("SEARCH_PER_DOC_CAP", "2")
    body = "ユニー様向け第2回提案：縦型動画でZ世代の指名検索を増やす施策。"
    pool = [
        _hit(1, _TEMPLATE_A, 0.95, 1),
        _hit(2, _TEMPLATE_B, 0.90, 2),
        _hit(3, body, 0.85, 1),
        _hit(4, body + "（撮影体制）", 0.80, 1),
        _hit(5, body + "（納期と費用）", 0.75, 1),
    ]
    pg = _pgvector(pool)
    skill = SearchSkill(
        bedrock=_fake_bedrock(),
        pgvector=pg,
        embedder=_FakeEmbedder(),
        use_new_schema=True,
        use_cohere_rerank=False,
        use_client_boost=False,
    )
    out = skill.run(input=SearchInput(query="ユニーの提案", top_k=10), ctx=SkillContext())
    # collapse: chunk2→chunk1 / cap(doc1,2): chunk1(0.95)+chunk3(0.85) → {1,3}
    assert {h.chunk_id for h in out.hits} == {1, 3}


# ----------------------------------------------------------------------------
# L3: 全除外 0 件 → exclude 全外しで救済し is_low_confidence を付ける
# ----------------------------------------------------------------------------


def _skill_with_exclude(
    pg: MagicMock,
    *,
    rescue_env: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> SearchSkill:
    monkeypatch.setenv("DOC_DEDUP_EXCLUDE_SEARCH", "1")  # exclude 系を ON
    if rescue_env is None:
        monkeypatch.delenv("SEARCH_EXCLUSION_RESCUE", raising=False)
    else:
        monkeypatch.setenv("SEARCH_EXCLUSION_RESCUE", rescue_env)
    return SearchSkill(
        bedrock=_fake_bedrock(),
        pgvector=pg,
        embedder=_FakeEmbedder(),
        use_new_schema=True,
        use_client_boost=False,
    )


def test_l3_all_excluded_zero_then_rescued_low_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exclude ON で 0 件 → exclude 全外しの再検索で近傍が返り is_low_confidence=True。

    pgvector.search_similar_new_schema を「exclude 指定が真なら 0 件、両方 False なら
    近傍を返す」side_effect にし、_pool_search の最後の砦が発火することを固定する。
    """
    nearby = [_hit(7, "近傍だが除外対象だった本文", 0.6, 7)]

    def _se(*args, **kwargs):  # type: ignore[no-untyped-def]
        excl = kwargs.get("exclude_boilerplate") or kwargs.get("exclude_duplicates")
        return [] if excl else list(nearby)

    pg = _pgvector([])
    pg.search_similar_new_schema.side_effect = _se
    skill = _skill_with_exclude(pg, rescue_env=None, monkeypatch=monkeypatch)  # 既定 ON
    out = skill.run(input=SearchInput(query="提案", top_k=5), ctx=SkillContext())

    assert len(out.hits) == 1
    assert out.hits[0].chunk_id == 7
    # 救済 hit は低信頼マークが付く
    assert out.hits[0].is_low_confidence is True
    # 最後の砦は exclude を両方 False で呼んでいる
    rescue_calls = [
        c
        for c in pg.search_similar_new_schema.call_args_list
        if c.kwargs.get("exclude_boilerplate") is False
        and c.kwargs.get("exclude_duplicates") is False
    ]
    assert rescue_calls, "救済（exclude 全外し）の再検索が呼ばれていない"


def test_l3_rescue_disabled_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """SEARCH_EXCLUSION_RESCUE=0 なら救済せず 0 件のまま（安全側 gating）。"""

    def _se(*args, **kwargs):  # type: ignore[no-untyped-def]
        excl = kwargs.get("exclude_boilerplate") or kwargs.get("exclude_duplicates")
        return [] if excl else [_hit(7, "近傍", 0.6, 7)]

    pg = _pgvector([])
    pg.search_similar_new_schema.side_effect = _se
    skill = _skill_with_exclude(pg, rescue_env="0", monkeypatch=monkeypatch)
    out = skill.run(input=SearchInput(query="提案", top_k=5), ctx=SkillContext())
    assert len(out.hits) == 0
    # exclude 全外しの救済呼び出しは行われない
    for c in pg.search_similar_new_schema.call_args_list:
        assert not (
            c.kwargs.get("exclude_boilerplate") is False
            and c.kwargs.get("exclude_duplicates") is False
        )


def test_l3_no_rescue_when_exclude_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """exclude 系が両方 OFF なら救済経路に入らない（無影響・後方互換）。

    0 件は素直に 0 件のまま。救済は exclude が効いている前提でのみ意味を持つ。
    """
    monkeypatch.delenv("DOC_DEDUP_EXCLUDE_SEARCH", raising=False)
    monkeypatch.delenv("BOILERPLATE_EXCLUDE_SEARCH", raising=False)
    monkeypatch.setenv("SEARCH_EXCLUSION_RESCUE", "true")
    pg = _pgvector([])  # 常に 0 件
    skill = SearchSkill(
        bedrock=_fake_bedrock(),
        pgvector=pg,
        embedder=_FakeEmbedder(),
        use_new_schema=True,
        use_client_boost=False,
    )
    out = skill.run(input=SearchInput(query="提案", top_k=5), ctx=SkillContext())
    assert len(out.hits) == 0
    # 全呼び出しが exclude False（=救済の再検索が無い）であることまでは問わないが、
    # exclude 系 OFF なので 1 回目から exclude=False で呼ばれている
    assert pg.search_similar_new_schema.call_args.kwargs.get("exclude_duplicates") is False


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
