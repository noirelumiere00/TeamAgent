"""件数は取得済み候補の上限。架空資料だけで一覧・fallback・添付の接続を確認する。"""

from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from teamagent.mcp_gateway.search_file_delivery import candidates
from teamagent.skills._shared.input_policy import requested_item_count
from teamagent.skills.search import compact, fanout, material_files
from teamagent.skills.search.answer_mode import classify_answer_mode, requested_count
from teamagent.skills.search.case_list import render
from teamagent.skills.search.composite import SlackItem, SlackLookup
from teamagent.skills.search.not_found import case_query_target
from teamagent.skills.search.schema import SlackHitOut
from tests.skills.search.test_fanout import META, _vault_hit, make, run
from tests.skills.search.test_fanout import setup as setup


def _hits():
    hits = [
        replace(
            _vault_hit(
                i,
                title=f"架空飲料資料{i}",
                source_uri=f"gdrive://COUNT{i}",
                document_id=f"count-{i}",
                cls_industry="飲料",
                **({"case_corpus": "true", "case_product": f"架空飲料{i}"} if i <= 3 else {}),
            ),
            content="架空の飲料の紹介動画。店頭と配信面を連動した。",
        )
        for i in range(1, 7)
    ]
    hits[4] = replace(
        hits[4],
        metadata={
            **hits[4].metadata,
            "source_type": "other",
            "source_uri": "https://example.slack.com/files/U0FAKE/F0COUNT/material.pdf",
            "external_id": "F0COUNT",
        },
    )
    return hits


@pytest.mark.parametrize(
    ("query", "count"),
    [
        ("事例を 5 件見せて", 5),
        ("飲料系の事例を５件並べて", 5),
        ("資料を3つ見せて", 3),
        ("５件の提案書を見せて", 5),
        ("事例を12件", 12),
        ("資料を見せて", None),
        ("2026年の資料", None),
        ("0件の資料", None),
        ("資料を1.5件", None),
        ("資料を-3件", None),
    ],
)
def test_requested_count_input_rule(query, count):
    assert requested_item_count(query) == count
    assert requested_count(query) == count
    if count:
        assert classify_answer_mode(query) == "list"


@pytest.mark.parametrize(
    "query", ["事例3件の共通点を分析して", "資料3件を比較して", "動画3つの再生数は？"]
)
def test_count_does_not_change_analysis_or_metric_questions(query):
    assert requested_count(query) is None


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("drive_mode", [False, True])
@pytest.mark.parametrize("available", [0, 2, 6])
def test_case_count_lists_cases_and_materials_without_asking(
    monkeypatch, fanout_enabled, drive_mode, available
):
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", str(int(drive_mode)))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: SlackLookup("ok", "ok"))
    monkeypatch.setattr(material_files, "file_reader", lambda *a, **kw: None)
    skill, _ = make(_hits()[:available])
    monkeypatch.setattr(skill, "_collect_slack_lookup", lambda *a: SlackLookup("ok", "ok"))
    skill._summarize = MagicMock(side_effect=AssertionError("case list needs no model"))
    out = run(skill, query="飲料系の事例を５件並べて", meta={**META, compact.CTX_KEY: True})
    assert out.found == bool(available)
    assert out.answer.count("https://") == min(available, 5)
    assert ("見つかったのは 2 件です。" in out.answer) == (available == 2)
    assert ("見つかったのは 0 件です。" in out.answer) == (available == 0)
    assert "COUNT6" not in out.answer
    assert "ください" not in out.answer and "？" not in out.answer
    if available >= 5:
        assert candidates(out) == [("F0COUNT", "架空飲料資料5")]
    skill._summarize.assert_not_called()


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("drive_mode", [False, True])
@pytest.mark.parametrize("available", [2, 6])
def test_missing_citations_fallback_count_has_links_and_delivery_candidates(
    monkeypatch, fanout_enabled, drive_mode, available
):
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", str(int(drive_mode)))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: SlackLookup("ok", "ok"))
    monkeypatch.setattr(material_files, "file_reader", lambda *a, **kw: None)
    skill, _ = make(_hits()[:available])
    monkeypatch.setattr(skill, "_collect_slack_lookup", lambda *a: SlackLookup("ok", "ok"))
    skill._summarize = MagicMock(return_value=("架空資料を紹介します。", 0.0))
    with capture_logs() as logs:  # capsys はテスト順で空になる
        out = run(skill, query="飲料資料を５件見せて", meta={**META, compact.CTX_KEY: True})
    assert out.found and out.answer_format == "list"
    assert out.answer.count("https://") == min(available, 5)
    assert "資料URL未確認" not in out.answer
    assert "missing_citations" in str(logs)
    assert "ください" not in out.answer and "？" not in out.answer
    assert ("見つかったのは 2 件です。" in out.answer) == (available == 2)
    if available >= 5:
        assert candidates(out) == [("F0COUNT", "架空飲料資料5")]
    skill._summarize.assert_called_once()


