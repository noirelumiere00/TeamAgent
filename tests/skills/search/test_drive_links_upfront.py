"""初回の短い回答と保存参照は、残した本文の Drive 正本だけにそろう。"""

from unittest.mock import MagicMock

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.search import compact, drive_first, fanout
from teamagent.skills.search.schema import DocumentReference, SearchInput
from tests.skills.search.test_fanout import META, _converse, _vault_hit, make
from tests.skills.search.test_fanout import setup as setup


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("compact_enabled", [False, True])
@pytest.mark.parametrize(
    "query",
    [
        "撮影日はいつ？",
        "提案の進め方は？",
        "提案を比較して",
        "提案をまとめて",
        "資料を一覧で出して",
    ],
)
def test_initial_answer_keeps_only_used_canonical_drive_refs(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool, compact_enabled: bool, query: str
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1" if fanout_enabled else "0")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setattr(
        drive_first, "resolve_saved_files", lambda skill, hits, ctx: [None] * len(hits)
    )
    hits = [_vault_hit(i, title=f"資料{i}", source_uri=f"gdrive://DOC{i}") for i in range(1, 6)]
    skill, _ = make(hits)
    skill._bedrock.converse.return_value = _converse(
        "撮影日は決定済みです。[S:2]\n進行表を共有します。[S:2]\n提案の準備を進めます。[S:4]"
    )
    monkeypatch.setattr(fanout, "build_summary_client", lambda: skill._bedrock)
    out = skill.run(
        SearchInput(query=query), SkillContext(metadata={**META, compact.CTX_KEY: compact_enabled})
    )
    assert out.answer_format == "compact"
    assert "撮影日は決定済みです。" in out.answer
    assert out.answer.endswith(
        "<https://drive.google.com/file/d/DOC2/view|資料を見る> / <https://drive.google.com/file/d/DOC4/view|資料を見る>"
    )
    assert [ref.drive_file_id for ref in out.displayed_refs] == ["DOC2", "DOC4"]
    assert out.answer.count("https://") == 2
    assert "[S:" not in out.answer and "金庫の資料" not in out.answer
    prompt = skill._bedrock.converse.call_args.kwargs
    assert "[S:2]" in str(prompt["messages"])


def test_compact_ref_tracking_distinguishes_slack_evidence_and_never_links_sheets() -> None:
    hits = [
        _vault_hit(i, title=f"資料{i}", source_uri=uri, source_type=kind)
        for i, uri, kind in [
            (1, "gdrive://DOC1", "gdrive"),
            (2, "gdrive://DOC1", "gdrive"),
            (3, "https://example.slack.com/archives/C1/p1234567890123456", "slack"),
            (4, "https://docs.google.com/spreadsheets/d/SHEET/edit", "gsheets"),
        ]
    ]
    refs: list[DocumentReference] = []
    text = compact.build(
        "準備済みです。[S:1]\n確認しました。[S:2]\n共有済みです。[S:3]\n記録済みです。[S:4]",
        hits,
        (),
        lambda h: drive_first.canonical_reference(h.metadata, h.content).url,
        query="状況は？",
        displayed_refs=refs,
    )
    assert len(refs) == 1 and refs[0].drive_file_id == "DOC1"
    assert text.count("|資料を見る>") == 1
    assert "|会話の記録>" in text and "spreadsheets" not in text


