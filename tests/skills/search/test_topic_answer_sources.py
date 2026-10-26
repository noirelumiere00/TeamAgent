"""匿名化した本番の話題質問。外部サービスへの接続は行わない。"""

from unittest.mock import MagicMock

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.search import compact, fanout
from teamagent.skills.search.composite import SlackItem, SlackLookup
from teamagent.skills.search.schema import DocumentReference, SearchInput, SlackHitOut
from tests.skills.search.test_drive_only_case_list import _case
from tests.skills.search.test_fanout import META, make
from tests.skills.search.test_fanout import setup as setup

QUERY = "{newspaper}の{exhibition}って何？うちも関わってる？"
POST = "https://example.slack.com/archives/CPUBLIC/p1792727460000000"


@pytest.fixture(autouse=True)
def drive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: SlackLookup("ok", "ok"))


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("shortened", [False, True])
def test_known_alias_or_unique_short_name_reaches_named_and_karte(
    monkeypatch: pytest.MonkeyPatch, explicit: bool, shortened: bool
) -> None:
    from teamagent.skills.search import client_match

    legal = "株式会社{newspaper}新聞社" if shortened else "株式会社{legal_publisher}"
    monkeypatch.setattr(client_match, "_ALIAS_SEED", (("{newspaper}", "{legal_publisher}"),))
    skill, pg = make([])
    pg.list_client_names.return_value = [legal]
    pg.find_documents_for_advertiser.return_value = []
    skill._summarize = MagicMock(return_value=("", 0.0))
    out = skill.run(
        SearchInput(query=QUERY, filter_client="{newspaper}" if explicit else None),
        SkillContext(metadata=META),
    )
    assert out.query_client == legal
    named = [c.args[1] for c in pg.find_documents_for_advertiser.call_args_list]
    assert legal in named and "{newspaper}" in named
    assert any(
        "{newspaper}新聞社" in c.kwargs["client_name"].replace("\\_", "_")
        if shortened
        else "{legal_publisher}" in c.kwargs["client_name"].replace("\\_", "_")
        for c in pg.list_client_timeline_recent.call_args_list
    )
    assert pg.connection.call_args.kwargs["user_email"] == META["user_email"]


@pytest.mark.parametrize("blocked", ["flag", "email", "identity", "channel"])
def test_alias_resolution_is_limited_to_verified_allowed_dm(
    monkeypatch: pytest.MonkeyPatch, blocked: str
) -> None:
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "0" if blocked == "flag" else "1")
    meta = {**META}
    if blocked == "email":
        monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", "other@example.test")
    if blocked == "identity":
        meta["identity_verified"] = False
    if blocked == "channel":
        meta["channel_id"] = "CPUBLIC"
    skill, _ = make([])
    skill._client_vocabulary = MagicMock(return_value=["株式会社{newspaper}新聞社"])
    probe: dict[str, str] = {}
    skill._retrieve([0.0], SearchInput(query=QUERY), SkillContext(metadata=meta), probe=probe)
    assert "query_client" not in probe


def test_ambiguous_short_name_does_not_guess_a_client() -> None:
    skill, pg = make([])
    pg.list_client_names.return_value = ["{newspaper}新聞社", "{newspaper}広告社"]
    probe: dict[str, str] = {}
    skill._retrieve([0.0], SearchInput(query=QUERY), SkillContext(metadata=META), probe=probe)
    assert "query_client" not in probe


def test_industry_word_does_not_become_a_short_client_name() -> None:
    from teamagent.skills.search.alias_expansion import resolve_topic_client

    assert resolve_topic_client("食品の事例は？", ["{client_a}食品株式会社"]) is None


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_topic_normal_fallback_keeps_only_used_slack_and_drive_sources(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    item = SlackItem(
        SlackHitOut(channel="#public-exhibition", permalink=POST),
        "{exhibition}は展示企画で、当社は動画制作を担当。",
    )
    lookup = SlackLookup("ok", "ok", (item,))
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: lookup)
    skill, pg = make(
        [
            _case(1, title="{newspaper}_{exhibition_b}_効果分析レポート.pptx"),
            _case(2, title="{unused}_提案書.pptx"),
        ]
    )
    pg.list_authorized_document_references.return_value = [
        {"source_type": "gdrive", "external_id": "FILE1"}
    ]
    skill._start_slack_lookup = MagicMock(return_value=MagicMock())
    skill._collect_slack_lookup = MagicMock(return_value=lookup)
    skill._summarize = MagicMock(
        side_effect=[
            ("## 企画の概要\n- 展示企画で、当社は動画制作を担当。[S:slack0]", 0.0),
            (
                "展示企画で、当社は動画制作を担当しています。[S:slack0]\n"
                "関連案件のレポートもあります。[S:1]\n詳しい資料が見たければどうぞ。",
                0.0,
            ),
        ]
    )
    out = skill.run(SearchInput(query=QUERY), SkillContext(metadata=META))
    assert "動画制作を担当" in out.answer and "[S:" not in out.answer
    assert f"<{POST}|会話の記録>" in out.answer
    assert "<https://drive.google.com/file/d/FILE1/view|資料を見る>" in out.answer
    assert [ref.drive_file_id for ref in out.displayed_refs] == ["FILE1"]
    assert "/FILE2/" not in out.answer
    assert out.answer_format is None


