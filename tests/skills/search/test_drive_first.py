from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.search import drive_first, fanout
from teamagent.skills.search.composite import SlackItem, SlackLookup
from teamagent.skills.search.expand import ExpansionResult
from teamagent.skills.search.fanout_sources import SourceResult
from teamagent.skills.search.schema import SearchInput, SlackHitOut
from tests.skills.search.test_fanout import META, make

DRIVE = "https://drive.google.com/file/d/ALPHA/view"
POST = "https://example.slack.com/archives/CALPHA/p1234567890123456"


@pytest.fixture
def setup_search(monkeypatch):
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1")
    monkeypatch.setenv("SEARCH_FANOUT_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setattr(fanout, "build_expansion_planner", MagicMock)
    monkeypatch.setattr(
        fanout,
        "run_expansion",
        lambda *a, **kw: ExpansionResult((), None, (), "empty", 1, 0.0),
    )
    item = SlackItem(
        SlackHitOut(channel="#shared", permalink=POST, file_names=["proposal_alpha.pdf"]),
        "食品の事例 proposal_alpha.pdf",
    )
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: SlackLookup("ok", "ok", (item,)))
    skill, pg = make([])
    pg.resolve_file_urls_by_titles.return_value = {"proposal_alpha.pdf": DRIVE}

    def saved_files(skill, hits, ctx):
        result = []
        for hit in hits:
            names = drive_first.candidate_names(hit.metadata or {}, hit.content)
            url = pg.resolve_file_urls_by_titles.return_value.get(names[0]) if names else None
            result.append(
                SimpleNamespace(
                    record_number=str(hit.chunk_id),
                    branch_number="1",
                    drive_url=url,
                    drive_file_id=drive_first.drive_file_id(url),
                    title=names[0],
                )
                if url and hit.metadata.get("source_type") == "slack"
                else None
            )
        return result

    skill._saved_records_mock = MagicMock(side_effect=saved_files)
    monkeypatch.setattr(drive_first, "resolve_saved_files", skill._saved_records_mock)
    return skill, pg


def test_live_slack_case_uses_drive_instead_of_post(setup_search):
    skill, pg = setup_search
    out = skill.run(SearchInput(query="食品の事例出して"), SkillContext(metadata=META))
    assert DRIVE in out.answer
    assert POST not in out.answer
    assert "資料を見る" in out.answer
    assert out.displayed_refs[0].drive_file_id == "ALPHA"
    pg.resolve_file_urls_by_titles.assert_not_called()
    skill._saved_records_mock.assert_called_once()


def test_unresolved_slack_attachment_is_explicitly_unread(setup_search):
    skill, pg = setup_search
    pg.resolve_file_urls_by_titles.return_value = {}
    # 事例一覧では除外するが、通常の資料探しでは従来どおり未読の出典を示す。
    out = skill.run(SearchInput(query="食品の資料を探して"), SkillContext(metadata=META))
    assert "Slack にだけある資料（本文未読）" in out.answer
    assert "本文未読" in out.answer
    assert "[資料]" not in out.answer
    assert out.slack_hits[0].reference.unread


def test_off_and_outside_allowlist_are_byte_identical(setup_search, monkeypatch):
    skill, _ = setup_search
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "0")
    baseline = skill.run(SearchInput(query="食品の事例出して"), SkillContext(metadata=META))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", "another@example.test")
    actual = skill.run(SearchInput(query="食品の事例出して"), SkillContext(metadata=META))
    assert actual.model_dump_json() == baseline.model_dump_json()
    assert "displayed_refs" not in actual.model_dump()


def test_ambiguous_version_prefix_is_not_removed():
    names = drive_first.candidate_names({"file_names": ["2024__proposal_alpha.pdf"]})
    assert names == ["2024__proposal_alpha.pdf"]


def test_partial_attachment_match_keeps_all_files_unread():
    ref = drive_first.canonical_reference(
        {
            "source_type": "slack",
            "source_uri": POST,
            "file_names": ["proposal_alpha.pdf", "proposal_beta.pdf"],
        },
        file_urls={"proposal_alpha.pdf": DRIVE},
    )
    assert ref.unread
    assert ref.drive_file_id is None
    assert ref.url == POST


