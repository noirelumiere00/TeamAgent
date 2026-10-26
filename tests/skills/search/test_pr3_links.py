"""batch-11 の文面を匿名化した、検索から送信までの回帰。"""

from unittest.mock import MagicMock

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.search import compact, drive_first, fanout
from teamagent.skills.search.answer_mode import strip_advice
from teamagent.skills.search.schema import SearchInput
from tests.skills.search.test_fanout import META, _converse, _vault_hit, make
from tests.skills.search.test_fanout import setup as setup


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize(
    ("query", "body", "tail"),
    [
        (
            "青空社の事例出して",
            "青空社の動画施策です。",
            "詳しい資料が見たいときは、お知らせください。",
        ),
        (
            "社内でいう動画商品とは？",
            "動画商品は配信サービスです。",
            "詳しい資料が必要でしたらお知らせください。",
        ),
        (
            "展示企画って何？うちも関わってる？",
            "展示企画の広報を担当しました。",
            "提案資料や詳細が必要でしたら、お知らせください。",
        ),
        (
            "宅配サービスの進め方は？",
            "宅配サービスの提案です。",
            "詳しく見たい資料があれば、内容をお送りすることもできます。",
        ),
        (
            "青空社の施策は今どうなっている？",
            "青空社の施策は完了しました。",
            "詳しい資料が必要でしたら、お知らせください。",
        ),
    ],
    ids=[
        "list_cases",
        "internal_term",
        "unjoined_channel_topic",
        "delivery_kanji",
        "completed_status",
    ],
)
def test_batch11_used_original_always_linked(monkeypatch, fanout_enabled, query, body, tail):
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setattr(
        drive_first, "resolve_saved_files", lambda skill, hits, ctx: [None] * len(hits)
    )
    hit = _vault_hit(1, title="青空社の匿名提案資料", source_uri="gdrive://ORIGINAL")
    skill, pg = make([hit])
    pg.list_authorized_document_references.return_value = [
        {"external_id": "ORIGINAL", "source_type": "gdrive"}
    ]
    # compact が根拠を残せず、通常文へ戻る形も通す（事例一覧は既存描画）。
    skill._bedrock.converse.side_effect = [_converse(""), _converse(body + "[S:1]" + tail)]
    monkeypatch.setattr(fanout, "build_summary_client", lambda: skill._bedrock)
    out = skill.run(SearchInput(query=query), SkillContext(metadata=META))
    assert "<https://drive.google.com/file/d/ORIGINAL/view|資料を見る>" in out.answer
    assert "お知らせ" not in out.answer and "声をかけ" not in out.answer
    assert [ref.drive_file_id for ref in out.displayed_refs] == ["ORIGINAL"]


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://drive.google.com/file/d/ORIGINAL/view",
        "https://example.slack.com/archives/C1/p1234567890123456",
        "https://example.test/app",
    ],
)
@pytest.mark.parametrize(
    "tail",
    [
        "詳しい資料が必要でしたらお知らせください。",
        "詳しい資料が見たいときは、お知らせください。",
        "提案資料や詳細が必要でしたら、声をかけてください。",
    ],
)
def test_guard_removes_invitations_with_or_without_links(url, tail):
    answer = compact.strip_linkless_invitation(
        "確認済みです。" + tail + ("\n" + url if url else "")
    )
    assert "お知らせ" not in answer and "声をかけ" not in answer
    assert ("資料は見つかりませんでした。" in answer) == (not url)
    if url:
        assert url in answer


