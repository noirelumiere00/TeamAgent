"""r68 の Drive 優先の穴を、外部サービスなしで再現する。"""

from dataclasses import replace
from unittest.mock import MagicMock

import pytest
import structlog.testing

from teamagent.skills.base import SkillContext
from teamagent.skills.search import fanout
from teamagent.skills.search.composite import SlackItem, SlackLookup
from teamagent.skills.search.schema import DocumentReference, SearchInput, SlackHitOut
from tests.skills.search.test_drive_only_case_list import _case
from tests.skills.search.test_fanout import META, make
from tests.skills.search.test_fanout import setup as setup


@pytest.fixture(autouse=True)
def drive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: SlackLookup("ok", "ok"))


def test_fanout_entry_widens_vault_pool_and_preserves_rls() -> None:
    hits = [_case(i) for i in range(1, 31)]
    skill, pg = make(hits)
    skill._use_cohere_rerank = False
    pg.search_similar_new_schema.side_effect = lambda **kw: hits[: kw["limit"]]
    out = fanout.run_fanout(
        skill, SearchInput(query="食品の事例出して", top_k=1), SkillContext(metadata=META)
    )
    assert out.answer.count("|資料を見る>") == 5
    assert pg.search_similar_new_schema.call_args.kwargs["limit"] == 30
    assert pg.connection.call_args.kwargs["user_email"] == META["user_email"]


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("explicit_filter", [False, True])
def test_industry_applies_to_all_card_candidates(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool, explicit_filter: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    hits = [
        _case(1),
        _case(2, cls_industry="化粧品"),  # 本文に食品とあっても既知の別業種は不可。
        _case(3, cls_industry=None),  # 未分類でも食品を明記すれば残す。
        replace(_case(4, title="分類なし", cls_industry=None), content="動画施策を実施。"),
    ]
    skill, _ = make(hits)
    skill._summarize = MagicMock(return_value=("", 0.0))
    query = "事例を一覧で" if explicit_filter else "事例を一覧で、食品メーカーに絞って"
    out = skill.run(
        SearchInput(query=query, filter_industry="食品" if explicit_filter else None),
        SkillContext(metadata=META),
    )
    assert [ref.drive_file_id for ref in out.displayed_refs] == ["FILE1", "FILE3", "FILE4"]
    assert "食品会社2" not in out.answer


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("has_drive", [False, True])
def test_case_list_envelope_contains_no_unused_slack_candidates(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool, has_drive: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    post = "https://example.slack.com/archives/CFOOD/p1234567890123456"
    item = SlackItem(SlackHitOut(channel="#shared", permalink=post), "食品の追加候補会社の商品")
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: SlackLookup("ok", "ok", (item,)))
    skill, _ = make(
        [*([_case(1)] if has_drive else []), _case(9, source_type="slack", source_uri=post)]
    )
    # 通常経路にも同じ取得済みSlack候補を渡す。
    skill._start_slack_lookup = MagicMock(return_value=MagicMock())
    skill._collect_slack_lookup = MagicMock(return_value=SlackLookup("ok", "ok", (item,)))
    out = skill.run(SearchInput(query="食品の事例出して"), SkillContext(metadata=META))
    assert not out.slack_hits
    assert post not in out.model_dump_json()
    assert "追加候補会社" not in out.model_dump_json()
    if not has_drive:
        assert out.answer == "Drive に資料のある事例は見つかりませんでした。"
        assert not out.found and out.displayed_refs == []


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize(
    "query", ["TikTokの保存率を上げるコツは？", "食品会社Aと食品会社Bの事例を比べて"]
)
def test_normal_fallback_links_only_named_used_drive_materials(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool, query: str
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    hits = [
        _case(1, title="食品会社Aの提案", client_name="食品会社A"),
        _case(2, title="食品会社Bの提案", case_product="商品ベータ"),
        _case(3, title="第三の動画資料.pdf"),
        _case(4, title="未使用資料"),
        _case(5, title="食品会社Aの提案の重複", source_uri="gdrive://FILE1"),
        _case(6, title="第四の動画資料"),
        _case(
            7,
            title="Slack投稿",
            source_type="slack",
            source_uri="https://example.slack.com/archives/C1/p1234567890123456",
        ),
        _case(
            8,
            title="保管台帳",
            source_type="gsheets",
            source_uri="https://docs.google.com/spreadsheets/d/SHEET/edit",
        ),
    ]
    skill, pg = make(hits)
    pg.list_authorized_document_references.return_value = [
        {"source_type": "gdrive", "external_id": f"FILE{i}"} for i in range(1, 7)
    ]
    skill._use_cohere_rerank = False
    normal = "食品会社Aの提案では商品ベータを紹介。第三の動画資料と第四の動画資料、Slack投稿、保管台帳も参照した。"
    skill._summarize = MagicMock(side_effect=[("", 0.0), (normal, 0.0)])
    out = skill.run(SearchInput(query=query, top_k=30), SkillContext(metadata=META))
    assert out.answer.startswith(normal)
    assert [ref.drive_file_id for ref in out.displayed_refs] == ["FILE1", "FILE2", "FILE3"]
    assert out.answer.count("|資料を見る>") == 3
    assert out.answer_format is None
    assert "/d/FILE4/" not in out.answer and "/d/FILE6/" not in out.answer
    assert "slack.com" not in out.answer and "spreadsheets" not in out.answer


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize(
    "raw,reason",
    [
        ("", "empty_summary"),
        ("記録があります。", "missing_citations"),
        ("記録です。[S:999]", "unknown_citation"),
        ("保存率は900%です。[S:1]", "metric_prose"),
    ],
)
def test_compact_fallback_logs_reason(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool, raw: str, reason: str
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    skill, _ = make([_case(1)])
    skill._summarize = MagicMock(side_effect=[(raw, 0.0), ("状況を確認しました。", 0.0)])
    with structlog.testing.capture_logs() as logs:
        skill.run(SearchInput(query="保存率を上げるコツは？"), SkillContext(metadata=META))
    events = [event for event in logs if event["event"] == "search_compact_fallback"]
    assert len(events) == 1 and events[0]["reason"] == reason


@pytest.mark.parametrize(
    "body", ["食品会社ABの商品です。", "資料を使いました。", "https://example.test/食品会社A"]
)
def test_normal_links_require_clear_body_names(body: str) -> None:
    from teamagent.skills.search.compact import append_used_drive_links

    refs: list[DocumentReference] = []
    answer = append_used_drive_links(
        body,
        [_case(1, title="食品会社A"), _case(2, title="資料")],
        refs,
    )
    assert answer == body and refs == []


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_hit_only_request_keeps_candidates(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    skill, _ = make([_case(1)])
    out = skill.run(
        SearchInput(query="食品の事例出して", include_answer=False), SkillContext(metadata=META)
    )
    assert out.answer == "" and out.hits
