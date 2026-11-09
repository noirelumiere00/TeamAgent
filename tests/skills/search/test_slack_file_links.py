"""取込済み Slack 資料の原本リンクを正確なホストで表示する。"""

import pytest

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.search.case_list import case_link, render
from teamagent.skills.search.compact import append_used_drive_links
from teamagent.skills.search.drive_first import canonical_reference
from teamagent.skills.search.schema import DocumentReference


def test_reference_uses_attached_file_before_message_permalink():
    post = "https://example.slack.com/archives/C0FAKE/p1784424000000001"
    file_url = "https://example.slack.com/files/U0FAKE/F0SHARED/proposal.pdf"
    ref = canonical_reference(
        {
            "source_type": "slack",
            "source_uri": post,
            "files": [{"id": "F0SHARED", "permalink": file_url}],
        }
    )
    assert ref.url == file_url and ref.kind == "slack_attachment"
    assert ref.slack_file_ids == ["F0SHARED"]


def test_reference_does_not_use_another_files_permalink():
    post = "https://example.slack.com/archives/C0FAKE/p1784424000000001"
    ref = canonical_reference(
        {
            "source_type": "slack",
            "source_uri": post,
            "slack_file_ids": ["F0WANTED"],
            "files": [
                {"id": "F0OTHER", "permalink": "https://example.slack.com/files/U0/F0OTHER/"}
            ],
        }
    )
    assert ref.url == post and ref.slack_file_ids == ["F0WANTED"]


@pytest.mark.parametrize("host", ["example.slack.com", "slack.com", "nested.example.slack.com"])
def test_case_link_accepts_slack_file_permalink(host: str) -> None:
    url = f"https://{host}/files/U0OWNER/F0SHARED/proposal.pdf"
    assert case_link(url, "資料を見る") == f"<{url}|資料を見る>"
    assert case_link(url, "会話の記録") is None


@pytest.mark.parametrize(
    "url",
    [
        "https://slack.com.evil.jp/files/U0/F0/p.pdf",
        "https://evilslack.com/files/U0/F0/p.pdf",
        "https://example.slack.com.evil.jp/files/U0/F0/p.pdf",
        "https://slack.com@evil.jp/files/U0/F0/p.pdf",
        "https://evil.jp@slack.com/files/U0/F0/p.pdf",
        "https://example.slack.com:444/files/U0/F0/p.pdf",
        "http://example.slack.com/files/U0/F0/p.pdf",
        "https://example.slack.com/files/U0/F0/p.pdf|偽装",
    ],
)
def test_case_link_rejects_fake_or_non_file_slack_url(url: str) -> None:
    assert case_link(url, "資料を見る") is None


@pytest.mark.parametrize("drive_only", [False, True])
def test_slack_ingested_file_card_and_compact_link_point_to_original(drive_only: bool) -> None:
    url = "https://example.slack.com/files/U0OWNER/F0SHARED/proposal.pdf"
    hit = SearchHit(
        1,
        "配信面を組み合わせた提案。",
        0.9,
        {
            "title": "共有資料.pdf",
            "source_type": "other",
            "external_id": "F0SHARED",
            "source_uri": url,
        },
    )
    ref = canonical_reference(hit.metadata, hit.content)
    assert ref.kind == "slack_attachment" and ref.slack_file_id == "F0SHARED"
    assert not ref.unread
    refs: list[DocumentReference] = []
    text = render(
        [hit], None, lambda h: url, cards=True, drive_only=drive_only, displayed_refs=refs
    )
    assert f"<{url}|資料を見る>" in text
    assert "本文未読" not in text and "会話のURL未確認" not in text
    assert refs == [ref]
    compact_refs: list[DocumentReference] = []
    answer = append_used_drive_links(
        "共有資料の提案です。[S:1]", [hit], compact_refs, authorize_drive=lambda ref: False
    )
    assert f"<{url}|資料を見る>" in answer
    assert compact_refs == [ref]
