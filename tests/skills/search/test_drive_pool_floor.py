"""Drive 実資料のリコール床（_apply_drive_floor）の回帰テスト。

**再現する本番障害（2026-08-27 実測）**

営業 FB（gsheets 809 + slack 559 chunk / 873 document）が埋め込み空間で 1 つの巨大な
近傍クラスタを作っており、コーパス全体の類似度分布が min 0.790 / avg 0.876 / max 1.000・
σ=0.0196 まで圧縮されている。その結果、営業の話し言葉クエリでは **dense 上位 30 件が
100% FB 行**になり、gdrive の最上位は 78 位（大塚製薬「広告審査について」は 4,422 位）。
rerank は与えられた候補しか並べ替えられないので Drive 資料は土俵に上がらず、
knowledge_deliver の添付候補が 0 件になる（= ユーザー報告「ドライブ資料が検索でヒット
しない」）。閾値を下げても候補が FB 行なので実ファイルは 1 件も添付できない。

本テストは「フェイクが本番の失敗モードを再現していること」を要件にする:
pgvector フェイクは **filter_source_types が無いクエリでは FB 行しか返さない**
（＝ dense のプールに gdrive が 1 件も入らない本番の状態）。
"""

from __future__ import annotations

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

    def _do_rerank(*, query: str, documents: list[str], request_id: str, top_n: int):  # type: ignore[no-untyped-def]
        """本物の cross-encoder を模した rerank。**入力順の passthrough ではない**。

        passthrough にすると「プールに入れさえすれば上位に出る」という誤った緑になる。
        本番の Cohere Rerank は候補の中身を読んで並べ替えるので、ここでも
        「クエリ語（薬機法/広告審査）を実際に含む文書を上へ」というスコアで並べ替える。
        重要なのは **候補に入っていない文書は何位にもなれない** という性質で、
        これが今回の障害（gdrive がプールに 1 件も無い）を再現する軸になる。
        """
        scored = sorted(
            enumerate(documents),
            key=lambda p: (-(0.9 if ("薬機法" in p[1] or "広告審査" in p[1]) else 0.3), p[0]),
        )
        n = min(top_n, len(documents))
        results = []
        for rank, (idx, doc) in enumerate(scored[:n]):
            base = 0.9 if ("薬機法" in doc or "広告審査" in doc) else 0.3
            results.append(RerankResult(index=idx, relevance_score=base - rank * 0.001))
        return RerankResponse(
            results=results,
            model_arn="arn:stub",
            latency_ms=1,
            query_count=1,
        )

    mock.rerank.side_effect = _do_rerank
    return mock


def _fb_hit(i: int) -> SearchHit:
    """営業 FB（管理シート行）。本文に資料名を持たない＝添付できる実ファイルが無い。"""
    return SearchHit(
        chunk_id=1000 + i,
        content=f"営業FB {i}: 先方の反応は良好。次回は薬事の審査フローを確認する。",
        score=0.94 - i * 0.0001,
        metadata={
            "source_type": "gsheets",
            "source_uri": f"gsheets://SHEET/{i}#gid=0&range={i}:{i}",
            "document_id": f"fb-doc-{i}",
            "title": f"営業FB行{i}",
            "is_sales_fb": True,
            "client_name": "某社",
        },
    )


def _drive_hit(i: int) -> SearchHit:
    return SearchHit(
        chunk_id=2000 + i,
        content=f"提案書 {i}: 広告審査について（薬機法・景表法の確認フロー）",
        score=0.88 - i * 0.0001,
        metadata={
            "source_type": "gdrive",
            "source_uri": f"gdrive://FILEID{i}",
            "document_id": f"drive-doc-{i}",
            "title": f"タテガタ｜広告審査について{i}",
        },
    )


def _pgvector(
    *,
    fb_pool: list[SearchHit],
    drive_pool: list[SearchHit],
    calls: list[dict[str, Any]],
    drive_raises: bool = False,
) -> MagicMock:
    """本番の失敗モードを再現するフェイク。

    filter_source_types 未指定（本検索）→ FB 行しか返さない。
    filter_source_types=["gdrive"]（リコール床の補助検索）→ Drive 資料を返す。
    """
    mock = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock())
    cm.__exit__ = MagicMock(return_value=False)
    mock.connection.return_value = cm

    def _search(**kwargs: Any) -> list[SearchHit]:
        calls.append(kwargs)
        types = kwargs.get("filter_source_types")
        if types:
            if drive_raises:
                raise RuntimeError("pgvector down")
            limit = int(kwargs.get("limit") or 0)
            return drive_pool[:limit]
        limit = int(kwargs.get("limit") or 0)
        return fb_pool[:limit]

    mock.search_similar_new_schema.side_effect = _search
    return mock


