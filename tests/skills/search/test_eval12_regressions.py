"""eval12: 本人RLSで見えた事例原本・追質問・最終文の回帰（外部接続なし）。"""

import json
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.search import case_list, compact, drive_first
from teamagent.skills.search.not_found import case_query_target
from teamagent.skills.search.schema import SearchInput, SearchOutput
from tests.skills.search.test_drive_only_case_list import _case
from tests.skills.search.test_fanout import META, make
from tests.skills.search.test_fanout import setup as setup


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("visible", [False, True])
def test_other_case_original_is_reauthorized_by_document_and_source(
    monkeypatch, fanout_enabled, visible
):
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setattr(
        drive_first, "resolve_saved_files", lambda skill, hits, ctx: [None] * len(hits)
    )
    hit = _case(1, source_type="other", title="青空社の事例", client_name="青空社")
    skill, pg = make([hit])
    # case deck の external_id は fileId ではなく fileId:case:hash。
    row = {
        "document_id": "doc-1",
        "source_type": "other",
        "external_id": "FILE1:case:hash",
        "source_uri": "gdrive://FILE1",
    }

    def lookup(conn, **kw):
        return [row] if visible and "doc-1" in kw.get("document_ids", []) else []

    pg.list_authorized_document_references.side_effect = lookup
    skill._summarize = MagicMock(side_effect=[("", 0.0), ("青空社の事例です。[S:1]", 0.0)])
    out = skill.run(SearchInput(query="青空社について教えて"), SkillContext(metadata=META))
    assert ("<https://drive.google.com/file/d/FILE1/view|資料を見る>" in out.answer) == visible
    assert bool(out.displayed_refs) == visible
    assert pg.connection.call_args.kwargs["user_email"] == META["user_email"]


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_list_followup_keeps_case_target_without_client_vocabulary(monkeypatch, fanout_enabled):
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    skill, pg = make([_case(1, title="青空社の事例"), _case(2, title="若葉社の提案書")])
    pg.list_client_names.return_value = []
    out = skill.run(
        SearchInput(query="一覧で出して\n青空社の事例ある？"), SkillContext(metadata=META)
    )
    assert "青空社" in out.answer and "若葉社" not in out.answer
    assert [r.drive_file_id for r in out.displayed_refs] == ["FILE1"]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("一覧で出して\n青空社の事例ある？", "青空社"),
        ("若葉社の事例を一覧で\n青空社の事例ある？", "若葉社"),
        ("一覧で出して\n若葉社の事例ある？\n青空社の事例ある？", "青空社"),
    ],
)
def test_case_target_uses_current_or_latest_context(query, expected):
    assert case_query_target(query) == expected


def test_case_details_ignore_copyright_and_page_numbers():
    hit = replace(
        _case(1),
        content="©Vector inc. All rights reserved.\n12\nPage 12 / 30\n調理実演を配信。\n利用場面を紹介。",
    )
    text = case_list.render([hit], None, lambda h: None, cards=True)
    assert "©" not in text and "rights reserved" not in text and "Page 12" not in text
    assert "• 12" not in text
    assert "調理実演を配信。" in text and "利用場面を紹介。" in text


@pytest.mark.parametrize(
    "tail",
    [
        "資料が必要でしたら、お知らせください。",
        "詳しい内容をご覧になりたい場合は、お知らせください。",
        "何か他に知りたいことはありますか？",
    ],
)
def test_drive_final_removes_invitation_and_unsolicited_question(tail):
    text = compact.strip_linkless_invitation("配信サービスです。" + tail)
    assert "お知らせ" not in text and "ありますか" not in text
    assert text.startswith("配信サービスです。")


def test_empty_displayed_refs_survives_json_and_legacy_absence():
    assert (
        json.loads(
            SearchOutput(answer="説明", displayed_refs=[], total_cost_usd=0).model_dump_json()
        )["displayed_refs"]
        == []
    )
    assert "displayed_refs" not in SearchOutput(answer="説明", total_cost_usd=0).model_dump()


def test_insight_followup_does_not_repeat_metrics_requested_in_previous_turn():
    from tests.skills.search.test_comparison import _campaign

    hit = replace(
        _campaign(
            1,
            "ハンバーグ",
            "調理実演で利用場面を伝えた。\n投稿本数: 1 本。\n再生数: 合計 100、平均 100、最大 100\n上位の投稿（再生数順）:\n1. 再生 100、保存 10、@creator、調理実演 https://example.test/video",
        ),
        metadata={
            **_case(1).metadata,
            "campaign_aggregate": "true",
            "advertiser": "青空社",
            "campaign": "ハンバーグ",
        },
    )
    refs = []
    text = compact.build(
        "調理実演で利用場面を伝えた点が効いた可能性があります。[S:1]",
        [hit],
        (),
        lambda h: "https://drive.google.com/file/d/FILE1/view",
        query="比較して何が効いた？\n青空社のハンバーグとクイックディナーの結果を見せて",
        displayed_refs=refs,
    )
    assert "調理実演" in text and "可能性" in text
    assert "📊" not in text and "再生 100" not in text


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_insight_normal_fallback_preserves_explanation_and_grounding(monkeypatch, fanout_enabled):
    from teamagent.skills.search import fanout
    from tests.skills.search.test_comparison import _campaign
    from tests.skills.search.test_fanout import _converse

    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    hits = [
        replace(
            _campaign(
                i,
                name,
                "投稿本数: 1 本。\n再生数: 合計 100、平均 100、最大 100\n調理実演で利用場面を伝えた。",
            ),
            metadata={
                **_case(i, title=f"青空社 {name}").metadata,
                "campaign_aggregate": "true",
                "advertiser": "青空社",
                "campaign": name,
            },
        )
        for i, name in enumerate(("ハンバーグ", "クイックディナー"), 1)
    ]
    skill, pg = make(hits)
    pg.list_authorized_document_references.return_value = [
        {"source_type": "gdrive", "external_id": "FILE1"}
    ]
    skill._bedrock.converse.side_effect = [
        _converse(""),
        _converse(
            "調理実演で利用場面を伝えた点が効いた可能性があります。[S:1]\n再生数は999999回です。[S:1]"
        ),
    ]
    monkeypatch.setattr(fanout, "build_summary_client", lambda: skill._bedrock)
    out = skill.run(
        SearchInput(
            query="比較して何が効いた？\n青空社のハンバーグとクイックディナーの結果を見せて"
        ),
        SkillContext(metadata=META),
    )
    assert "効いた可能性" in out.answer
    assert "999999" not in out.answer
    assert "資料を見る" in out.answer
