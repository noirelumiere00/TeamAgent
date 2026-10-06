"""「該当なし」をはっきり言う（not_found.judge_found と SearchSkill.run の配線）のテスト。

背景: 評価セット 50 問の「該当なし」が正解の 7 問（23〜25・47〜50）が 0/7 だった。本番は
SEARCH_MIN_RELEVANCE=0.4 ＋ FALLBACK=0.05 で、0.4 未満のヒットしか無い問いでも低信頼として
救出され、要約器がそれを根拠に回答を書く。ここではその失敗モード（rerank 後の低スコア・
救出された低信頼ヒット・別取引先の資料・主題語が無い資料）を再現して固定する。

スコアの値は gold set 実測（commit c8cf1e83）に合わせる: 実ヒット 0.50〜0.94、
該当なし 23/24/25 = 0.23/0.12/0.06。PoC の境界の実ヒット 0.30 も残す。
実 DB 0・実 Bedrock 0。
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
from teamagent.skills.search.not_found import (
    NOT_FOUND_HEAD,
    build_not_found_answer,
    judge_found,
    query_subject_terms,
)
from teamagent.skills.search.result_guard import WEAK_RESULT_NOTICE
from teamagent.skills.search.schema import SearchInput
from teamagent.skills.search.skill import SearchSkill
from teamagent.skills.search.two_stage import TWO_STAGE_CTX_KEY, TWO_STAGE_ENV

_DEFAULT = {"score_threshold": 0.25, "subject_check_below": 0.5}


def _hit(score: float, content: str = "本文", chunk_id: int = 1, **meta: Any) -> SearchHit:
    return SearchHit(chunk_id=chunk_id, content=content, score=score, metadata=dict(meta))


# ── 判定（純関数）──────────────────────────────────────────────────────────


def test_no_hits_is_not_found() -> None:
    d = judge_found("何か", [], **_DEFAULT)
    assert (d.found, d.reason) == (False, "no_hits")


@pytest.mark.parametrize(
    ("query", "score"),
    [
        ("東芝の半導体事業の提案について", 0.23),  # gold 23（漢字の固有名は score で判定）
        ("2027 年 4 月のキャンペーン計画", 0.12),  # gold 24
        ("TeamAgent Bot 自身の開発履歴", 0.06),  # gold 25
    ],
)
def test_gold_negatives_by_score_are_not_found(query: str, score: float) -> None:
    hits = [_hit(score, content="飲料メーカー向けショート動画の提案。キャンペーン計画の例。")]
    d = judge_found(query, hits, **_DEFAULT)
    assert (d.found, d.reason) == (False, "low_score")


@pytest.mark.parametrize("score", [0.50, 0.94, 0.30])
def test_gold_positive_scores_stay_found(score: float) -> None:
    """実ヒットの最低 0.50・最高 0.94、PoC 境界の 0.30 は該当ありのまま。"""
    hits = [_hit(score, content="日本ガイシ ADK中部 ケイパ提案のFB", client_name="日本ガイシ")]
    d = judge_found("日本ガイシのケイパ提案について教えて", hits, **_DEFAULT)
    assert d.found is True


def test_threshold_boundary_is_inclusive() -> None:
    hits = [_hit(0.25, content="提案資料")]
    assert judge_found("提案資料", hits, **_DEFAULT).found is True
    assert judge_found("提案資料", [_hit(0.2499, content="提案資料")], **_DEFAULT).found is False


def test_threshold_zero_disables_score_criterion() -> None:
    hits = [_hit(0.01, content="提案資料")]
    d = judge_found("提案資料", hits, score_threshold=0.0, subject_check_below=0.0)
    assert d.found is True


@pytest.mark.parametrize(
    ("query", "score", "content"),
    [
        # gold 47: 「キャンペーン提案」で別取引先の資料が中位スコアで当たる
        ("トヨタ自動車の EV 向けキャンペーン提案", 0.35, "花王様向けキャンペーン提案の構成"),
        # gold 49: 未来の架空テーマ
        ("2030年のメタバース広告戦略", 0.32, "2025年のショート動画広告の戦略"),
        # gold 50: 業務無関係の雑談
        ("今日のランチのおすすめは", 0.28, "おすすめの訴求は価格より体験"),
    ],
)
def test_gold_negatives_by_subject_are_not_found(query: str, score: float, content: str) -> None:
    hits = [_hit(score, content=content, chunk_id=i) for i in range(1, 4)]
    d = judge_found(query, hits, **_DEFAULT)
    assert (d.found, d.reason) == (False, "subject_mismatch")
    assert d.terms


@pytest.mark.parametrize(
    ("query", "hit_kwargs"),
    [
        # gold 36: 「キリンビバレッジ」と問われ、ヒットは取引先「キリン」（逆向きの包含）
        (
            "キリンビバレッジ向けの商談メモ",
            {"content": "商談メモ。温度感は高い", "client_name": "キリン"},
        ),
        # gold 37: 本文に「ニチレイ」
        ("ニチレイフーズの2回目以降の提案", {"content": "ニチレイフーズ 2回目以降提案"}),
        # gold 26: 「リクルーティング」は一般語なので「ガイシ」だけで照合する
        ("日本ガイシのリクルーティング案件はどうなってる", {"content": "日本ガイシ 採用動画"}),
        # gold 12・45・22: 一般語しか無い問いは主題語の照合をしない
        ("飲料メーカー向けの提案実績", {"content": "伊藤園 提案"}),
        ("ショート動画の制作フロー資料", {"content": "制作の進め方"}),
        ("ショート動画戦略の基本資料", {"content": "基本の考え方"}),
        # gold 38: 語のどれか 1 つが当たれば該当あり
        ("PPIH ドンキ向けのショート動画提案", {"content": "PPIH 向け提案のFB"}),
    ],
)
def test_gold_positives_with_terms_stay_found(query: str, hit_kwargs: dict[str, Any]) -> None:
    hits = [_hit(0.42, **hit_kwargs)]
    d = judge_found(query, hits, **_DEFAULT)
    assert d.found is True, d


def test_high_score_skips_subject_check() -> None:
    """確信の高いヒット（≥0.5）は語の表記ゆれで落とさない。"""
    hits = [_hit(0.6, content="別表記の資料")]
    assert judge_found("メタバース広告", hits, **_DEFAULT).found is True


def test_subject_term_in_title_or_entities_counts() -> None:
    hits = [_hit(0.3, content="本文", title="メタバース施策_提案.pptx")]
    assert judge_found("メタバース広告の事例", hits, **_DEFAULT).found is True
    hits = [_hit(0.3, content="本文", cls_entities=["トヨタ"])]
    assert judge_found("トヨタの提案", hits, **_DEFAULT).found is True


def test_client_mismatch_when_no_hit_is_about_the_asked_client() -> None:
    hits = [
        _hit(0.81, client_name="花王", chunk_id=1),
        _hit(0.7, cls_project="ライオン", chunk_id=2),
    ]
    d = judge_found("資生堂の提案書ある？", hits, **_DEFAULT, query_client="資生堂")
    assert (d.found, d.reason) == (False, "client_mismatch")


def test_client_found_if_any_hit_matches_or_alias() -> None:
    hits = [
        _hit(0.81, client_name="花王", chunk_id=1),
        _hit(0.5, title="資生堂様_提案", chunk_id=2),
    ]
    assert judge_found("資生堂の提案書", hits, **_DEFAULT, query_client="資生堂").found is True
    hits = [_hit(0.8, cls_project="ハビットプロ")]
    d = judge_found(
        "アース製薬の資料",
        hits,
        **_DEFAULT,
        query_client="アース製薬",
        client_aliases=["ハビットプロ"],
    )
    assert d.found is True


def test_client_mentioned_in_content_counts_as_found() -> None:
    """判定は「有り」側へ倒す: 本文に名指しの取引先が出るなら該当なしとは言わない（警告ヘッダの領分）。"""
    hits = [_hit(0.81, content="競合の資生堂は…", client_name="花王")]
    assert judge_found("資生堂の提案書", hits, **_DEFAULT, query_client="資生堂").found is True


def test_related_drive_hits_do_not_lift_the_top_score() -> None:
    hits = [_hit(0.1, chunk_id=1), _hit(1.0, chunk_id=2, is_related_drive=True)]
    assert judge_found("提案", hits, **_DEFAULT).reason == "low_score"


def test_top_score_uses_max_not_first() -> None:
    """並べ替え（予算近接・取引先一致）で先頭が最大とは限らない。"""
    hits = [_hit(0.2, chunk_id=1, content="提案"), _hit(0.6, chunk_id=2, content="提案")]
    assert judge_found("提案", hits, **_DEFAULT).found is True


def test_query_subject_terms_drops_general_words_and_self_org() -> None:
    assert query_subject_terms("NewsTV の採用向け資料") == []
    assert query_subject_terms("UGCのTTOで成功した事例") == []
    assert query_subject_terms("ＴｏｙｏｔａのEV キャンペーン") == ["Toyota"]
    assert query_subject_terms("トヨタ自動車の EV 向けキャンペーン提案") == ["トヨタ"]


# ── 回答文 ────────────────────────────────────────────────────────────────


def test_not_found_answer_lists_up_to_three_near_items() -> None:
    hits = [
        _hit(0.2, chunk_id=1, title="花王_提案書.pptx"),
        _hit(0.2, chunk_id=2, title="花王_提案書.pptx"),  # 重複は畳む
        _hit(0.1, chunk_id=3, file_name="ライオン報告.pdf"),
        _hit(0.1, chunk_id=4, channel_name="proj-01"),
        _hit(0.1, chunk_id=5, cls_project="資生堂"),
    ]
    assert build_not_found_answer(hits) == (
        f"{NOT_FOUND_HEAD}（近いもの: 『花王_提案書.pptx』『ライオン報告.pdf』『#proj-01』）。"
    )


def test_not_found_answer_without_labels_has_no_parentheses() -> None:
    assert build_not_found_answer([]) == f"{NOT_FOUND_HEAD}。"
    assert build_not_found_answer([_hit(0.1)]) == f"{NOT_FOUND_HEAD}。"


def test_not_found_answer_truncates_long_labels() -> None:
    answer = build_not_found_answer([_hit(0.1, title="あ" * 80)])
    assert "あ" * 40 + "…" in answer
    assert "あ" * 41 not in answer


# ── skill 配線（本番の設定で再現）────────────────────────────────────────────


def _converse() -> ConverseResponse:
    return ConverseResponse(
        text="もっともらしい要約",
        usage=TokenUsage(
            input_tokens=10,
            output_tokens=10,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.002,
        ),
        model_id="m",
        latency_ms=1,
        stop_reason="end_turn",
    )


class _Embedder:
    def embed(self, text: str) -> list[float]:
        return [0.1] * 8


def _pg(hits: list[SearchHit], vocab: list[str] | None = None) -> MagicMock:
    pg = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock())
    cm.__exit__ = MagicMock(return_value=False)
    pg.connection.return_value = cm
    pg.search_similar.return_value = hits
    pg.search_similar_new_schema.return_value = hits
    pg.search_drive_by_client_names.return_value = []
    pg.list_client_names.return_value = vocab or []
    pg.resolve_file_urls_by_titles.return_value = {}
    return pg


def _prod_skill(bedrock: MagicMock, pg: MagicMock) -> SearchSkill:
    """本番の mcp と同じ閾値（rerank あり・0.4 ＋ fallback 0.05）。"""
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
    )


def _rerank(scores: list[float]) -> RerankResponse:
    return RerankResponse(
        results=[RerankResult(index=i, relevance_score=s) for i, s in enumerate(scores)],
        model_arn="arn",
        latency_ms=1,
        query_count=1,
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "SEARCH_NOT_FOUND_ANSWER",
        "SEARCH_NOT_FOUND_THRESHOLD",
        "SEARCH_NOT_FOUND_SUBJECT_BELOW",
        TWO_STAGE_ENV,
        "USE_KNOWLEDGE_DELIVER",
    ):
        monkeypatch.delenv(name, raising=False)


def test_prod_low_confidence_rescue_no_longer_summarizes() -> None:
    """本番の失敗モード: 0.4 で全滅 → 0.05 で低信頼救出 → 以前は要約器がそれで回答していた。"""
    bedrock = MagicMock()
    bedrock.converse.return_value = _converse()
    bedrock.rerank.return_value = _rerank([0.23, 0.11])
    hits = [
        _hit(0.9, chunk_id=1, content="他社の提案", title="花王_提案書.pptx"),
        _hit(0.9, chunk_id=2, content="他社の報告", title="ライオン報告.pdf"),
    ]
    out = _prod_skill(bedrock, _pg(hits)).run(
        SearchInput(query="東芝の半導体事業の提案について"), SkillContext(metadata={})
    )
    bedrock.converse.assert_not_called()
    assert out.found is False
    assert out.answer == f"{NOT_FOUND_HEAD}（近いもの: 『花王_提案書.pptx』『ライオン報告.pdf』）。"
    assert out.total_cost_usd == 0.0
    assert not out.answer.startswith(WEAK_RESULT_NOTICE)
    # 近いものは参考として hits に残す（knowledge_deliver 等の下流は従来どおり score で判断）。
    assert [h.chunk_id for h in out.hits] == [1, 2]


def test_prod_real_hit_still_summarizes() -> None:
    bedrock = MagicMock()
    bedrock.converse.return_value = _converse()
    bedrock.rerank.return_value = _rerank([0.86])
    hits = [_hit(0.9, chunk_id=1, content="日本ガイシ ケイパ提案", client_name="日本ガイシ")]
    out = _prod_skill(bedrock, _pg(hits, vocab=["日本ガイシ"])).run(
        SearchInput(query="日本ガイシのケイパ提案"), SkillContext(metadata={})
    )
    bedrock.converse.assert_called_once()
    assert out.found is True
    assert out.answer == "もっともらしい要約"


def test_zero_hits_answer_and_found_false() -> None:
    bedrock = MagicMock()
    out = _prod_skill(bedrock, _pg([])).run(SearchInput(query="何か"), SkillContext(metadata={}))
    assert out.found is False
    assert out.answer == f"{NOT_FOUND_HEAD}。"
    bedrock.converse.assert_not_called()


def test_client_mismatch_via_vocabulary_is_not_found() -> None:
    bedrock = MagicMock()
    bedrock.converse.return_value = _converse()
    bedrock.rerank.return_value = _rerank([0.81])
    hits = [_hit(0.9, chunk_id=1, content="本文", client_name="花王")]
    out = _prod_skill(bedrock, _pg(hits, vocab=["花王", "資生堂"])).run(
        SearchInput(query="資生堂の提案書ある？"), SkillContext(metadata={})
    )
    assert out.found is False
    assert out.answer.startswith(NOT_FOUND_HEAD)
    bedrock.converse.assert_not_called()


def test_self_org_name_is_not_treated_as_client() -> None:
    bedrock = MagicMock()
    bedrock.converse.return_value = _converse()
    bedrock.rerank.return_value = _rerank([0.8])
    hits = [_hit(0.9, chunk_id=1, content="採用向け資料", client_name="花王")]
    out = _prod_skill(bedrock, _pg(hits, vocab=["NewsTV", "花王"])).run(
        SearchInput(query="NewsTV の採用向け資料"), SkillContext(metadata={})
    )
    assert out.found is True


def test_not_found_does_not_offer_file_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_KNOWLEDGE_DELIVER", "true")
    bedrock = MagicMock()
    bedrock.rerank.return_value = _rerank([0.1])
    hits = [_hit(0.9, chunk_id=1, content="資料 a.pdf", source_type="gsheets", title="行")]
    pg = _pg(hits)
    pg.resolve_file_urls_by_titles.return_value = {"a.pdf": "https://drive.google.com/x"}
    out = _prod_skill(bedrock, pg).run(SearchInput(query="何か"), SkillContext(metadata={}))
    assert out.found is False
    assert "📎" not in out.answer


def test_two_stage_is_skipped_when_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TWO_STAGE_ENV, "true")
    bedrock = MagicMock()
    bedrock.rerank.return_value = _rerank([0.1])
    skill = _prod_skill(bedrock, _pg([_hit(0.9, chunk_id=1, title="花王")]))
    called: list[bool] = []
    monkeypatch.setattr(skill, "deliver_followup_answer", lambda **_: called.append(True))
    out = skill.run(
        SearchInput(query="何か"),
        SkillContext(metadata={TWO_STAGE_CTX_KEY: True, "channel_id": "C1", "thread_ts": "1.0"}),
    )
    assert out.answer.startswith(NOT_FOUND_HEAD)
    assert called == []


def test_fast_path_keeps_empty_answer_but_reports_found() -> None:
    bedrock = MagicMock()
    bedrock.rerank.return_value = _rerank([0.1])
    out = _prod_skill(bedrock, _pg([_hit(0.9, chunk_id=1)])).run(
        SearchInput(query="何か", include_answer=False), SkillContext(metadata={})
    )
    assert out.answer == ""
    assert out.found is False


def test_kill_switch_restores_summary_on_weak_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_NOT_FOUND_ANSWER", "false")
    bedrock = MagicMock()
    bedrock.converse.return_value = _converse()
    bedrock.rerank.return_value = _rerank([0.1])
    pg = _pg([_hit(0.9, chunk_id=1, title="花王")], vocab=["花王"])
    out = _prod_skill(bedrock, pg).run(SearchInput(query="何か"), SkillContext(metadata={}))
    bedrock.converse.assert_called_once()
    assert out.found is True  # 判定を止めたときは件数だけで決める
    assert out.answer.startswith(WEAK_RESULT_NOTICE)


def test_threshold_is_env_tunable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_NOT_FOUND_THRESHOLD", "0.1")
    monkeypatch.setenv("SEARCH_NOT_FOUND_SUBJECT_BELOW", "0")
    bedrock = MagicMock()
    bedrock.converse.return_value = _converse()
    bedrock.rerank.return_value = _rerank([0.18])
    out = _prod_skill(bedrock, _pg([_hit(0.9, chunk_id=1)])).run(
        SearchInput(query="何か"), SkillContext(metadata={})
    )
    assert out.found is True
    bedrock.converse.assert_called_once()