@pytest.mark.parametrize("query", ["比較して何が効いた？", "過去提案で効いたのは？", "共通点は？"])
def test_analysis_does_not_request_next_action(query):
    body = "冒頭で完成形を示した点が効いたと考えられます。"
    answer = body + "\n\n:alarm_clock: ネクストアクション\n同条件での配信が推奨です。\n\n出典: 資料"
    assert strip_advice(answer, query) == body + "\n\n出典: 資料"
    assert strip_advice(answer, query + "次の打ち手も考えて") == answer


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("drive_enabled", [False, True])
def test_case_cards_kind_uses_existing_cards(monkeypatch, fanout_enabled, drive_enabled):
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", str(int(drive_enabled)))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setattr(
        drive_first, "resolve_saved_files", lambda skill, hits, ctx: [None] * len(hits)
    )
    hits = [
        _vault_hit(i, title=f"匿名案件{i}", source_uri=f"gdrive://FILE{i}", updated_at="2026-09-01")
        for i in range(1, 8)
    ]
    skill, _ = make(hits)
    skill._summarize = MagicMock(side_effect=AssertionError("cards need no summary"))
    out = skill.run(
        SearchInput(query="事例出して"),
        SkillContext(
            metadata={
                **META,
                "_search_direct_answer": True,
                "_search_direct_answer_kinds": ("case_cards",),
            }
        ),
    )
    assert out.direct_answer_kind == "case_cards" and out.direct_answer == out.answer
    assert out.answer_format == "list"
    assert out.answer.count("|資料を見る>") == len(out.displayed_refs) == 5
    assert "*匿名案件1*（2026年9月）" in out.answer
    skill._summarize.assert_not_called()


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("failure", ["denied", "error", "other-file"])
def test_used_original_is_reauthorized_fail_closed(monkeypatch, fanout_enabled, failure):
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setattr(
        drive_first, "resolve_saved_files", lambda skill, hits, ctx: [None] * len(hits)
    )
    skill, pg = make([_vault_hit(1, title="匿名提案", source_uri="gdrive://REVOKED")])
    pg.list_authorized_document_references.return_value = (
        [] if failure == "denied" else [{"source_type": "gdrive", "external_id": "OTHER"}]
    )
    if failure == "error":
        pg.list_authorized_document_references.side_effect = RuntimeError("offline")
    skill._bedrock.converse.side_effect = [
        _converse(""),
        _converse("確認済みです。[S:1]詳しい資料が必要でしたらお知らせください。"),
    ]
    monkeypatch.setattr(fanout, "build_summary_client", lambda: skill._bedrock)
    out = skill.run(SearchInput(query="提案の進め方は？"), SkillContext(metadata=META))
    assert "REVOKED" not in out.answer and out.displayed_refs == []
    assert "資料は見つかりませんでした。" in out.answer and "お知らせ" not in out.answer
    pg.list_authorized_document_references.assert_called_once()
    assert pg.list_authorized_document_references.call_args.kwargs["drive_file_ids"] == ["REVOKED"]
    assert pg.connection.call_args.kwargs["user_email"] == META["user_email"]


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("format_mode", ["compact", "title", "vault"])
def test_original_links_survive_model_formats(monkeypatch, fanout_enabled, format_mode):
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setattr(
        drive_first, "resolve_saved_files", lambda skill, hits, ctx: [None] * len(hits)
    )
    skill, pg = make([_vault_hit(1, title="匿名提案資料", source_uri="gdrive://ORIGINAL")])
    pg.list_authorized_document_references.return_value = [
        {"source_type": "gdrive", "external_id": "ORIGINAL"}
    ]
    body = "進行表を共有済みです。[S:1]"
    if format_mode == "title":
        body = "匿名提案資料では進行表を共有済みです。"
    if format_mode == "vault":
        body += "\nAico Vault: https://example.test/app"
    skill._bedrock.converse.side_effect = (
        [_converse(body)] if format_mode == "compact" else [_converse(""), _converse(body)]
    )
    monkeypatch.setattr(fanout, "build_summary_client", lambda: skill._bedrock)
    out = skill.run(SearchInput(query="提案の進め方は？"), SkillContext(metadata=META))
    assert out.answer.count("<https://drive.google.com/file/d/ORIGINAL/view|資料を見る>") == 1
    assert [ref.drive_file_id for ref in out.displayed_refs] == ["ORIGINAL"]


@pytest.mark.parametrize(
    "link",
    [
        "<https://drive.google.com/file/d/ORIGINAL/view|資料を見る>",
        "[資料を見る](https://drive.google.com/file/d/ORIGINAL/view)",
        "https://drive.google.com/file/d/ORIGINAL/view",
    ],
)
def test_invitation_removal_preserves_links_in_the_same_sentence(link):
    text = compact.strip_linkless_invitation(
        "確認済みです。詳しい資料が必要でしたらお知らせください " + link + "。"
    )
    assert link in text and "お知らせ" not in text
    assert "資料は見つかりませんでした。" not in text


@pytest.mark.parametrize("authorized", [False, True])
@pytest.mark.parametrize(
    "url_pattern",
    [
        "https://drive.google.com/file/d/FILE{i}/view",
        "https://drive.google.com/file/d/FILE{i}/view?usp=sharing",
        "https://drive.google.com/open?id=FILE{i}",
    ],
)
def test_model_original_urls_are_replaced_by_authorized_code_links(authorized, url_pattern):
    hits = [
        _vault_hit(i, title=f"匿名資料{i}", source_uri=f"gdrive://FILE{i}") for i in range(1, 6)
    ]
    body = (
        "確認済みです。"
        + "".join(f"[S:{i}]" for i in range(1, 6))
        + "\n"
        + "\n".join(f"[原本]({url_pattern.format(i=i)})" for i in range(1, 6))
    )
    refs = []
    answer = compact.append_used_drive_links(
        body, hits, refs, authorize_drive=lambda ref: authorized
    )
    assert answer.count("https://drive.google.com") == (3 if authorized else 0)
    assert answer.count("|資料を見る>") == len(refs) == (3 if authorized else 0)
    assert "[原本](" not in answer


@pytest.mark.parametrize(
    "question",
    ["どの期間の数字か、お知らせください。", "決まったら声をかけてください。"],
)
def test_guard_keeps_clarifying_questions(question):
    answer = compact.strip_linkless_invitation("確認済みです。" + question)
    assert question in answer
    assert "資料は見つかりませんでした。" not in answer