def _build(
    pg: MagicMock,
    *,
    drive_pool_floor: int = 15,
    use_cohere_rerank: bool = True,
) -> SearchSkill:
    return SearchSkill(
        bedrock=_fake_bedrock(),
        pgvector=pg,
        embedder=_FakeEmbedder(),
        use_new_schema=True,
        use_cohere_rerank=use_cohere_rerank,
        rerank_pool_size=30,
        drive_pool_floor=drive_pool_floor,
        use_client_boost=False,
    )


def _drive_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in calls if c.get("filter_source_types")]


# ---------------------------------------------------------------------------
# 中核: FB 行だけのプールへ Drive 資料が合流する
# ---------------------------------------------------------------------------


def test_drive_floor_rescues_gdrive_from_all_fb_pool() -> None:
    """dense プールが 100% FB 行でも、Drive 資料が最終ヒットに現れる（本障害の直接再現）。"""
    calls: list[dict[str, Any]] = []
    pg = _pgvector(
        fb_pool=[_fb_hit(i) for i in range(30)],
        drive_pool=[_drive_hit(i) for i in range(15)],
        calls=calls,
    )
    out = _build(pg).run(input=SearchInput(query="薬事審査フロー", top_k=5), ctx=SkillContext())

    source_types = [h.source_type for h in out.hits]
    assert "gdrive" in source_types, f"Drive 資料が 1 件も出ていない: {source_types}"
    # 補助検索は gdrive 限定で 1 回だけ走る。
    assert len(_drive_calls(calls)) == 1
    assert _drive_calls(calls)[0]["filter_source_types"] == ["gdrive"]


def test_drive_floor_hits_are_attachable_drive_uris() -> None:
    """合流した Drive ヒットは gdrive:// の source_uri を持つ＝ knowledge_deliver が
    file_id を抽出して実添付できる（「hits はあるのに candidates=0」の解消）。"""
    calls: list[dict[str, Any]] = []
    pg = _pgvector(
        fb_pool=[_fb_hit(i) for i in range(30)],
        drive_pool=[_drive_hit(i) for i in range(15)],
        calls=calls,
    )
    out = _build(pg).run(input=SearchInput(query="薬事審査フロー", top_k=10), ctx=SkillContext())

    drive = [h for h in out.hits if h.source_type == "gdrive"]
    assert drive, "gdrive ヒットが無い"
    assert all(str(h.source_uri).startswith("gdrive://") for h in drive)


# ---------------------------------------------------------------------------
# 発火条件: 満たしているクエリには 1 クエリも足さない（費用ゼロ）
# ---------------------------------------------------------------------------


def test_drive_floor_not_fired_when_pool_already_has_enough_gdrive() -> None:
    """プールが既に床を満たしていれば補助検索を一切走らせない。"""
    calls: list[dict[str, Any]] = []
    pg = _pgvector(
        fb_pool=[_drive_hit(i) for i in range(20)],  # 本検索が gdrive を 20 件返す
        drive_pool=[_drive_hit(100 + i) for i in range(15)],
        calls=calls,
    )
    _build(pg).run(input=SearchInput(query="広告審査", top_k=5), ctx=SkillContext())

    assert _drive_calls(calls) == []


def test_drive_floor_disabled_by_zero() -> None:
    """drive_pool_floor=0 は完全無効（従来挙動と一致）。"""
    calls: list[dict[str, Any]] = []
    pg = _pgvector(
        fb_pool=[_fb_hit(i) for i in range(30)],
        drive_pool=[_drive_hit(i) for i in range(15)],
        calls=calls,
    )
    out = _build(pg, drive_pool_floor=0).run(
        input=SearchInput(query="薬事審査フロー", top_k=5), ctx=SkillContext()
    )

    assert _drive_calls(calls) == []
    assert all(h.source_type != "gdrive" for h in out.hits)