@pytest.mark.parametrize(
    "url",
    [
        "https://drive.google.com/file/d/ALPHA.invalid/view",
        "https://drive.google.com/file/d/ALPHA%2FBETA/view",
        "https://drive.google.com/open?id=ALPHA&id=BETA",
        "https://drive.google.com.evil.test/file/d/ALPHA/view",
        "https://drive.google.com/file/d/ALPHA/view?id=BETA",
        "https://drive.google.com:444/file/d/ALPHA/view",
    ],
)
def test_invalid_or_ambiguous_drive_url_is_not_rewritten(url):
    assert drive_first.drive_file_id(url) is None


@pytest.mark.parametrize(
    "meta,kind",
    [
        ({"source_type": "slack", "source_uri": POST}, "slack_conversation"),
        (
            {"source_type": "slack", "source_uri": POST, "file_names": ["proposal_alpha.pdf"]},
            "slack_attachment",
        ),
        (
            {
                "source_type": "gsheets",
                "source_uri": "https://docs.google.com/spreadsheets/d/SHEET/edit#gid=1",
            },
            "sheet",
        ),
        ({"source_uri": "https://example.test/app"}, "vault"),
        ({"source_type": "gdrive", "source_uri": DRIVE}, "drive"),
    ],
)
def test_reference_kinds_are_distinct(meta, kind):
    assert drive_first.canonical_reference(meta).kind == kind


def _record(name, index):
    return SearchHit(
        index,
        f"食品の事例 {name}.pdf",
        0.9,
        {
            "source_type": "slack",
            "source_uri": POST + str(index),
            "title": f"{name}.pdf",
            "document_id": f"doc-{index}",
        },
    )


def test_named_karte_expand_and_main_all_use_canonical_function(setup_search, monkeypatch):
    skill, pg = setup_search
    main, named, karte, expanded = [
        _record(name, i)
        for i, name in enumerate(["main_alpha", "named_alpha", "karte_alpha", "expand_alpha"], 1)
    ]

    def retrieve(*args, **kwargs):
        probe = kwargs["fanout_probe"]
        probe.update(
            named=[named],
            candidates=[main],
            karte=SourceResult("karte", "ok", "ok", items=(karte,)),
        )
        return [main]

    monkeypatch.setattr(skill, "_retrieve", retrieve)
    monkeypatch.setattr(
        fanout,
        "run_expansion",
        lambda *a, **kw: ExpansionResult((), None, (expanded,), "ok", 1, 0.0),
    )
    pg.resolve_file_urls_by_titles.return_value = {
        f"{name}_alpha.pdf": f"https://drive.google.com/file/d/{name.upper()}/view"
        for name in ("main", "named", "karte", "expand")
    }
    out = skill.run(SearchInput(query="食品の資料を探して"), SkillContext(metadata=META))
    for name in ("MAIN", "NAMED", "KARTE", "EXPAND"):
        assert f"/d/{name}/" in out.answer
    assert out.hits[0].reference.kind == "drive"
    requested = [
        name
        for hit in skill._saved_records_mock.call_args.args[1]
        for name in drive_first.candidate_names(hit.metadata or {}, hit.content)
    ]
    assert {"main_alpha.pdf", "named_alpha.pdf", "karte_alpha.pdf", "expand_alpha.pdf"} <= set(
        requested
    )


def test_displayed_references_match_only_five_rendered_cases(setup_search, monkeypatch):
    skill, _ = setup_search
    rows = [
        SearchHit(
            i,
            "食品の事例",
            0.9,
            {
                "source_type": "gdrive",
                "source_uri": f"gdrive://FILE{i}",
                "title": f"事例{i}",
                "document_id": f"doc-{i}",
            },
        )
        for i in range(1, 8)
    ]
    monkeypatch.setattr(skill, "_retrieve", lambda *a, **kw: rows)
    out = skill.run(SearchInput(query="食品の事例出して"), SkillContext(metadata=META))
    assert out.answer.count("|資料を見る>") == len(out.displayed_refs) == 5
    assert [ref.document_id for ref in out.displayed_refs] == [f"doc-{i}" for i in range(1, 6)]
    assert [ref.drive_file_id for ref in out.displayed_refs] == [f"FILE{i}" for i in range(1, 6)]


