"""adapters/dm_history（質問と回答の履歴を Slack からその場で読む・上司 5）。

固定すること:
- 管理者は HISTORY_ADMIN_EMAILS だけ（空は誰もいない）
- 本人専用の一回限りのリンク（連携の開始 URL・署名付き S3 URL）は伏せる。普通のリンクと /r/ は残す
- 往復の区切りは 10/6・10/14 の全 DM 判定と同じ（最初の発言より前の通知は束ねない・返事は 6 通まで）
- 読むのは 30 日まで・1,000 件まで。スレッドは返信のある親だけ開く
- Slack の失敗は error code の固定語だけにする（本番の SlackApiError の形で再現）
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from slack_sdk.errors import SlackApiError

from teamagent.adapters import dm_history as dh

USER = "U0123456789"
OTHER = "U0999999999"
DM = "D0123456789"


def _api_error(code: str) -> SlackApiError:
    # 本番の slack_sdk は ok=false の応答で SlackApiError を投げ、response.data に error を持つ
    return SlackApiError("failed", SimpleNamespace(data={"ok": False, "error": code}))


class _FakeSlack:
    def __init__(
        self,
        *,
        pages: list[list[dict[str, Any]]] | None = None,
        replies: dict[str, list[dict[str, Any]]] | None = None,
        fail: dict[str, str] | None = None,
    ) -> None:
        self.pages = pages or [[]]
        self.replies = replies or {}
        self.fail = fail or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            raise _api_error(self.fail[name])

    def users_lookupByEmail(self, **kw: Any) -> dict[str, Any]:  # noqa: N802 - slack_sdk の名前
        self.calls.append(("users_lookupByEmail", kw))
        self._maybe_fail("users_lookupByEmail")
        return {"ok": True, "user": {"id": USER}}

    def conversations_open(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("conversations_open", kw))
        self._maybe_fail("conversations_open")
        return {"ok": True, "channel": {"id": DM}}

    def conversations_history(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("conversations_history", kw))
        self._maybe_fail("conversations_history")
        index = int(kw.get("cursor") or 0)
        more = index + 1 < len(self.pages)
        return {
            "ok": True,
            "messages": self.pages[index],
            "has_more": more,
            "response_metadata": {"next_cursor": str(index + 1) if more else ""},
        }

    def conversations_replies(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("conversations_replies", kw))
        return {"ok": True, "messages": self.replies.get(kw["ts"], [])}

    def conversations_list(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("conversations_list", kw))
        return {
            "ok": True,
            "channels": [
                {"id": "D1", "user": USER},
                {"id": "D2", "user": OTHER},
                {"id": "D3", "user": "U0BOTBOT01"},
                {"id": "D4", "user": "U0GONE0001", "is_user_deleted": True},
            ],
        }

    def users_list(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("users_list", kw))
        return {
            "ok": True,
            "members": [
                {"id": USER, "profile": {"real_name": "小俣 <b>"}},
                {"id": OTHER, "profile": {"display_name": "稲上"}},
                {"id": "U0BOTBOT01", "is_bot": True, "profile": {"real_name": "Aico"}},
                {"id": "U0NOTINDM1", "profile": {"real_name": "DM なし"}},
            ],
        }

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _u(ts: str, text: str, **extra: Any) -> dict[str, Any]:
    return {"ts": ts, "user": USER, "text": text, **extra}


def _a(ts: str, text: str, **extra: Any) -> dict[str, Any]:
    return {"ts": ts, "user": "U0BOTBOT01", "bot_id": "B0123", "text": text, **extra}


# ── 管理者 ─────────────────────────────────────────────────────────────────


def test_admin_allowlist_is_empty_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(dh.ADMIN_EMAILS_ENV, raising=False)
    assert dh.admin_emails() == frozenset()
    assert not dh.is_admin("s-komata@vectorinc.co.jp")
    monkeypatch.setenv(dh.ADMIN_EMAILS_ENV, " S-Komata@vectorinc.co.jp ")
    assert dh.is_admin("s-komata@VECTORINC.co.jp")
    assert not dh.is_admin("x@vectorinc.co.jp") and not dh.is_admin(None)


# ── 伏せ字 ─────────────────────────────────────────────────────────────────


def test_one_time_links_are_masked_but_ordinary_links_stay() -> None:
    text = (
        "① <https://connect.newstv.co.jp/oauth2/start/aC13YXRh|Google を連携>\n"
        "② <https://connect.newstv.co.jp/slack/oauth/start/aC13>\n"
        "PPTX: https://b.s3.ap-northeast-1.amazonaws.com/x.pptx?X-Amz-Algorithm=A&X-Amz-Signature=abc\n"
        "<https://example.com/doc|資料> と <https://connect.newstv.co.jp/r/tok123|レポート>"
    )
    out = dh.redact_secret_links(text)
    assert "oauth2/start" not in out and "slack/oauth/start" not in out
    assert "X-Amz-Signature" not in out
    assert f"Google を連携{dh.SECRET_LINK_MASK}" in out
    assert out.count(dh.SECRET_LINK_MASK) == 3
    assert "<https://example.com/doc|資料>" in out
    assert "<https://connect.newstv.co.jp/r/tok123|レポート>" in out


# ── 区切り ─────────────────────────────────────────────────────────────────


def test_classify_splits_user_aico_and_skips_others() -> None:
    assert dh.classify(_u("1.0", "質問"), user_id=USER) == dh.DmMessage(1.0, "user", "質問")
    assert dh.classify(_a("2.0", "返事"), user_id=USER).role == "aico"  # type: ignore[union-attr]
    assert dh.classify({"ts": "3.0", "user": OTHER, "text": "x"}, user_id=USER) is None
    assert dh.classify({"ts": "4.0", "subtype": "channel_join", "user": USER}, user_id=USER) is None
    assert dh.classify({"ts": "", "user": USER}, user_id=USER) is None
    with_files = dh.classify(
        _u("5.0", "", files=[{"name": "a.pdf"}, {"title": "b"}, {}]), user_id=USER
    )
    assert with_files is not None and with_files.files == ("a.pdf", "b", "ファイル")


def test_group_exchanges_matches_the_dm_judgement_cut() -> None:
    msgs = [
        dh.DmMessage(5.0, "aico", "a3"),
        dh.DmMessage(0.5, "aico", "朝のサマリー"),  # 最初の発言より前の通知は束ねない
        dh.DmMessage(1.0, "user", "q1"),
        dh.DmMessage(2.0, "aico", "a1"),
        dh.DmMessage(4.0, "user", "q2"),
        *[dh.DmMessage(6.0 + i, "aico", f"r{i}") for i in range(7)],
    ]
    out = dh.group_exchanges(msgs)
    assert [e.user_text for e in out] == ["q1", "q2"]
    assert out[0].aico == ("a1",) and out[0].n_aico == 1
    assert out[1].aico == ("a3", "r0", "r1", "r2", "r3", "r4") and out[1].n_aico == 8


# ── 読み取り ───────────────────────────────────────────────────────────────


def test_read_pages_threads_and_window() -> None:
    slack = _FakeSlack(
        pages=[
            [_u("1000.2", "q2"), _a("1000.1", "a1"), _u("1000.0", "q1", reply_count=2)],
            [_a("999.0", "old")],
        ],
        replies={"1000.0": [_u("1000.0", "q1"), _a("1000.05", "スレッドの返事")]},
    )
    read = dh.DmHistoryReader(slack).read(USER, days=90, now=10_000.0)
    assert [m.text for m in read.messages] == ["old", "q1", "スレッドの返事", "a1", "q2"]
    assert not read.truncated
    history = [kw for name, kw in slack.calls if name == "conversations_history"]
    # 90 日を頼んでも 30 日に詰める
    assert history[0]["oldest"] == f"{10_000.0 - 30 * 86400:.6f}"
    assert history[0]["channel"] == DM and history[1]["cursor"] == "1"
    # 返信のある親だけスレッドを開く
    assert [kw["ts"] for name, kw in slack.calls if name == "conversations_replies"] == ["1000.0"]


def test_read_stops_at_the_message_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dh, "MESSAGE_CAP", 3)
    slack = _FakeSlack(pages=[[_u("1.0", "a"), _u("2.0", "b")], [_u("3.0", "c")], [_u("4.0", "d")]])
    read = dh.DmHistoryReader(slack).read(USER, days=30, now=10.0)
    assert read.truncated and len(read.messages) == 3
    assert slack.names().count("conversations_history") == 2


def test_bad_user_id_never_reaches_slack() -> None:
    slack = _FakeSlack()
    with pytest.raises(dh.DmHistoryError) as caught:
        dh.DmHistoryReader(slack).read("someone@example.com", days=30)
    assert caught.value.code == "bad_user" and slack.calls == []


def test_slack_errors_become_fixed_codes() -> None:
    slack = _FakeSlack(fail={"conversations_history": "ratelimited"})
    with pytest.raises(dh.DmHistoryError) as caught:
        dh.DmHistoryReader(slack).read(USER, days=30)
    assert caught.value.code == "ratelimited"
    lookup = _FakeSlack(fail={"users_lookupByEmail": "users_not_found"})
    with pytest.raises(dh.DmHistoryError) as caught:
        dh.DmHistoryReader(lookup).user_id_for_email("x@vectorinc.co.jp")
    assert caught.value.code == "users_not_found"
    # 固定語にならない文字列は slack_failed に丸める（値をログへ持ち込まない）
    assert dh.DmHistoryError("Bad Code With <html>").code == "slack_failed"


def test_user_id_for_email_lowercases_and_validates() -> None:
    slack = _FakeSlack()
    assert dh.DmHistoryReader(slack).user_id_for_email(" S-Komata@Vectorinc.co.jp ") == USER
    assert slack.calls[0] == ("users_lookupByEmail", {"email": "s-komata@vectorinc.co.jp"})


def test_list_users_keeps_people_with_a_dm_only() -> None:
    users = dh.DmHistoryReader(_FakeSlack()).list_users()
    assert [(u.slack_user_id, u.name) for u in users] == [(USER, "小俣 <b>"), (OTHER, "稲上")]


def test_from_env_requires_the_bot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    with pytest.raises(dh.DmHistoryError) as caught:
        dh.DmHistoryReader.from_env()
    assert caught.value.code == "no_bot_token"
