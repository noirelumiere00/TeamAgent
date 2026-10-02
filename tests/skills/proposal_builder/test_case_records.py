"""proposal_builder × 事例レコード: 候補化・差し込み順・重複除去・USE_CASE_RECORDS フラグ。

変異テスト（赤くなることを確認済み）:
- selectors.merge_case_candidates で record 側を末尾に足す → test_merge_prepends_records_and_dedupes が赤
- skill._collect_case_record_candidates の env ゲートを外す → test_flag_off_never_calls_searcher が赤
"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.cases.schema import CaseRecord
from teamagent.cases.store import CaseSearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.proposal_builder import skill as builder_module
from teamagent.skills.proposal_builder.schema import (
    ProductMeta,
    ProposalBuilderCaseReference,
    ProposalBuilderInput,
)
from teamagent.skills.proposal_builder.selectors import (
    CaseCandidate,
    case_candidate_from_hit,
    merge_case_candidates,
    product_meta_traits,
    search_case_record_candidates,
)
from teamagent.skills.proposal_builder.skill import ProposalBuilderSkill
from teamagent.skills.proposal_deck.schema import ProposalDeckInput, ProposalDeckOutput


def _record(case_id: str, url: str, **overrides: Any) -> CaseRecord:
    base: dict[str, Any] = {
        "case_id": case_id,
        "case_group": "g",
        "client_internal": "カネカ",
        "client_masked": "機能性食品メーカー様",
        "sector": "飲料食品",
        "purpose": ["認知拡大"],
        "product_state": "新商品",
        "channel": [],
        "traits": ["検証型"],
        "product": "Q10グミ",
        "scale": "",
        "period": {"start": "", "end": ""},
        "metrics": [{"name": "再生目標比", "value": "130%", "unit": "", "source_url": url}],
        "result_masked": "目標比130%前後の再生",
        "winpattern": "実演×テンポ",
        "competitors": [],
        "external_use": "ok",
        "sources": [{"external_id": "e", "url": url, "excerpt": "x"}],
        "confidence": 0.9,
        "reviewed": True,
    }
    base.update(overrides)
    return CaseRecord.model_validate(base)


def _hit(record: CaseRecord, score: int = 6) -> CaseSearchHit:
    return CaseSearchHit(record=record, structure_score=score, similarity=0.5)


def _meta(**overrides: Any) -> ProductMeta:
    base: dict[str, Any] = {
        "sector": "飲料食品",
        "purpose": ["認知拡大", "売上・POS"],
        "product_state": "新商品",
        "channel": ["店頭小売"],
        "regulation": True,
        "moment": "春",
        "target_categories": ["学生"],
        "kaiwai_keywords": ["集中"],
    }
    base.update(overrides)
    return ProductMeta.model_validate(base)


class _FakeRepo:
    def __init__(self, hits: list[CaseSearchHit]) -> None:
        self.hits = hits
        self.calls: list[dict[str, Any]] = []

    def search_case_records(self, **kwargs: Any) -> list[CaseSearchHit]:
        self.calls.append(kwargs)
        return self.hits


# ── 候補化 ────────────────────────────────────────────────────


def test_candidate_projection_uses_masked_title_and_cited_metrics() -> None:
    url = "https://drive.google.com/file/d/abc/view"
    candidate = case_candidate_from_hit(_hit(_record("a#1", url), score=7))
    assert candidate is not None
    assert candidate.source == "case_record"
    assert candidate.title == "機能性食品メーカー様｜Q10グミ"
    assert candidate.url == url
    assert "結果: 目標比130%前後の再生" in candidate.excerpt
    assert "勝ち筋: 実演×テンポ" in candidate.excerpt
    assert f"再生目標比 130%（出典: {url}）" in candidate.excerpt
    assert "カネカ" not in candidate.title and "カネカ" not in candidate.excerpt
    assert candidate.score == 1.0


def test_candidate_marks_unknown_external_use_and_drops_ng() -> None:
    unknown = case_candidate_from_hit(_hit(_record("a#1", "https://d/1", external_use="unknown")))
    assert unknown is not None and "対外利用可否: 未確認" in unknown.excerpt
    assert case_candidate_from_hit(_hit(_record("a#2", "https://d/2", external_use="ng"))) is None


def test_candidate_requires_http_source_url() -> None:
    assert case_candidate_from_hit(_hit(_record("a#1", "gdrive://abc"))) is None


def test_search_passes_product_meta_and_regulation_trait() -> None:
    repo = _FakeRepo([_hit(_record("a#1", "https://d/1")), _hit(_record("a#2", "https://d/2"))])
    candidates = search_case_record_candidates(repo, _meta(), max_cases=2, exclude_client="カネカ")
    assert [c.url for c in candidates] == ["https://d/1", "https://d/2"]
    call = repo.calls[0]
    assert call["sector"] == "飲料食品"
    assert call["purpose"] == ["認知拡大", "売上・POS"]
    assert call["product_state"] == "新商品"
    assert call["traits"] == ["薬機・景表規制"]
    assert call["exclude_client"] == "カネカ"
    assert call["limit"] == 6
    assert product_meta_traits(_meta(regulation=False)) == []


def test_search_dedupes_same_url_and_caps_at_max_cases() -> None:
    repo = _FakeRepo(
        [
            _hit(_record("a#1", "https://d/1")),
            _hit(_record("a#1b", "https://d/1")),
            _hit(_record("a#2", "https://d/2")),
            _hit(_record("a#3", "https://d/3")),
        ]
    )
    candidates = search_case_record_candidates(repo, _meta(), max_cases=2)
    assert [c.url for c in candidates] == ["https://d/1", "https://d/2"]


def test_search_rejects_more_than_two_slots() -> None:
    with pytest.raises(ValueError):
        search_case_record_candidates(_FakeRepo([]), _meta(), max_cases=3)


def test_search_uses_store_when_given_a_connection() -> None:
    class _Cursor:
        def __init__(self, owner: Any) -> None:
            self.owner = owner

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def execute(self, sql: str, params: Any) -> None:
            self.owner.sql.append(sql)

        def fetchall(self) -> list[dict[str, Any]]:
            return []

    class _Conn:
        def __init__(self) -> None:
            self.sql: list[str] = []

        def cursor(self) -> _Cursor:
            return _Cursor(self)

    conn = _Conn()
    assert search_case_record_candidates(conn, _meta(), max_cases=1) == []
    assert conn.sql and "FROM case_records" in conn.sql[0]


# ── 差し込み ──────────────────────────────────────────────────


def _cand(source: str, url: str) -> CaseCandidate:
    return CaseCandidate(source=source, title=f"t-{url}", url=url, excerpt="e", score=0.5)  # type: ignore[arg-type]


def test_merge_prepends_records_and_dedupes() -> None:
    records = [
        _cand("case_record", "https://d/1"),
        _cand("case_record", "https://d/2"),
        _cand("case_record", "https://d/3"),
    ]
    rag = [_cand("report_rag", "https://d/2"), _cand("general_news-tv", "https://d/9")]
    merged = merge_case_candidates(records, rag, max_records=2)
    assert [(c.source, c.url) for c in merged] == [
        ("case_record", "https://d/1"),
        ("case_record", "https://d/2"),
        ("general_news-tv", "https://d/9"),
    ]


def test_merge_without_records_keeps_rag_untouched() -> None:
    rag = [_cand("report_rag", "https://d/2")]
    assert merge_case_candidates([], rag) == rag


def test_case_reference_accepts_case_record_source() -> None:
    ref = ProposalBuilderCaseReference(source="case_record", title="t", url="https://d/1")
    assert ref.source == "case_record"


# ── skill のフラグ ────────────────────────────────────────────


def _research(brand: str = "ACME") -> dict[str, Any]:
    return {
        "research_date": "2026-08-04",
        "brand": brand,
        "product_meta": {
            "sector": "飲料食品",
            "purpose": ["認知拡大"],
            "product_state": "新商品",
            "channel": ["店頭小売"],
            "regulation": False,
            "moment": "秋",
            "target_categories": ["学生"],
            "kaiwai_keywords": ["集中"],
        },
        "A_market_data": [
            {
                "theme": "市場",
                "headline": "需要がある",
                "analysis": "利用場面が広い",
                "url": "https://example.com/market",
                "source_name": "市場資料",
                "alt_data": [],
            }
        ],
        "B_social_trend": [
            {
                "theme": "潮流",
                "headline": "短尺動画が好相性",
                "analysis": "生活者が投稿を参考にする",
                "url": "https://example.com/social",
                "source_name": "潮流資料",
                "alt_data": [],
            }
        ],
        "C_tiktok": [
            {
                "related_tag": "集中",
                "representative_post_url": "https://www.tiktok.com/@seed/video/seed",
                "search_demand_note": "検索需要は実測前",
                "total_count": "取得不可（UI非表示）",
            }
        ],
        "D_publicity": [
            {
                "trend_word": "集中",
                "article_count_500days": "要確認",
                "evidence_url": "https://example.com/publicity",
                "recommended_media": ["生活情報"],
            }
        ],
        "E_community": [
            {
                "name": "集中界隈",
                "estimated_population": "要確認",
                "calculation": "要確認",
                "data_url": "https://example.com/community",
                "tiktok_tags": [
                    {
                        "tag": "集中",
                        "representative_post_url": "https://www.tiktok.com/@seed/video/community",
                    }
                ],
            }
        ],
        "F_competitor": [
            {
                "name": "競合",
                "target": "一般生活者",
                "core_concept": "日常利用",
                "features": "手軽さ",
                "positioning": "身近",
                "url": "https://example.com/competitor",
            }
        ],
        "G_insight": {
            "complaint_pattern": "続けにくい",
            "complaint_example": "習慣化したい",
            "desire_pattern": "手軽に使いたい",
            "desire_example": "日常に取り入れたい",
        },
        "H_event": {
            "overview": "体験イベント",
            "scale": "全国",
            "sns_reality": "投稿と相性が良い",
            "benchmark_case": "体験型企画",
            "url": "https://example.com/event",
        },
    }


class _CapturingDeck:
    def __init__(self) -> None:
        self.inputs: list[ProposalDeckInput] = []

    def run(self, input: ProposalDeckInput, ctx: SkillContext) -> ProposalDeckOutput:
        del ctx
        self.inputs.append(input)
        return ProposalDeckOutput(
            pptx_path="/tmp/fake-proposal.pptx",
            version_id="v-test",
            filled_count=95,
            skipped_count=0,
            coverage_ratio=1.0,
            skipped_ids=[],
            total_cost_usd=0.0,
        )

    def cleanup_output(self, output: ProposalDeckOutput) -> None:
        del output


_RAG_URL = "https://www.tiktok.com/@x/video/1"
_RECORD_URL = "https://prtimes.jp/main/html/rd/p/1.html"


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROPOSAL_BUILDER_TEMPLATE_PATH", "/tmp/template.pptx")
    monkeypatch.delenv("PROPOSAL_BUILDER_DELIVER_INTERNAL_DRAFTS", raising=False)
    monkeypatch.delenv("PROPOSAL_BUILDER_PUBLISH_READY", raising=False)
    monkeypatch.setattr(builder_module, "load_and_select_accounts", lambda *_a, **_kw: [])
    monkeypatch.setattr(
        builder_module,
        "search_case_candidates",
        lambda *_a, **_kw: [
            CaseCandidate(
                source="report_rag", title="RAG事例", url=_RAG_URL, excerpt="rag", score=0.4
            ),
            CaseCandidate(
                source="report_rag", title="重複事例", url=_RECORD_URL, excerpt="dup", score=0.3
            ),
        ],
    )
    monkeypatch.setattr(
        builder_module.MediaJobClient, "is_configured", classmethod(lambda cls: False)
    )


def _skill(searcher: Any) -> tuple[ProposalBuilderSkill, _CapturingDeck]:
    deck = _CapturingDeck()
    skill = ProposalBuilderSkill(
        search=object(),
        deck=deck,  # type: ignore[arg-type]
        account_db_path="unused.json",
        tiktok_searcher=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no tiktok")),
        campaign_factory=lambda s: (_ for _ in ()).throw(AssertionError("no campaign")),
        case_record_searcher=searcher,
    )
    return skill, deck


def _input() -> ProposalBuilderInput:
    return ProposalBuilderInput(gemini_json=_research(), posting_start_date="2026-09-01")


def test_flag_off_never_calls_searcher(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    monkeypatch.delenv("USE_CASE_RECORDS", raising=False)
    calls: list[Any] = []

    def searcher(*args: Any, **kwargs: Any) -> list[CaseCandidate]:
        calls.append((args, kwargs))
        return [
            CaseCandidate(
                source="case_record", title="レコード事例", url=_RECORD_URL, excerpt="r", score=0.9
            )
        ]

    skill, deck = _skill(searcher)
    output = skill.run(_input(), SkillContext(request_id="flag-off"))
    assert calls == []
    material = deck.inputs[0].research_material
    assert "1. RAG事例" in material and "レコード事例" not in material
    assert all(ref.source != "case_record" for ref in output.case_references)


def test_flag_on_prepends_records_and_dedupes_by_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setenv("USE_CASE_RECORDS", "1")
    calls: list[dict[str, Any]] = []

    def searcher(
        product_meta: Any, *, max_cases: int, exclude_client: str | None
    ) -> list[CaseCandidate]:
        calls.append(
            {
                "sector": product_meta.sector,
                "max_cases": max_cases,
                "exclude_client": exclude_client,
            }
        )
        return [
            CaseCandidate(
                source="case_record", title="レコード事例", url=_RECORD_URL, excerpt="r", score=0.9
            )
        ]

    skill, deck = _skill(searcher)
    output = skill.run(_input(), SkillContext(request_id="flag-on"))
    assert calls == [{"sector": "飲料食品", "max_cases": 2, "exclude_client": "ACME"}]
    material = deck.inputs[0].research_material
    assert "1. レコード事例" in material
    assert "2. RAG事例" in material
    assert "重複事例" not in material  # 同じ URL の RAG 候補は落ちる
    assert material.index("レコード事例") < material.index("RAG事例")
    assert [ref.source for ref in output.case_references] == ["case_record", "report_rag"]


def test_flag_on_searcher_failure_is_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    monkeypatch.setenv("USE_CASE_RECORDS", "true")

    def searcher(*_a: Any, **_k: Any) -> list[CaseCandidate]:
        raise RuntimeError("db down")

    skill, deck = _skill(searcher)
    skill.run(_input(), SkillContext(request_id="flag-fail"))
    assert "1. RAG事例" in deck.inputs[0].research_material