@pytest.mark.parametrize("attachment", [False, True], ids=["conversation", "unread-attachment"])
@pytest.mark.parametrize("drive_shaped_url", [False, True], ids=["slack-url", "drive-shaped-url"])
def test_cited_non_drive_reference_is_not_a_document_link(
    attachment: bool, drive_shaped_url: bool
) -> None:
    hit = _vault_hit(
        1,
        title="提案の投稿",
        source_type="slack",
        source_uri="https://example.slack.com/archives/C1/p1234567890123456",
        file_names=["proposal.pdf"] if attachment else [],
    )
    ref = drive_first.canonical_reference(hit.metadata, hit.content)
    assert ref.kind == ("slack_attachment" if attachment else "slack_conversation")
    assert ref.unread is attachment
    if drive_shaped_url:
        # URL が Drive 形式でも、正本の種別が Drive 以外なら資料にしない。
        hit.metadata[drive_first.REFERENCE_KEY] = ref.model_copy(
            update={"url": "https://drive.google.com/file/d/NOT_A_DRIVE_REFERENCE/view"}
        )
    body = "提案の記録があります。"
    refs: list[DocumentReference] = []
    answer = compact.build(
        body + "[S:1]",
        [hit],
        (),
        lambda h: drive_first.canonical_reference(h.metadata, h.content).url,
        query="状況は？",
        displayed_refs=refs,
    )
    assert refs == []
    assert answer == body if drive_shaped_url else answer == body + (
        "\n\n<https://example.slack.com/archives/C1/p1234567890123456|会話の記録>"
    )


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize(
    "rejected",
    ["", "根拠不明です。[S:999]", "投稿は900万回再生です。[S:1]", compact.EMPTY_ANSWER + "[S:1]"],
)
def test_rejected_drive_compact_returns_normal_answer_without_unused_inventory(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool, direct: bool, rejected: str
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setenv("SEARCH_ANSWER_SOURCE_LINKS", "1")
    monkeypatch.setattr(
        drive_first, "resolve_saved_files", lambda skill, hits, ctx: [None] * len(hits)
    )
    monkeypatch.setattr(
        compact, "fallback_cards", MagicMock(side_effect=AssertionError("no candidate cards"))
    )
    skill, _ = make([_vault_hit(1, title="提案資料", source_uri="gdrive://UNUSED")])
    normal = "配信面と出演者を組み合わせた記録があります。"
    skill._bedrock.converse.side_effect = [_converse(rejected), _converse(normal)]
    skill._direct_answer_kind = MagicMock(return_value="comparison")
    monkeypatch.setattr(fanout, "build_summary_client", lambda: skill._bedrock)
    out = skill.run(
        SearchInput(query="提案資料の進め方は？"),
        SkillContext(
            metadata={
                **META,
                "_search_direct_answer": direct,
                "_search_direct_answer_kinds": ("comparison",),
            }
        ),
    )
    assert out.answer == normal
    assert out.answer_format is None
    assert out.displayed_refs == []
    assert "UNUSED" not in out.answer and compact.EMPTY_ANSWER not in out.answer
    assert skill._bedrock.converse.call_count == 2
    assert "[S:1]" not in str(skill._bedrock.converse.call_args.kwargs["messages"])


def test_named_only_drive_hit_reaches_normal_summary_after_empty_compact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setattr(
        drive_first, "resolve_saved_files", lambda skill, hits, ctx: [None] * len(hits)
    )
    skill, pg = make([])
    named = _vault_hit(1, title="日本ガイシの提案資料", source_uri="gdrive://NAMED")
    pg.find_documents_for_advertiser.return_value = [named]
    normal = "進行表を先に共有する提案です。"
    skill._bedrock.converse.side_effect = [_converse(""), _converse(normal)]
    monkeypatch.setattr(fanout, "build_summary_client", lambda: skill._bedrock)
    out = skill.run(SearchInput(query="日本ガイシの事例の進め方は？"), SkillContext(metadata=META))
    assert out.found is True and out.answer == normal
    assert out.answer_format is None and out.displayed_refs == []
    assert skill._bedrock.converse.call_count == 2
    assert named.content in str(skill._bedrock.converse.call_args.kwargs["messages"])


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_drive_flag_off_and_non_target_are_identical(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1" if fanout_enabled else "0")
    skill, _ = make()
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "0")
    baseline = skill.run(SearchInput(query="進め方は？"), SkillContext(metadata=META))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", "outside@example.test")
    actual = skill.run(SearchInput(query="進め方は？"), SkillContext(metadata=META))
    assert actual.model_dump_json() == baseline.model_dump_json()


def test_final_three_source_limit_and_dropped_sentences_match_saved_refs() -> None:
    hits = [_vault_hit(i, title=f"資料{i}", source_uri=f"gdrive://DOC{i}") for i in range(1, 6)]
    refs: list[DocumentReference] = []
    answer = compact.build(
        "未確認の数値987654件です。[S:5]\n"
        "最初の資料を確認しました。[S:1]\n次の資料も確認しました。[S:2]\n"
        "別の資料を確認しました。[S:3]\n最後の資料を確認しました。[S:4]",
        hits,
        (),
        lambda h: drive_first.canonical_reference(h.metadata, h.content).url,
        query="進め方は？",
        displayed_refs=refs,
    )
    assert [ref.drive_file_id for ref in refs] == ["DOC1", "DOC2", "DOC3"]
    assert answer.count("|資料を見る>") == 3
    assert "987654" not in answer and "DOC5" not in answer
    assert "最後の資料" not in answer and "DOC4" not in answer


def test_slack_citation_uses_its_authorized_drive_original_only() -> None:
    from teamagent.skills.search.composite import SlackItem
    from teamagent.skills.search.schema import SlackHitOut

    url = "https://drive.google.com/file/d/SAVED/view"
    item = SlackItem(
        SlackHitOut(
            channel="#shared",
            permalink="https://example.slack.com/archives/C1/p1234567890123456",
            reference=DocumentReference(kind="drive", drive_file_id="SAVED", url=url),
        ),
        "進行表を共有済みです。",
    )
    refs: list[DocumentReference] = []
    answer = compact.build(
        "進行表を共有済みです。[S:slack0]",
        [],
        (item,),
        lambda h: None,
        query="進め方は？",
        displayed_refs=refs,
    )
    assert answer == f"進行表を共有済みです。\n\n<{url}|資料を見る>"
    assert [ref.drive_file_id for ref in refs] == ["SAVED"]


def test_code_generated_comparison_links_both_named_materials() -> None:
    from tests.skills.search.test_comparison import _campaign

    hits = [
        _campaign(1, "施策A", "再生数: 平均 200"),
        _campaign(2, "施策B", "再生数: 平均 100"),
    ]
    for hit in hits:
        hit.metadata.update(source_type="gdrive", source_uri=f"gdrive://DOC{hit.chunk_id}")
    refs: list[DocumentReference] = []
    answer = compact.build(
        "",
        hits,
        (),
        lambda h: drive_first.canonical_reference(h.metadata, h.content).url,
        query="施策Aと施策Bを比較して",
        displayed_refs=refs,
    )
    assert "施策Aが200回" in answer and "施策Bが100回" in answer
    assert [ref.drive_file_id for ref in refs] == ["DOC1", "DOC2"]
    assert answer.count("|資料を見る>") == 2
