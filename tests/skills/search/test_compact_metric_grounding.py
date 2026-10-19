"""再生中央値と保存合計を、モデルが同一指標のように比較しない。"""

import pytest

from teamagent.skills.search import fanout
from teamagent.skills.search.compact import render
from tests.skills.search.test_comparison import _campaign
from tests.skills.search.test_fanout import META, _converse, make, run
from tests.skills.search.test_fanout import setup as setup


def test_model_cannot_compare_retrieved_median_plays_against_total_saves():
    answer = render(
        "中央値153,300回再生に対し保存261が多く効きました。[S:1]\n"
        "投稿文は調理アレンジを紹介しています。[S:1]",
        {
            "1": (
                "施策シート",
                "https://example.test/sheet",
                "再生中央値153,300回再生。保存合計261。調理アレンジ",
            )
        },
    )
    assert "153,300" not in answer and "261" not in answer
    assert "調理アレンジ" in answer
    assert "https://example.test/sheet" in answer


def test_compact_campaign_comparison_inserts_only_code_generated_matching_metrics(monkeypatch):
    hits = [
        _campaign(
            1,
            "施策A",
            "再生数: 合計 1,230,000、平均 205,000、中央値 153,300\n反応: 保存 合計 261、平均 43.5",
        ),
        _campaign(2, "施策B", "再生数: 合計 685,300、平均 171,325\n反応: 保存 合計 110、平均 27.5"),
    ]
    for hit in hits:
        hit.metadata["source_uri"] = f"gdrive://CAMPAIGN{hit.chunk_id}"
    skill, _ = make(hits)
    monkeypatch.setattr(skill, "_attach_campaign_files", lambda *a: None)
    skill._bedrock.converse.return_value = _converse("調理の手軽さを紹介しています。[S:1]")
    monkeypatch.setattr(fanout, "build_summary_client", lambda: skill._bedrock)
    out = run(
        skill,
        query="施策Aと施策Bを比較して何が効いた？",
        meta={**META, "_search_compact_answer": True},
    )
    assert "205,000" in out.answer and "171,325" in out.answer
    assert "再生平均" in out.answer and "施策Aが上" in out.answer
    assert "153,300" not in out.answer
    assert out.answer.count("https://") == 2


@pytest.mark.parametrize(
    "claim",
    [
        "再生数は153,300回に対し、保存数は261件です。",
        "平均再生数は153,300回で、保存数261件より効果的です。",
        "再生については153,300回、保存については261件です。",
        "投稿は5本で、合計153,300回の視聴を集めました。",
        "１５３，３００回の再生数に比べ保存数は２６１件です。",
    ],
)
def test_model_quantitative_metric_prose_is_rejected_even_when_numbers_are_retrieved(claim):
    answer = render(
        claim + "[S:1]\n調理アレンジを紹介しています。[S:1]",
        {
            "1": (
                "施策シート",
                "https://example.test/sheet",
                "投稿5本。再生中央値153,300回。保存合計261件。調理アレンジ。",
            )
        },
    )
    assert claim not in answer
    assert "調理アレンジを紹介" in answer


@pytest.mark.parametrize(
    "date", ["2026-10-19", "2026年10月19日", "10/19", "10月19日 13:38", "10/19 13:38"]
)
def test_compact_preserves_dated_status_without_metric_values(date):
    claim = f"{date}時点の資料では再生を増やす施策が記録されています。"
    answer = render(
        claim + "[S:1]",
        {"1": ("施策資料", "https://example.test/sheet", claim)},
    )
    assert claim in answer


@pytest.mark.parametrize(
    "claim",
    [
        "再生数は1/2回に対し保存数は3/4件です。",
        "再生数は１／２回に対し保存数は３／４件です。",
        "再生数は一万回に対し保存数は二百六十一件です。",
        "保存の合計は二百六十一です。",
        "投稿は二本で、再生は一万です。",
    ],
)
def test_model_fractions_and_kanji_quantities_cannot_bypass_code_metrics(claim):
    answer = render(
        claim + "[S:1]\n調理アレンジを紹介しています。[S:1]",
        {"1": ("施策資料", "https://example.test/sheet", "再生1 2。保存3 4。調理アレンジ。")},
    )
    assert claim not in answer
    assert "調理アレンジを紹介" in answer


def test_compact_preserves_a_year_in_a_metric_related_document_title():
    claim = "2026年版の再生改善施策には調理アレンジが記録されています。"
    answer = render(
        claim + "[S:1]",
        {"1": ("再生改善施策_2026年版", "https://example.test/sheet", claim)},
    )
    assert claim in answer