def test_ingested_slack_body_drive_url_requires_same_user_rls(setup_search):
    skill, pg = setup_search
    record = SearchHit(1, f"原本 {DRIVE}", 0.9, {"source_type": "slack", "source_uri": POST})
    pg.list_authorized_document_references.return_value = [
        {"source_type": "gdrive", "external_id": "ALPHA", "title": "proposal_alpha.pdf"}
    ]
    groups, _, _ = drive_first.annotate(skill, [[record]], (), SkillContext(metadata=META))
    ref = groups[0][0].metadata[drive_first.REFERENCE_KEY]
    assert ref.drive_file_id == "ALPHA"
    assert ref.title == "proposal_alpha.pdf"
    pg.list_authorized_document_references.return_value = []
    groups, _, _ = drive_first.annotate(skill, [[record]], (), SkillContext(metadata=META))
    assert groups[0][0].metadata[drive_first.REFERENCE_KEY].kind == "slack_conversation"


def test_drive_document_is_not_replaced_by_url_mentioned_in_its_body(setup_search):
    skill, pg = setup_search
    record = SearchHit(
        1,
        "参照 https://drive.google.com/file/d/OTHER/view",
        0.9,
        {
            "source_type": "gdrive",
            "source_uri": DRIVE,
        },
    )
    groups, _, _ = drive_first.annotate(skill, [[record]], (), SkillContext(metadata=META))
    assert groups[0][0].metadata[drive_first.REFERENCE_KEY].drive_file_id == "ALPHA"
    pg.list_authorized_document_references.assert_not_called()


def test_drive_document_keeps_its_own_identity_over_related_drive_url():
    ref = drive_first.canonical_reference(
        {
            "source_type": "gdrive",
            "source_uri": "gdrive://ORIGINAL",
            "drive_url": "https://drive.google.com/file/d/RELATED/view",
        }
    )
    assert ref.drive_file_id == "ORIGINAL"


def test_compact_slack_uses_canonical_link_and_unread_provenance(setup_search):
    from teamagent.skills.search import compact

    skill, _ = setup_search
    item = SlackItem(SlackHitOut(permalink=POST, file_names=["proposal_alpha.pdf"]), "紹介投稿")
    _, items, _ = drive_first.annotate(skill, [], (item,), SkillContext(metadata=META))
    sources = compact.source_map([], items, lambda h: None)
    title, url, content = sources["slack0"]
    assert url == DRIVE
    assert "Drive資料" in title and "添付本文未読" in title
    assert content == "紹介投稿"


def test_nonfanout_live_slack_case_uses_same_resolver(setup_search, monkeypatch):
    from teamagent.skills.search.composite import MCP_SURFACE_CTX_KEY
    from tests.skills.search.test_composite_search import _bedrock, _skill, _slack_client
    from tests.skills.slack_search.test_slack_search import PUBLIC

    monkeypatch.setenv("USE_SEARCH_FANOUT", "0")
    monkeypatch.setenv("USE_COMPOSITE_SEARCH", "1")
    skill, pg = setup_search
    match = {
        **PUBLIC,
        "text": "食品の事例 proposal_alpha.pdf",
        "files": [
            {"name": "proposal_alpha.pdf", "id": "FALPHA"},
        ],
    }
    skill, _ = _skill(_bedrock(), pg, client=_slack_client([match]))
    out = skill.run(
        SearchInput(query="食品の事例出して"),
        SkillContext(
            metadata={
                **META,
                MCP_SURFACE_CTX_KEY: True,
                "_search_compact_answer": True,
            }
        ),
    )
    assert DRIVE in out.answer
    assert out.displayed_refs[0].drive_file_id == "ALPHA"


def test_record_sheet_url_is_never_offered_as_the_document() -> None:
    # 10-20 小俣さん: 自動保管シート等の記録シートは資料ではない。資料そのものの URL だけを出す。
    sheet = "https://docs.google.com/spreadsheets/d/1SHEETabcdefghijklmnopqrstuvwxyz0123/edit#gid=1"
    ref = drive_first.canonical_reference(
        {
            "source_type": "gsheets",
            "source_uri": sheet,
            "drive_url": sheet,
            "title": "ファイル記録",
        },
        file_urls={"ファイル記録": sheet},
    )
    assert ref.kind == "sheet"
    assert ref.drive_file_id is None
    assert "/file/d/" not in (ref.url or "")