@pytest.mark.parametrize("count", [1, 3, 6, 12])
def test_fallback_cards_use_requested_count_and_skip_duplicate_links(count):
    hits = _hits()
    answer = compact.fallback_cards(
        f"資料を{count}つ見せて", [hits[0], *hits], [], lambda h: str(h.metadata["source_uri"])
    )
    assert answer.count("https://") == min(count, 6)
    assert ("見つかったのは 6 件です。" in answer) == (count > 6)


def test_unspecified_count_keeps_existing_renderer_output():
    hits = _hits()

    def url_of(hit):
        return str(hit.metadata["source_uri"])

    assert compact.fallback_cards("資料を見せて", hits, [], url_of) == render(
        hits, None, url_of, cards=True
    )


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_successful_compact_count_keeps_existing_answer(monkeypatch, fanout_enabled):
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    skill, _ = make(_hits()[:2])
    skill._summarize = MagicMock(return_value=("店頭と配信面を連動した。[S:1]", 0.0))
    fallback = MagicMock(side_effect=AssertionError("successful compact needs no fallback"))
    monkeypatch.setattr(compact, "fallback_cards", fallback)
    out = run(skill, query="飲料資料を５件見せて", meta={**META, compact.CTX_KEY: True})
    assert out.answer_format == "compact"
    assert "店頭と配信面を連動した。" in out.answer
    fallback.assert_not_called()


def test_counted_industry_suffix_uses_industry_without_changing_unspecified_query():
    assert case_query_target("飲料系の事例を５件並べて") == "飲料"
    assert case_query_target("飲料系の事例") == "飲料系"


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("drive_mode", [False, True])
def test_case_count_combines_three_case_rows_with_material_and_live_slack(
    monkeypatch, fanout_enabled, drive_mode
):
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", str(int(drive_mode)))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setattr(material_files, "file_reader", lambda *a, **kw: None)
    slack_url = "https://example.slack.com/archives/C0FAKE/p1784424000000001"
    lookup = SlackLookup(
        "ok",
        "ok",
        items=(
            SlackItem(
                hit=SlackHitOut(
                    permalink=slack_url,
                    file_ids=["F0LIVE"],
                    file_names=["架空飲料提案.pdf"],
                ),
                text="架空飲料の提案資料を共有。",
            ),
        ),
    )
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: lookup)
    skill, pg = make([_hits()[3]])
    monkeypatch.setattr(skill, "_collect_slack_lookup", lambda *a: lookup)
    pg.list_case_studies.return_value = [
        {
            "title": f"架空飲料事例{i}",
            "source_uri": "https://docs.google.com/presentation/d/COUNTDECK/edit",
            "case_client": f"架空飲料社{i}",
            "case_product": f"架空ドリンク{i}",
            "case_effect": "飲料の紹介動画。",
            "case_source": "deck",
            "case_external_use": "unknown",
            "case_industry": "飲料",
        }
        for i in range(1, 4)
    ]
    skill._summarize = MagicMock(side_effect=AssertionError("case list needs no model"))
    out = run(skill, query="飲料系の事例を５件並べて", meta={**META, compact.CTX_KEY: True})
    assert out.found and out.answer.count("https://") == 5
    assert out.answer.count("COUNTDECK") == 3
    assert candidates(out) == [("F0LIVE", "架空飲料提案.pdf")]
    assert "見つかったのは" not in out.answer
    skill._summarize.assert_not_called()
