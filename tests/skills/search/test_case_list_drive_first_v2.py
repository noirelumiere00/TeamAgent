"""Drive 正本を確認した事例だけ、根拠の抜粋を短いカードで返す。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.search.case_list import render
from teamagent.skills.search.drive_first import REFERENCE_KEY
from teamagent.skills.search.schema import DocumentReference

DRIVE = "https://drive.google.com/file/d/ALPHA/view"
POST = "https://example.slack.com/archives/C0PUBLIC/p1234567890123456"


def _hit(index: int = 1, **meta: object) -> SearchHit:
    values: dict[str, object] = {
        "title": "匿名案件",
        "source_type": "gdrive",
        "source_uri": DRIVE,
        "updated_at": "2025-09-18",
        REFERENCE_KEY: DocumentReference(
            document_id=f"doc-{index}", kind="drive", drive_file_id="ALPHA", url=DRIVE
        ),
    }
    values.update(meta)
    return SearchHit(
        index, "生活シーンを描く動画を提案。出演者と配信面を組み合わせた。", 0.9, values
    )


def _render(hits: list[SearchHit], refs: list[DocumentReference] | None = None) -> str:
    return render(
        hits, None, lambda h: str(h.metadata.get("source_uri") or "") or None, displayed_refs=refs
    )


def test_drive_card_has_exact_format_and_two_source_sentences() -> None:
    refs: list[DocumentReference] = []
    assert _render([_hit()], refs) == (
        "*匿名案件*（2025年9月）\n"
        "• 生活シーンを描く動画を提案。\n"
        "• 出演者と配信面を組み合わせた。\n"
        f"• <{DRIVE}|資料を見る>"
    )
    assert [ref.document_id for ref in refs] == ["doc-1"]


def test_case_effect_is_used_without_advice_or_a_third_sentence() -> None:
    text = _render(
        [
            _hit(
                case_effect=(
                    "店頭から動画へ誘導する施策を提案。配信面を連動させた。三つ目の説明。\n\n"
                    "推奨アクション\n別の施策を試す。\n\nまとめ\n成功のパターンです。"
                )
            )
        ]
    )
    assert "店頭から動画へ誘導する施策を提案。" in text
    assert "配信面を連動させた。" in text
    assert "三つ目" not in text and "推奨" not in text and "まとめ" not in text
    assert "生活シーン" not in text


@pytest.mark.parametrize("source_date", [None, "2025-13-01", "2025-02-31"])
def test_date_is_never_filled_from_current_time(source_date: str | None) -> None:
    assert _render([_hit(updated_at=source_date)]).startswith("*匿名案件*（日付不明）")


def test_slack_only_attachment_is_unread_and_links_only_to_the_conversation() -> None:
    ref = DocumentReference(
        kind="slack_attachment", unread=True, url=POST, source_uri=POST, slack_file_ids=["F0ALPHA"]
    )
    text = _render([_hit(source_type="slack", source_uri=POST, **{REFERENCE_KEY: ref})])
    assert "Slack にだけある資料（本文未読）" in text
    assert f"\n• <{POST}|会話の記録>" in text
    assert "資料を見る" not in text


@pytest.mark.parametrize(
    ("metadata", "mark"),
    [
        ({"case_source": "deck", "case_external_use": "ng"}, "社外展開NG（社内のみ）"),
        ({"case_source": "deck", "case_external_use": "unknown"}, "社外利用は要確認"),
        ({"topic": "案件決定"}, "案件（実績ではありません）"),
    ],
)
def test_case_and_deal_restrictions_are_retained(metadata: dict[str, str], mark: str) -> None:
    assert mark in _render([_hit(**metadata)])


def test_only_five_rendered_cards_are_saved_after_duplicate_removal() -> None:
    hits = []
    for i in range(1, 8):
        url = f"https://drive.google.com/file/d/FILE{i}/view"
        hits.append(
            _hit(
                i,
                title=f"匿名案件{i}",
                source_uri=url,
                **{
                    REFERENCE_KEY: DocumentReference(
                        document_id=f"doc-{i}", kind="drive", drive_file_id=f"FILE{i}", url=url
                    )
                },
            )
        )
    refs: list[DocumentReference] = []
    answer = _render([hits[0], hits[0], *hits[1:]], refs)
    assert answer.count("|資料を見る>") == len(refs) == 5
    assert [ref.document_id for ref in refs] == [f"doc-{i}" for i in range(1, 6)]
    assert "匿名案件6" not in answer


def test_unannotated_output_is_the_legacy_single_line() -> None:
    hit = _hit()
    hit.metadata.pop(REFERENCE_KEY)
    assert _render([hit]) == (
        "匿名案件（2025-09-18）— 生活シーンを描く動画を提案。出演者と配信面を組み合わせた。 "
        f"[資料]({DRIVE})"
    )


def test_untrusted_text_and_url_do_not_create_slack_controls() -> None:
    hit = replace(
        _hit(title="匿名*案件* <!channel>"), content="<!here> 配信を提案。<@U0123> と組んだ。"
    )
    text = _render([hit])
    assert "<!channel>" not in text and "<!here>" not in text and "<@U0123>" not in text
    assert "匿名＊案件＊" in text
    ref = DocumentReference(
        kind="drive", url="https://drive.google.com.evil.test/file/d/ALPHA/view"
    )
    assert "|資料を見る>" not in _render([_hit(**{REFERENCE_KEY: ref})])


def test_link_and_mention_metadata_do_not_replace_the_two_proposal_sentences() -> None:
    hit = replace(
        _hit(title="匿名案件 <@U0123> <https://evil.test/source|参照>"),
        content=(
            "共有先: [資料](https://example.test/original)\n<!here> <@U0123>\n提案内容:\n"
            "生活シーンを描く動画を提案。出演者と配信面を組み合わせた。"
        ),
    )
    answer = _render([hit])
    assert answer == (
        "*匿名案件*（2025年9月）\n"
        "• 生活シーンを描く動画を提案。\n"
        "• 出演者と配信面を組み合わせた。\n"
        f"• <{DRIVE}|資料を見る>"
    )


def test_length_budget_keeps_only_the_references_of_retained_cards() -> None:
    hits = []
    for index in range(1, 8):
        url = f"{POST}?note={index}" + "a" * 350
        ref = DocumentReference(
            document_id=f"doc-{index}",
            kind="slack_attachment",
            unread=True,
            url=url,
            slack_file_ids=[f"F0{index}"],
        )
        hits.append(
            _hit(
                index,
                title=f"匿名案件{index}",
                source_type="slack",
                source_uri=url,
                **{REFERENCE_KEY: ref},
            )
        )
    refs: list[DocumentReference] = []
    answer = _render(hits, refs)
    assert len(answer) <= 2500
    assert 0 < len(refs) < 5
    assert answer.count("|会話の記録>") == len(refs)
    for index in range(1, 8):
        assert (f"*匿名案件{index}*" in answer) == any(
            ref.document_id == f"doc-{index}" for ref in refs
        )
