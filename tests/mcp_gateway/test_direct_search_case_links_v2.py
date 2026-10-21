"""定型事例カードの固定リンクだけを直接投稿でも保持する。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from teamagent.mcp_gateway import detached_jobs, direct_search_answer
from tests.mcp_gateway.test_surface_video_followup import _FakeSlack

DRIVE = "https://drive.google.com/file/d/ALPHA/view"
POST = "https://example.slack.com/archives/C0PUBLIC/p1234567890123456?thread_ts=1.2&cid=C0PUBLIC"


@pytest.fixture
def slack(monkeypatch: pytest.MonkeyPatch) -> _FakeSlack:
    client = _FakeSlack()
    monkeypatch.setenv("SEARCH_DIRECT_ANSWER_KINDS", "list_fast,comparison")
    monkeypatch.setattr(detached_jobs, "_slack_client", lambda timeout_seconds: client)
    monkeypatch.setattr(detached_jobs, "_POST_RETRY_WAIT_S", 0)
    return client


def _deliver(answer: str, *, enabled: bool = True, kind: str = "list_fast") -> object:
    return direct_search_answer.deliver(
        answer,
        kind,
        detached_jobs.Destination("D0ORIGIN", None),
        request_id="req",
        allow_case_links=enabled,
    )


def test_fixed_original_and_conversation_links_survive_exactly_once(slack: _FakeSlack) -> None:
    answer = (
        f"*匿名案件*（2025年9月）\n• 提案 & 組み方。\n• <{DRIVE}|資料を見る>\n"
        f"• <{POST.replace('&', '&amp;')}|会話の記録>"
    )
    assert _deliver(answer)["delivered"] is True
    post = slack.posts[0]
    assert f"<{DRIVE}|資料を見る>" in post["text"]
    assert f"<{POST.replace('&', '&amp;')}|会話の記録>" in post["text"]
    assert "&amp;amp;" not in post["text"]
    assert "*匿名案件*" in post["text"]
    assert post["blocks"][0]["text"] == {
        "type": "mrkdwn",
        "text": post["text"],
        "verbatim": True,
    }


def test_other_links_and_mentions_remain_escaped(slack: _FakeSlack) -> None:
    answer = (
        f"*匿名案件*\n• <{DRIVE}|資料を見る>\n<!here> <@U0123> "
        "<https://evil.test/file|資料を見る> <https://drive.google.com/file/d/ALPHA/view|任意名>"
    )
    _deliver(answer)
    text = slack.posts[0]["text"]
    assert f"<{DRIVE}|資料を見る>" in text
    for forbidden in (
        "<!here>",
        "<@U0123>",
        "<https://evil.test/file|資料を見る>",
        "<https://drive.google.com/file/d/ALPHA/view|任意名>",
    ):
        assert forbidden not in text


@pytest.mark.parametrize(
    "url",
    [
        "https://drive.google.com.evil.test/file/d/ALPHA/view",
        "https://user@drive.google.com/file/d/ALPHA/view",
        "https://example.slack.com/archives/C0PUBLIC/p1234567890123456",
        "http://drive.google.com/file/d/ALPHA/view",
    ],
)
def test_unapproved_named_link_does_not_enable_rich_delivery(slack: _FakeSlack, url: str) -> None:
    _deliver(f"*匿名案件*\n• <{url}|資料を見る>")
    assert slack.posts[0]["blocks"] is None
    assert f"<{url}|資料を見る>" not in slack.posts[0]["text"]


@pytest.mark.parametrize(("enabled", "kind"), [(False, "list_fast"), (True, "comparison")])
def test_off_and_other_answer_kinds_use_original_escape_path(
    slack: _FakeSlack, enabled: bool, kind: str
) -> None:
    answer = f"*匿名案件*\n• <{DRIVE}|資料を見る>"
    _deliver(answer, enabled=enabled, kind=kind)
    assert slack.posts[0]["blocks"] is None
    assert slack.posts[0]["text"] == detached_jobs.slack_escape(answer)


def test_block_rejection_retries_with_a_clickable_plain_url(
    monkeypatch: pytest.MonkeyPatch, slack: _FakeSlack
) -> None:
    original = slack.post_message

    async def reject_blocks(**kwargs):
        result = await original(**kwargs)
        return SimpleNamespace(ok=False) if kwargs.get("blocks") else result

    monkeypatch.setattr(slack, "post_message", reject_blocks)
    assert _deliver(f"*匿名案件*\n• <{DRIVE}|資料を見る>")["delivered"] is True
    assert len(slack.posts) == 2
    assert slack.posts[1]["blocks"] is None
    assert f"<{DRIVE}>" in slack.posts[1]["text"]
    assert "資料を見る" in slack.posts[1]["text"]
    assert "|資料を見る>" not in slack.posts[1]["text"]