def test_normal_source_limit_counts_slack_and_deduplicates() -> None:
    from teamagent.adapters.pgvector_client import SearchHit

    hits = [
        SearchHit(
            i,
            "企画の記録。",
            1.0,
            {
                "title": f"{{topic_{i}}}",
                "source_type": "slack",
                "source_uri": POST if i < 3 else POST.replace("000000", f"00000{i}"),
            },
        )
        for i in range(1, 7)
    ]
    refs: list[DocumentReference] = []
    answer = compact.append_used_drive_links(
        " ".join(f"{{topic_{i}}}の記録。" for i in range(1, 7)), hits, refs
    )
    assert answer.count("|会話の記録>") == 3
    assert answer.count(POST) == 1 and refs == []


def test_normal_citation_does_not_add_uncited_same_client_material() -> None:
    refs: list[DocumentReference] = []
    answer = compact.append_used_drive_links(
        "{newspaper}の展示企画です。[S:1]",
        [_case(1, client_name="{newspaper}"), _case(2, client_name="{newspaper}")],
        refs,
    )
    assert "FILE1" in answer and "FILE2" not in answer
    assert [ref.drive_file_id for ref in refs] == ["FILE1"]


def test_normal_slack_attribution_also_preserves_the_original_post() -> None:
    item = SlackItem(SlackHitOut(channel="#public-exhibition", permalink=POST), "企画の記録。")
    answer = compact.append_used_drive_links(
        "当社は動画制作を担当しています。（Slack: #public-exhibition）",
        [],
        [],
        slack_items=(item,),
    )
    assert f"<{POST}|会話の記録>" in answer


def test_slack_evidence_preserves_authorized_saved_attachment_even_when_unread() -> None:
    ref = DocumentReference(
        kind="drive",
        drive_file_id="UNREAD",
        unread=True,
        source_type="slack",
        url="https://drive.google.com/file/d/UNREAD/view",
    )
    item = SlackItem(
        SlackHitOut(channel="#public-exhibition", permalink=POST, reference=ref),
        "当社は動画制作を担当。",
    )
    refs: list[DocumentReference] = []
    answer = compact.append_used_drive_links(
        "当社は動画制作を担当しています。[S:slack0]", [], refs, slack_items=(item,)
    )
    assert "<https://drive.google.com/file/d/UNREAD/view|資料を見る>" in answer
    assert POST not in answer and refs == [ref]


@pytest.mark.parametrize("line", ["再生は900万回です。[S:1]", "未確認の主張。[S:unknown]"])
def test_normal_retry_does_not_restore_unknown_citations_or_invented_numbers(line: str) -> None:
    assert compact.normal_answer(line, sources={"1": ("資料", POST, "企画の記録。")}) == ""


def test_named_alias_report_reaches_summary_and_its_original_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill, pg = make([])
    pg.list_client_names.return_value = ["株式会社{newspaper}新聞社"]
    report = _case(51, title="{newspaper}_{exhibition_b}_効果分析レポート.pptx")
    pg.find_documents_for_advertiser.side_effect = lambda conn, name, **kw: (
        [report] if name == "{newspaper}" else []
    )
    skill._summarize = MagicMock(return_value=("関連案件のレポートが存在します。[S:51]", 0.0))
    out = skill.run(SearchInput(query=QUERY), SkillContext(metadata=META))
    assert out.found and "FILE51" in out.answer
    assert [ref.drive_file_id for ref in out.displayed_refs] == ["FILE51"]
    assert any(h.chunk_id == 51 for h in skill._summarize.call_args.args[1])


@pytest.mark.parametrize(
    "tail", ["詳しい資料が見たければどうぞ。", "詳しくはこちら。", "資料はこちらです。"]
)
@pytest.mark.parametrize("has_link", [False, True])
@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_linkless_invitation_is_removed_at_mcp_output(
    monkeypatch: pytest.MonkeyPatch, tail: str, has_link: bool, fanout_enabled: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    skill, _ = make([_case(1)])
    normal = "展示企画で、当社は動画制作を担当しています。" + tail
    if has_link:
        normal += f"\n<{POST}|会話の記録>"
    skill._summarize = MagicMock(side_effect=[("", 0.0), (normal, 0.0)])
    out = skill.run(SearchInput(query=QUERY), SkillContext(metadata=META))
    assert tail not in out.answer
    assert ("資料は見つかりませんでした。" in out.answer) is not has_link
    assert "動画制作を担当" in out.answer


@pytest.mark.parametrize("drive_mode", [False, True])
def test_compact_prompt_does_not_receive_conflicting_source_format(
    monkeypatch: pytest.MonkeyPatch, drive_mode: bool
) -> None:
    from tests.skills.search.test_fanout import _bedrock

    skill, _ = make([_case(1)])
    bedrock = _bedrock()
    item = SlackItem(SlackHitOut(channel="#public-exhibition", permalink=POST), "企画の記録。")
    skill._summarize(
        QUERY,
        [_case(1)],
        "topic-test",
        bedrock=bedrock,
        slack_items=(item,),
        compact_sources={"1": ("資料", POST, "企画の記録。")},
        compact_drive_mode=drive_mode,
    )
    kw = bedrock.converse.call_args.kwargs
    text = kw["system"] + str(kw["messages"])
    assert ("（Slack: #チャンネル名 投稿日）" in text) is not drive_mode
    assert ("出典は（金庫: 資料名）" in text) is not drive_mode