def test_drive_floor_not_fired_without_rerank() -> None:
    """rerank 無効時はプール概念が無い（retrieve_limit=top_k）ので発火しない。"""
    calls: list[dict[str, Any]] = []
    pg = _pgvector(
        fb_pool=[_fb_hit(i) for i in range(30)],
        drive_pool=[_drive_hit(i) for i in range(15)],
        calls=calls,
    )
    _build(pg, use_cohere_rerank=False).run(
        input=SearchInput(query="薬事審査フロー", top_k=5), ctx=SkillContext()
    )

    assert _drive_calls(calls) == []


# ---------------------------------------------------------------------------
# 明示フィルタの保持・重複・fail-open
# ---------------------------------------------------------------------------


def test_drive_floor_preserves_user_explicit_filters() -> None:
    """ユーザー明示の client / budget / doc_type は補助検索にも同値で載る。

    載せ忘れると「電通の提案書」で絞ったのに無関係な Drive 資料が合流する
    （明示フィルタは全再検索で保持する、という既存設計の穴を塞ぐ）。
    """
    calls: list[dict[str, Any]] = []
    pg = _pgvector(
        fb_pool=[_fb_hit(i) for i in range(30)],
        drive_pool=[_drive_hit(i) for i in range(15)],
        calls=calls,
    )
    _build(pg).run(
        input=SearchInput(
            query="広告審査",
            top_k=5,
            filter_client="電通",
            filter_doc_type="提案書",
            filter_budget="500万〜",
        ),
        ctx=SkillContext(),
    )

    drive_call = _drive_calls(calls)[0]
    assert drive_call["metadata_contains"] == {"__client__": "電通"}
    assert drive_call["sticky_filters"] == {"cls_doc_type": "提案書", "cls_budget": "500万〜"}


def test_drive_floor_does_not_duplicate_existing_chunks() -> None:
    """補助検索が本検索と同じ chunk を返しても二重に積まない。"""
    calls: list[dict[str, Any]] = []
    shared = [_drive_hit(i) for i in range(3)]
    pg = _pgvector(
        fb_pool=[_fb_hit(i) for i in range(27)] + shared,
        drive_pool=shared,  # 全て既出
        calls=calls,
    )
    out = _build(pg).run(input=SearchInput(query="薬事審査フロー", top_k=30), ctx=SkillContext())

    ids = [h.chunk_id for h in out.hits]
    assert len(ids) == len(set(ids)), f"chunk_id が重複している: {ids}"


def test_drive_floor_failure_is_fail_open() -> None:
    """補助検索が落ちても検索本体は成功する（fail-open）。"""
    calls: list[dict[str, Any]] = []
    pg = _pgvector(
        fb_pool=[_fb_hit(i) for i in range(30)],
        drive_pool=[],
        calls=calls,
        drive_raises=True,
    )
    out = _build(pg).run(input=SearchInput(query="薬事審査フロー", top_k=5), ctx=SkillContext())

    assert len(out.hits) == 5
    assert all(h.source_type == "gsheets" for h in out.hits)


# ---------------------------------------------------------------------------
# アダプタ側: filter_source_types の SQL 反映
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("types", "expect_clause"),
    [
        (None, False),
        ([], False),
        (["  "], False),  # 空白のみ → 全件除外の事故を避けて句を足さない
        (["gdrive"], True),
    ],
)
def test_pgvector_source_type_clause(types: list[str] | None, expect_clause: bool) -> None:
    """filter_source_types 指定時のみ ANY 句が足される（既定は現行 SQL と完全一致）。"""
    from teamagent.adapters.pgvector_client import PgVectorClient

    captured: dict[str, Any] = {}

    class _Cur:
        def __enter__(self) -> _Cur:
            return self

        def __exit__(self, *a: Any) -> bool:
            return False

        def execute(self, sql: str, params: list[Any]) -> None:
            captured["sql"] = sql
            captured["params"] = params

        def fetchall(self) -> list[dict[str, Any]]:
            return []

    conn = MagicMock()
    conn.cursor.return_value = _Cur()

    client = PgVectorClient(dsn="postgresql://stub")
    client.search_similar_new_schema(
        conn=conn,
        embedding=[0.1] * 1024,
        limit=5,
        filter_source_types=types,
    )

    has_clause = "d.source_type::text = ANY(%s::text[])" in captured["sql"]
    assert has_clause is expect_clause
    if expect_clause:
        assert ["gdrive"] in captured["params"]
