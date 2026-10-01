"""F0: 連携切れの見える化（朝ダイジェストの描画・50 ブロックの最終ガード・末尾の説明文・
管理者 DM・実行結果の JSON ログ）のテスト。

固定する仕様:
  - フラグ OFF（MORNING_DIGEST_FETCH_STATUS_EMAILS が空・対象外の人）は取得状態を一切見ない
    ＝取れなかった日でも従来どおりの描画（バイト単位で同じ）
  - フラグ ON で取れなかった節は「新着なし」「予定なし」と書かず「確認できませんでした」
    ＋原因。失効・権限不足だけ冒頭に案内を 1 つ（「この DM で『連携』」・認可 URL は貼らない）。
    一時的な失敗は再連携に誘導しない
  - ON でも取得がすべて正常な日の描画は OFF と同じ
  - 50 ブロックの最終ガード: 件数が最大で案内も出る日に 48 以下、案内と今日の予定と末尾は残し、
    削るのはメールの一覧から
  - 末尾の説明文は draft_mode の 3 通り（auto は実態・on_demand は従来の文言・off は下書きの一文なし）
  - 管理者 DM: 毎朝 1 行、問題の日だけ内訳。社内ドメイン・lookup の検査・D 宛てのみ。
    件名などの中身は混ぜない。一括実行だけ（予約の 1 人実行では送らない）
  - main() の実行結果が JSON で出る（別プロセスで configure_logging を実際に通す）

日付は calendar_window.now_jst と runner の _digest_day / _handoff_now を固定する。
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import importlib.util
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import psycopg
import pytest
from structlog.testing import capture_logs

from teamagent.skills.morning_digest import calendar_window as calwin
from teamagent.skills.morning_digest.schema import (
    CalendarEventItem,
    MailDigestItem,
    MorningDigestOutput,
    SlackUnreadItem,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_morning_digest_fargate.py"
JST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime(2026, 10, 1, 9, 30, tzinfo=JST)
DAY = NOW.date()
ME = "owner@vectorinc.co.jp"
SECRET_SUBJECT = "極秘案件Xの見積もり"
SECRET_WHO = "相手先 太郎"

OLD_FOOTER = (
    "_Aico｜本人だけに届く DM です（件名・相手は実名表示／監査ログ側はマスク）。"
    "下書きはボタンを押した時に生成し、送信はされません（手動送信）。_"
)


def _load() -> Any:
    name = "run_morning_digest_fetch_status_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load()

_ENV_KEYS = (
    "MORNING_DIGEST_FETCH_STATUS_EMAILS",
    "MORNING_DIGEST_ADMIN_REPORT_EMAILS",
    "MORNING_DIGEST_COMPACT",
    "MORNING_DIGEST_ACK_BUTTON",
    "MORNING_DIGEST_CALENDAR_BUTTON",
    "MORNING_DIGEST_SCHEDULE_BUTTON",
    "MORNING_DIGEST_REMINDERS",
    "MORNING_DIGEST_USERS",
    "MORNING_DIGEST_EXCLUDE",
    "MORNING_DIGEST_MODE",
    "MORNING_DIGEST_USER_REF",
    "MORNING_DIGEST_PERSONALIZED",
    "MORNING_DIGEST_DATE",
    "MORNING_DIGEST_SLACK_UNREAD",
    "DIGEST_INTERNAL_DOMAIN",
    "USE_SLACK_CONTEXT",
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(calwin, "now_jst", lambda: NOW)
    monkeypatch.setattr(mod, "_handoff_now", lambda: NOW)
    monkeypatch.setattr(mod, "_digest_day", lambda: DAY)


RENDERERS = {
    "normal": lambda d, email=ME: mod._format_block_kit(d, email),
    "compact": lambda d, email=ME: mod._format_block_kit_compact(d, email),
}


# ── フィクスチャ ─────────────────────────────────────────────────────────


def _mail(i: int, *, high: bool, tokens: bool = False, has_draft: bool = False) -> MailDigestItem:
    return MailDigestItem(
        counterpart_masked=f"u{i}***@client.example",
        counterpart_display=SECRET_WHO,
        subject_scrubbed="件名",
        subject_display=f"{SECRET_SUBJECT}{i}",
        importance="high" if high else "medium",
        to_self=high,
        is_unread=not high,
        summary="要約",
        deadline="10/3まで" if high else None,
        ask="見積もりの確認" if high else "",
        has_draft=has_draft,
        draft_token=f"d{i}" if tokens else "",
        event_token=f"e{i}" if tokens else "",
        meeting_start="2026-10-02T14:00:00+09:00" if tokens else None,
        scheduling_request=tokens,
        ack_token=f"a{i}" if tokens else "",
        thread_ok=True,
    )


def _cal(i: int) -> CalendarEventItem:
    return CalendarEventItem(
        summary_scrubbed=f"予定{i}",
        summary_display=f"予定{i}",
        start_at=f"2026-10-01T{10 + i % 8:02d}:00:00+09:00",
        end_at=f"2026-10-01T{11 + i % 8:02d}:00:00+09:00",
    )


def _slack(i: int, *, tokens: bool = False) -> SlackUnreadItem:
    return SlackUnreadItem(
        channel_id=f"C0CHAN{i:05d}",
        channel_kind="channel",
        channel_name_display=f"ch-{i}",
        excerpt_display="確認お願いします",
        occurred_at="2026-09-30T09:00:00+09:00",
        permalink=f"https://vector.slack.com/archives/C0CHAN{i:05d}/p17186814000001{i:02d}",
        thread_message_count=2,
        ack_token=f"s{i}" if tokens else "",
    )


def _digest(
    *,
    mail: str = "ok",
    cal: str = "ok",
    n_high: int = 0,
    n_unread: int = 0,
    n_cal: int = 0,
    n_slack: int = 0,
    threads_failed: int = 0,
    tokens: bool = False,
    draft_mode: str = "on_demand",
    draft_limit: int = 0,
) -> MorningDigestOutput:
    return MorningDigestOutput(
        user_email_masked="o***@vectorinc.co.jp",
        mail_digest=[_mail(i, high=True, tokens=tokens, has_draft=i == 0) for i in range(n_high)]
        + [_mail(100 + i, high=False, tokens=tokens) for i in range(n_unread)],
        calendar_events=[_cal(i) for i in range(n_cal)],
        calendar_date=DAY.isoformat(),
        slack_unread=[_slack(i, tokens=tokens) for i in range(n_slack)],
        slack_unread_total=n_slack,
        slack_unread_scanned=True,
        ack_all_token="all" if tokens else "",
        mail_fetch=mail,  # type: ignore[arg-type]
        calendar_fetch=cal,  # type: ignore[arg-type]
        mail_threads_failed=threads_failed,
        draft_mode=draft_mode,  # type: ignore[arg-type]
        draft_limit=draft_limit,
    )


def _all_text(rendered: tuple[str, list[dict[str, Any]]]) -> str:
    text, blocks = rendered
    return text + "\n" + json.dumps(blocks, ensure_ascii=False)


def _footer(blocks: list[dict[str, Any]]) -> str:
    last = blocks[-1]
    assert last["type"] == "context"
    return str(last["elements"][0]["text"])


# ── フラグ OFF は取得状態を見ない ─────────────────────────────────────────


@pytest.mark.parametrize("renderer", RENDERERS)
@pytest.mark.parametrize(
    ("mail", "cal"),
    [
        ("token_expired", "token_expired"),
        ("temporary", "ok"),
        ("ok", "scope_missing"),
        ("unknown", "unknown"),
    ],
)
@pytest.mark.parametrize("allow", ["", "someone-else@vectorinc.co.jp"])
def test_off_renders_exactly_as_before_even_when_fetch_failed(
    monkeypatch: pytest.MonkeyPatch, renderer: str, mail: str, cal: str, allow: str
) -> None:
    if allow:
        monkeypatch.setenv("MORNING_DIGEST_FETCH_STATUS_EMAILS", allow)
    failed = RENDERERS[renderer](_digest(mail=mail, cal=cal, threads_failed=3))
    normal = RENDERERS[renderer](_digest())
    assert failed == normal
    # 従来の（嘘になりうる）表示のまま＝OFF で挙動が変わっていない証拠。
    assert "📭 *メール*: 新着なし" in _all_text(failed)
    assert "10/1(木) の予定*: なし" in _all_text(failed)
    assert "確認できませんでした" not in _all_text(failed)


@pytest.mark.parametrize("renderer", RENDERERS)
def test_on_does_not_change_a_normal_day(monkeypatch: pytest.MonkeyPatch, renderer: str) -> None:
    d = _digest(n_high=3, n_unread=4, n_cal=5, n_slack=2)
    off = RENDERERS[renderer](d)
    monkeypatch.setenv("MORNING_DIGEST_FETCH_STATUS_EMAILS", "*")
    assert RENDERERS[renderer](d) == off


# ── フラグ ON: 取れなかった節 ───────────────────────────────────────────────


@pytest.mark.parametrize("renderer", RENDERERS)
@pytest.mark.parametrize("allow", ["*", "Owner@VectorInc.co.jp", f"x@vectorinc.co.jp, {ME}"])
def test_token_expired_says_unconfirmed_and_guides_to_reconnect_once(
    monkeypatch: pytest.MonkeyPatch, renderer: str, allow: str
) -> None:
    monkeypatch.setenv("MORNING_DIGEST_FETCH_STATUS_EMAILS", allow)
    text, blocks = RENDERERS[renderer](_digest(mail="token_expired", cal="token_expired"))
    body = json.dumps(blocks, ensure_ascii=False)
    assert "新着なし" not in body
    assert "予定*: なし" not in body
    assert (
        "⚠️ *メール*: 確認できませんでした（連携切れ。新着が無いという意味ではありません）" in body
    )
    assert (
        "⚠️ *10/1(木) の予定*: 確認できませんでした（連携切れ。予定が無いという意味ではありません）"
        in body
    )
    # 案内はメールと予定が両方だめでも 1 つ・見出しの直後。
    assert body.count("この DM で「連携」と送っていただければ") == 1
    assert "メールと予定を確認できませんでした" in blocks[2]["text"]["text"]
    # 認可 URL は貼らない（30 分で失効・再タイプ事故の実績）。
    assert "oauth2" not in body and "accounts.google.com" not in body
    # 通知のプレビューにも ⚠️。
    assert text.startswith(
        "⚠️ Google の連携が切れています（メールと予定を確認できませんでした）"
    ) or (
        text.startswith("朝ダイジェスト｜⚠️ Google の連携が切れています")
        and text.endswith("要返信–・未確認–・Slack0・予定–")
    )


def test_compact_header_shows_dash_instead_of_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MORNING_DIGEST_FETCH_STATUS_EMAILS", "*")
    _, blocks = mod._format_block_kit_compact(_digest(mail="temporary", n_cal=2), ME)
    assert blocks[0]["text"]["text"].endswith("｜🔴–・📬–・💬0・📅2")


@pytest.mark.parametrize("renderer", RENDERERS)
def test_temporary_failure_never_guides_to_reconnect(
    monkeypatch: pytest.MonkeyPatch, renderer: str
) -> None:
    """設定不備（invalid_client）等の朝に、全員へ「再連携して」と出さない。"""
    monkeypatch.setenv("MORNING_DIGEST_FETCH_STATUS_EMAILS", "*")
    all_text = _all_text(RENDERERS[renderer](_digest(mail="temporary", cal="temporary")))
    assert "連携" not in all_text
    assert (
        "確認できませんでした（取得・整理の途中で失敗しました。新着が無いという意味ではありません）"
        in all_text
    )
    assert "|受信トレイを開く>" in all_text
    assert "|カレンダーを開く>" in all_text


@pytest.mark.parametrize("renderer", RENDERERS)
def test_scope_missing_on_calendar_only(monkeypatch: pytest.MonkeyPatch, renderer: str) -> None:
    monkeypatch.setenv("MORNING_DIGEST_FETCH_STATUS_EMAILS", "*")
    _, blocks = RENDERERS[renderer](_digest(cal="scope_missing", n_high=1))
    body = json.dumps(blocks, ensure_ascii=False)
    assert "予定を確認する権限が足りないため" in blocks[2]["text"]["text"]
    assert "すべての項目にチェックを入れて" in body
    assert "確認できませんでした（権限不足。予定が無いという意味ではありません）" in body
    assert "メール*: 確認できませんでした" not in body  # メールは取れている


@pytest.mark.parametrize("renderer", RENDERERS)
def test_unknown_state_is_unconfirmed_not_none(
    monkeypatch: pytest.MonkeyPatch, renderer: str
) -> None:
    """状態が付いていない（旧 output 等）は「なし」ではなく確認できなかった側に倒す。"""
    monkeypatch.setenv("MORNING_DIGEST_FETCH_STATUS_EMAILS", "*")
    d = MorningDigestOutput(user_email_masked="o***", calendar_date=DAY.isoformat())
    body = _all_text(RENDERERS[renderer](d))
    assert "取得できたか確かめられませんでした" in body
    assert "新着なし" not in body
    assert "この DM で「連携」" not in body  # 原因が分からないときは再連携へ誘導しない


@pytest.mark.parametrize("renderer", RENDERERS)
def test_partially_unreadable_threads(monkeypatch: pytest.MonkeyPatch, renderer: str) -> None:
    monkeypatch.setenv("MORNING_DIGEST_FETCH_STATUS_EMAILS", "*")
    with_items = _all_text(RENDERERS[renderer](_digest(n_high=1, threads_failed=3)))
    assert "⚠️ ほか3件のメールは読み込めませんでした" in with_items
    without_items = _all_text(RENDERERS[renderer](_digest(threads_failed=2)))
    assert (
        "⚠️ *メール*: 2件のメールを読み込めませんでした（新着が無いという意味ではありません）"
        in without_items
    )
    assert "新着なし" not in without_items


@pytest.mark.parametrize("renderer", RENDERERS)
def test_reminder_clause_only_when_reminders_are_on(
    monkeypatch: pytest.MonkeyPatch, renderer: str
) -> None:
    monkeypatch.setenv("MORNING_DIGEST_FETCH_STATUS_EMAILS", "*")
    d = _digest(cal="token_expired")
    assert "リマインド" not in _all_text(RENDERERS[renderer](d))
    monkeypatch.setenv("MORNING_DIGEST_REMINDERS", "1")
    assert "本日の予定リマインドもお送りできません" in _all_text(RENDERERS[renderer](d))


# ── 50 ブロックの最終ガード ─────────────────────────────────────────────────


def _buttons_on(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "MORNING_DIGEST_ACK_BUTTON",
        "MORNING_DIGEST_CALENDAR_BUTTON",
        "MORNING_DIGEST_SCHEDULE_BUTTON",
    ):
        monkeypatch.setenv(key, "1")


@pytest.mark.parametrize("renderer", RENDERERS)
@pytest.mark.parametrize(("mail", "cal"), [("ok", "scope_missing"), ("scope_missing", "ok")])
def test_worst_case_day_fits_and_keeps_notice_and_calendar(
    monkeypatch: pytest.MonkeyPatch, renderer: str, mail: str, cal: str
) -> None:
    monkeypatch.setenv("MORNING_DIGEST_FETCH_STATUS_EMAILS", "*")
    _buttons_on(monkeypatch)
    d = _digest(
        mail=mail,
        cal=cal,
        n_high=12 if mail == "ok" else 0,
        n_unread=13 if mail == "ok" else 0,
        n_cal=20 if cal == "ok" else 0,
        n_slack=10,
        tokens=True,
        threads_failed=4 if mail == "ok" else 0,
        draft_mode="auto",
        draft_limit=5,
    )
    _, blocks = RENDERERS[renderer](d)
    body = json.dumps(blocks, ensure_ascii=False)
    assert len(blocks) <= 48
    assert "権限が足りないため" in blocks[2]["text"]["text"]
    assert "10/1(木) の予定" in body
    assert "作っておきます" in _footer(blocks)


def _ack_all_present(blocks: list[dict[str, Any]]) -> bool:
    return any(
        el.get("text", {}).get("text") == "☑️ 全部確認した"
        for b in blocks
        if b["type"] == "actions"
        for el in b.get("elements", [])
    )


@pytest.mark.parametrize("renderer", RENDERERS)
def test_guard_cuts_mail_list_first_and_never_the_notice_or_calendar(
    monkeypatch: pytest.MonkeyPatch, renderer: str
) -> None:
    """上限を下げて最終ガードを実際に働かせる（本番の上限では最悪ケースでも収まるため）。

    少しだけ溢れる日: 未確認（低優先）から削り、次に要返信の **後ろの件** から削る。
    """
    monkeypatch.setenv("MORNING_DIGEST_FETCH_STATUS_EMAILS", "*")
    _buttons_on(monkeypatch)
    d = _digest(cal="token_expired", n_high=5, n_unread=6, n_slack=3, tokens=True)
    monkeypatch.setattr(mod, "_COMPACT_MAX_BLOCKS", 1000)
    natural = len(RENDERERS[renderer](d)[1])
    limit = natural - 5  # 未確認（2 ブロック）＋ 要返信 1〜2 件ぶん溢れさせる
    monkeypatch.setattr(mod, "_COMPACT_MAX_BLOCKS", limit)
    _, blocks = RENDERERS[renderer](d)
    body = json.dumps(blocks, ensure_ascii=False)
    assert len(blocks) <= limit
    assert "この DM で「連携」と送っていただければ" in body  # 案内は残る
    assert "10/1(木) の予定*: 確認できませんでした" in body  # 今日の予定の節は残る
    assert "表示しきれない項目があります" in body
    assert "Aico｜" in _footer(blocks) and _ack_all_present(blocks)  # 末尾は残る
    assert "📬 *未確認" not in body  # 未確認が先に消える
    assert f"{SECRET_SUBJECT}0" in body  # 要返信の先頭は残る
    assert f"{SECRET_SUBJECT}4" not in body  # 後ろの件から削る
    assert "Slack 返信漏れ" in body  # メールの一覧より先に Slack は削らない


@pytest.mark.parametrize("renderer", RENDERERS)
def test_guard_under_extreme_overflow_still_keeps_notice_calendar_and_tail(
    monkeypatch: pytest.MonkeyPatch, renderer: str
) -> None:
    monkeypatch.setenv("MORNING_DIGEST_FETCH_STATUS_EMAILS", "*")
    _buttons_on(monkeypatch)
    monkeypatch.setattr(mod, "_COMPACT_MAX_BLOCKS", 12)
    d = _digest(cal="token_expired", n_high=8, n_unread=6, n_slack=5, tokens=True)
    _, blocks = RENDERERS[renderer](d)
    body = json.dumps(blocks, ensure_ascii=False)
    assert len(blocks) <= 12
    assert "この DM で「連携」と送っていただければ" in body
    assert "10/1(木) の予定*: 確認できませんでした" in body
    assert "Aico｜" in _footer(blocks) and _ack_all_present(blocks)


def test_off_compact_guard_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """OFF の compact は従来どおり末尾から切る（今日の予定が消える旧挙動のまま＝変えていない）。"""
    monkeypatch.setattr(mod, "_COMPACT_MAX_BLOCKS", 16)
    d = _digest(n_high=8, n_unread=6, n_cal=3)
    _, blocks = mod._format_block_kit_compact(d, ME)
    body = json.dumps(blocks, ensure_ascii=False)
    assert len(blocks) <= 16
    assert "の予定（3件）" not in body


# ── 末尾の説明文（draft_mode 3 通り）───────────────────────────────────────


@pytest.mark.parametrize("renderer", RENDERERS)
def test_footer_matches_how_drafts_are_really_made(renderer: str) -> None:
    auto = _footer(RENDERERS[renderer](_digest(draft_mode="auto", draft_limit=5))[1])
    assert auto == (
        "_Aico｜本人だけに届く DM です（件名・相手は実名表示／監査ログ側はマスク）。"
        "重要で本人宛てのメールには、Aico が返信の下書きを Gmail に作っておきます"
        "（最大 5 件・日程の打診は除く）。送信はしません（送るかはご自身で）。_"
    )
    assert "ボタンを押した時に生成" not in auto
    on_demand = _footer(RENDERERS[renderer](_digest(draft_mode="on_demand"))[1])
    assert on_demand == OLD_FOOTER
    off = _footer(RENDERERS[renderer](_digest(draft_mode="off"))[1])
    assert off == "_Aico｜本人だけに届く DM です（件名・相手は実名表示／監査ログ側はマスク）。_"


# ── 管理者 DM: 宛先 ─────────────────────────────────────────────────────────


def test_admin_recipients_only_internal_domain_and_at_most_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "MORNING_DIGEST_ADMIN_REPORT_EMAILS",
        " A@VectorInc.co.jp, evil@gmail.com, x@sub.vectorinc.co.jp, x@vectorinc.co.jp.evil.com,"
        " C0CHANNEL, b@vectorinc.co.jp, a@vectorinc.co.jp, c@vectorinc.co.jp, d@vectorinc.co.jp",
    )
    assert mod._admin_report_recipients() == [
        "a@vectorinc.co.jp",
        "b@vectorinc.co.jp",
        "c@vectorinc.co.jp",
    ]


def test_admin_recipients_empty_when_unset() -> None:
    assert mod._admin_report_recipients() == []


def _user(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"id": "U0ADMIN01", "profile": {"email": ME}}
    base.update(over)
    return base


@pytest.mark.parametrize(
    ("user", "ok"),
    [
        (_user(), True),
        (_user(is_restricted=True), False),
        (_user(is_ultra_restricted=True), False),
        (_user(is_stranger=True), False),
        (_user(is_bot=True), False),
        (_user(deleted=True), False),
        (_user(profile={"email": "other@vectorinc.co.jp"}), False),
        (_user(id="B0BOT"), False),
        (None, False),
    ],
)
def test_lookup_must_be_an_internal_full_member(user: Any, ok: bool) -> None:
    assert mod._is_internal_member(user, ME) is ok


class _FakeSlackClient:
    def __init__(self, user: dict[str, Any], channel: str) -> None:
        self._user = user
        self._channel = channel
        self.opened: list[str] = []

    async def users_lookupByEmail(self, *, email: str) -> dict[str, Any]:  # noqa: N802
        return {"user": self._user}

    async def conversations_open(self, *, users: str) -> dict[str, Any]:
        self.opened.append(users)
        return {"channel": {"id": self._channel}}


class _FakeSlack:
    def __init__(self, user: dict[str, Any], channel: str) -> None:
        self._client = _FakeSlackClient(user, channel)
        self.posted: list[tuple[str, str]] = []

    async def post_message(self, *, channel: str, text: str, **_: Any) -> Any:
        self.posted.append((channel, text))
        return type("R", (), {"ok": True})()


@pytest.mark.parametrize(
    ("user", "channel", "sent", "refused"),
    [
        (_user(), "D0ADMINDM", 1, 0),
        (_user(), "C0PUBLIC1", 0, 1),
        (_user(), "G0PRIVATE", 0, 1),
        (_user(is_restricted=True), "D0ADMINDM", 0, 1),
    ],
)
def test_admin_dm_goes_only_to_a_verified_im(
    monkeypatch: pytest.MonkeyPatch, user: dict[str, Any], channel: str, sent: int, refused: int
) -> None:
    from teamagent.adapters import slack_client

    fake = _FakeSlack(user, channel)
    monkeypatch.setattr(slack_client.SlackClient, "from_env", classmethod(lambda cls: fake))
    result = asyncio.run(mod._deliver_admin_report([ME], "本文"))
    assert result == (sent, refused)
    assert all(ch.startswith("D") for ch, _ in fake.posted)
    if user.get("is_restricted"):
        assert fake._client.opened == []  # 検査で止めたら DM を開きもしない


# ── 管理者 DM: 本文 ─────────────────────────────────────────────────────────


def _outcome(email: str, digest: Any, status: str = "delivered", **kw: Any) -> Any:
    return mod._user_outcome(email, status, kw.get("reason", ""), digest, kw.get("error", ""))


def test_admin_report_is_one_line_on_a_good_day() -> None:
    outcomes = [_outcome(f"u{i}@vectorinc.co.jp", _digest(n_high=2)) for i in range(3)]
    text, problem = mod._format_admin_report(outcomes, day=DAY, users=3)
    assert not problem
    assert text == (
        "🔧 朝ダイジェスト 10/1(木) の実行結果（管理者向け）｜対象 3・配信 3・配信失敗 0・未連携 0"
    )


def test_admin_report_breakdown_on_a_bad_day_without_contents() -> None:
    expired = _digest(mail="token_expired", cal="token_expired", n_high=0)
    expired.mail_fetch_detail = "RefreshError:invalid_grant"
    expired.calendar_fetch_detail = "RefreshError:invalid_grant"
    temporary = _digest(mail="temporary", n_high=0)
    temporary.mail_fetch_detail = "HttpError:503"
    partial = _digest(n_high=2, threads_failed=3)
    outcomes = [
        _outcome("t-yamada@vectorinc.co.jp", expired),
        _outcome("k-sato@vectorinc.co.jp", temporary),
        _outcome("m-ito@vectorinc.co.jp", partial),
        _outcome(
            "n-kato@vectorinc.co.jp",
            _digest(n_high=1),
            status="error",
            reason="deliver_failed",
            error="not_delivered",
        ),
        _outcome("x-new@vectorinc.co.jp", None, status="skipped", reason="not_connected"),
    ]
    text, problem = mod._format_admin_report(outcomes, day=DAY, users=5)
    assert problem
    lines = text.split("\n")
    assert lines[0].endswith("｜対象 5・配信 3・配信失敗 1・未連携 1")
    assert "取得できなかった: メール 2 人・予定 1 人" in text
    assert "・再連携が必要（連携切れ）: 1 人（t-yamada）" in text
    assert "※ うち 1 人には案内を表示していません" in text  # FETCH_STATUS の対象外
    assert "・一時的な失敗（再連携は案内していません）: 1 人" in text
    assert "・一部のメールを読み込めなかった: 1 人（計 3 件）" in text
    assert "・配信できなかった: 1 人（n-kato）" in text
    assert "RefreshError:invalid_grant×2" in text and "HttpError:503×1" in text
    # 件名・相手・要約・フル email は 1 文字も入れない。
    for leak in (SECRET_SUBJECT, SECRET_WHO, "要約", "@vectorinc.co.jp", "見積もり"):
        assert leak not in text


def test_admin_report_says_target_fetch_failed() -> None:
    text, problem = mod._format_admin_report([], day=DAY, users=0, target_error="OperationalError")
    assert problem
    assert "⚠️ 対象者を取得できず、誰にも配信していません（原因: OperationalError）" in text


def test_admin_report_flags_a_day_with_only_partially_unreadable_mail() -> None:
    """取得はすべて ok でも、スレッドが一部読めなかった日は「問題あり」で内訳を出す。

    変異: problem の判定から partial を外すと、この日が 1 行だけになって赤。
    """
    outcomes = [
        _outcome("m-ito@vectorinc.co.jp", _digest(n_high=2, threads_failed=2)),
        _outcome("ok@vectorinc.co.jp", _digest(n_high=1)),
    ]
    text, problem = mod._format_admin_report(outcomes, day=DAY, users=2)
    assert problem
    assert text.split("\n")[0].endswith("｜対象 2・配信 2・配信失敗 0・未連携 0")
    assert "・一部のメールを読み込めなかった: 1 人（計 2 件）" in text
    assert "取得できなかった" not in text  # 取得そのものは成功している


def test_admin_report_codes_are_sanitised_even_if_a_detail_carries_contents() -> None:
    """内訳コード・配信失敗の型名・対象者の取得失敗の型名に中身が紛れても、記号・空白・
    日本語は落ちる（_safe_code は管理者 DM に中身を混ぜない最後の砦）。

    変異: _safe_code を素通しにすると、件名・相手・「<」「@」が本文に出て赤。
    """
    dirty = _digest(mail="temporary", n_high=0)
    dirty.mail_fetch_detail = f"HttpError:503 {SECRET_SUBJECT} <{SECRET_WHO}@client.example>"
    outcomes = [
        _outcome("k-sato@vectorinc.co.jp", dirty),
        _outcome(
            "n-kato@vectorinc.co.jp",
            _digest(n_high=1),
            status="error",
            reason="deliver_failed",
            error=f"Boom {SECRET_WHO}; 件名={SECRET_SUBJECT}",
        ),
    ]
    text, problem = mod._format_admin_report(
        outcomes, day=DAY, users=2, target_error=f"Operational Error {SECRET_SUBJECT}"
    )
    assert problem
    for leak in (SECRET_SUBJECT, SECRET_WHO, "<", "@", ";", "=", "client.example"):
        assert leak not in text, leak
    breakdown = next(line for line in text.split("\n") if line.startswith("原因の内訳: "))
    items = breakdown.removeprefix("原因の内訳: ").split(", ")
    assert items and all(re.fullmatch(r"[A-Za-z0-9_:]+×\d+", item) for item in items), items
    # 件名の中の ASCII（"X"）だけは英数字なので残る。記号・空白・日本語は 1 文字も残らない。
    assert re.search(r"（原因: OperationalError[A-Za-z0-9_:]*）", text)


# ── 管理者 DM: 誰にも届かなかった朝を「問題なし」にしない ────────────────────


class _DeliveryPg:
    """DigestDeliveryStore が使う pg の形（connection → cursor.execute / rowcount）。

    ``fail`` は本番で起きた失敗の形: 最小権限ロールで ON CONFLICT が
    ``InsufficientPrivilege`` で落ちる（2026-08-14 の事故と同じ例外）。
    ``taken`` は一意制約で 2 回目の INSERT が 0 行になる形（既に別経路が送った）。
    """

    def __init__(self, *, fail: bool = False, taken: bool = False) -> None:
        self.fail = fail
        self.taken = taken

    @contextlib.contextmanager
    def connection(self, **_: Any) -> Any:
        yield self

    @contextlib.contextmanager
    def cursor(self) -> Any:
        yield self

    rowcount = 0

    def execute(self, sql: str, params: Any = None) -> None:
        if self.fail:
            raise psycopg.errors.InsufficientPrivilege(
                "permission denied for table digest_delivery"
            )
        self.rowcount = 0 if (self.taken or params is None) else 1

    def fetchone(self) -> Any:
        # claim が取れなかったときの「誰が持っているか」照会（0031）。別経路が送った行。
        return ("scheduled",) if self.taken else None

    def commit(self) -> None:
        return None


class _NeverRunSkill:
    def run(self, _inp: Any, _ctx: Any) -> Any:
        raise AssertionError("配信権を取れなかった人の skill は呼ばない（fail-closed）")


def _claim_outcomes(pg: _DeliveryPg, n: int) -> list[Any]:
    from teamagent.adapters.digest_delivery_store import DigestDeliveryStore

    store = DigestDeliveryStore(pg)
    outcomes: list[Any] = []
    for i in range(n):
        result = mod._process_user(
            _NeverRunSkill(),
            None,
            f"u{i}@vectorinc.co.jp",
            store=store,
            day=DAY,
            origin="bulk",
            sink=outcomes,
        )
        assert result == "skipped"  # 戻り値の型と意味は変えない
    return outcomes


def test_claim_db_failure_is_a_problem_not_already_delivered() -> None:
    """配信権の DB 確認が落ちた朝（誰にも届かない）を「送信済み」と数えて「問題なし」にしない。

    変異: _process_user で claim の失敗を already_delivered に戻す／problem の判定から外す／
    DigestDeliveryStore.claim_result の例外を CLAIM_TAKEN に倒す、のどれでも赤。
    """
    outcomes = _claim_outcomes(_DeliveryPg(fail=True), 23)
    text, problem = mod._format_admin_report(outcomes, day=DAY, users=23)
    assert problem
    head = text.split("\n")[0]
    assert head.endswith("｜対象 23・配信 0・配信失敗 0・未連携 0・送信の確認失敗 23")
    assert "送信済み" not in head
    assert "・送信済みかを DB で確かめられず、送らなかった: 23 人" in text


def test_already_delivered_by_another_route_is_not_a_problem() -> None:
    """一意制約で既に取られていた（予約の 1 人実行が送った）は正常＝1 行のまま。"""
    outcomes = _claim_outcomes(_DeliveryPg(taken=True), 3)
    text, problem = mod._format_admin_report(outcomes, day=DAY, users=3)
    assert not problem
    assert text.endswith("｜対象 3・配信 0・配信失敗 0・未連携 0・送信済み 3")


def test_store_without_claim_result_keeps_the_old_meaning() -> None:
    """claim（真偽）しか持たないストアは、False を従来どおり「送信済み」とみなす。"""

    class _BoolStore:
        def claim(self, *_: Any, **__: Any) -> bool:
            return False

    outcomes: list[Any] = []
    mod._process_user(
        _NeverRunSkill(), None, ME, store=_BoolStore(), day=DAY, origin="bulk", sink=outcomes
    )
    assert [o.reason for o in outcomes] == ["already_delivered"]


class _RdsCursor:
    def __init__(self, rows: list[tuple[str]]) -> None:
        self.rows = rows

    def __enter__(self) -> _RdsCursor:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def execute(self, _sql: str, *_: Any) -> None:
        return None

    def fetchall(self) -> list[tuple[str]]:
        return self.rows


class _RdsConn:
    def __init__(self, rows: list[tuple[str]]) -> None:
        self.rows = rows

    def __enter__(self) -> _RdsConn:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def cursor(self) -> _RdsCursor:
        return _RdsCursor(self.rows)


def _fake_rds(monkeypatch: pytest.MonkeyPatch, rows: list[tuple[str]]) -> None:
    import types

    monkeypatch.setitem(
        sys.modules, "psycopg", types.SimpleNamespace(connect=lambda dsn: _RdsConn(rows))
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://x@localhost/db")


@pytest.mark.parametrize(
    ("rows", "expected"),
    [([], "rds_zero_rows"), ([("A@vectorinc.co.jp",)], None)],
)
def test_rds_zero_rows_is_marked_as_a_target_failure(
    monkeypatch: pytest.MonkeyPatch, rows: list[tuple[str]], expected: str | None
) -> None:
    _fake_rds(monkeypatch, rows)
    monkeypatch.setattr(mod, "_TARGET_FETCH_ERROR", None)
    mod._fetch_connected_users_from_rds()
    assert mod._TARGET_FETCH_ERROR == expected


def test_main_reports_rds_zero_rows_instead_of_a_quiet_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RDS が例外なしで 0 行を返した朝（GUC・RLS が崩れた形）は、「対象 0」の 1 行で済ませず
    ⚠️ で知らせ、ERROR のイベント（警報の対象）も出す。

    変異: 0 行の印を付けない／main() の ERROR を出さない、のどちらでも赤。
    """
    monkeypatch.setenv("MORNING_DIGEST_ADMIN_REPORT_EMAILS", ME)
    _fake_rds(monkeypatch, [])
    calls: list[str] = []

    async def _admin(recipients: list[str], text: str) -> tuple[int, int]:
        calls.append(text)
        return (1, 0)

    monkeypatch.setattr(mod, "_deliver_admin_report", _admin)
    with capture_logs() as logs:
        assert mod.main() == 0
    assert len(calls) == 1
    assert "｜対象 0・配信 0" in calls[0]
    assert "⚠️ 連携済みの対象者が 0 人と返り、誰にも配信していません" in calls[0]
    failed = [e for e in logs if e["event"] == "morning_digest_target_fetch_failed"]
    assert [(e["err"], e["log_level"]) for e in failed] == [("rds_zero_rows", "error")]


# ── main() の結合 ───────────────────────────────────────────────────────────


def _patch_main(monkeypatch: pytest.MonkeyPatch, digests: dict[str, Any]) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"admin": [], "deliver": []}
    monkeypatch.setattr(mod, "_resolve_target_users", lambda: list(digests))
    monkeypatch.setattr(mod, "_build_token_store", lambda: object())

    class _FakeSkill:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def run(self, _inp: Any, ctx: Any) -> Any:
            d = digests[ctx.metadata["user_email"]]
            if isinstance(d, BaseException):
                raise d
            return d

    monkeypatch.setattr("teamagent.skills.morning_digest.skill.MorningDigestSkill", _FakeSkill)

    async def _deliver(email: str, text: str, blocks: Any) -> tuple[bool, str]:
        calls["deliver"].append((email, text))
        return (True, "D0USER")

    async def _admin(recipients: list[str], text: str) -> tuple[int, int]:
        calls["admin"].append((recipients, text))
        return (len(recipients), 0)

    monkeypatch.setattr(mod, "_deliver_to_slack", _deliver)
    monkeypatch.setattr(mod, "_deliver_admin_report", _admin)
    return calls


def test_main_sends_one_admin_report_in_bulk_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MORNING_DIGEST_ADMIN_REPORT_EMAILS", ME)
    expired = _digest(mail="token_expired", cal="token_expired")
    calls = _patch_main(
        monkeypatch,
        {
            "t-yamada@vectorinc.co.jp": expired,
            "ok@vectorinc.co.jp": _digest(n_high=1),
            "new@vectorinc.co.jp": PermissionError("未連携"),
        },
    )
    assert mod.main() == 0
    assert len(calls["admin"]) == 1
    recipients, text = calls["admin"][0]
    assert recipients == [ME]
    assert "対象 3・配信 2・配信失敗 0・未連携 1" in text
    assert "（t-yamada）" in text
    assert SECRET_SUBJECT not in text


def test_main_without_admin_env_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_main(monkeypatch, {"a@vectorinc.co.jp": _digest(mail="temporary")})
    assert mod.main() == 0
    assert calls["admin"] == []


def test_main_single_user_run_does_not_send_admin_report(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MORNING_DIGEST_ADMIN_REPORT_EMAILS", ME)
    calls = _patch_main(monkeypatch, {"a@vectorinc.co.jp": _digest(mail="temporary")})
    monkeypatch.setattr(mod, "_mode", lambda: "single")
    monkeypatch.setattr(mod, "_resolve_single_user", lambda users: users[0])
    assert mod.main() == 0
    assert len(calls["deliver"]) == 1
    assert calls["admin"] == []


def test_main_reports_when_targets_could_not_be_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    """RDS から対象者を取れない朝は「対象 0 人」ではなく、誰にも届いていないと知らせる。"""
    monkeypatch.setenv("MORNING_DIGEST_ADMIN_REPORT_EMAILS", ME)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    calls: list[str] = []

    async def _admin(recipients: list[str], text: str) -> tuple[int, int]:
        calls.append(text)
        return (1, 0)

    monkeypatch.setattr(mod, "_deliver_admin_report", _admin)
    assert mod.main() == 0
    assert len(calls) == 1
    assert "対象者を取得できず、誰にも配信していません（原因: DATABASE_URL_missing）" in calls[0]


# ── 実行結果の JSON ログ（別プロセスで configure_logging を実際に通す）──────


_DRIVER = textwrap.dedent(
    """
    import asyncio, importlib.util, sys
    spec = importlib.util.spec_from_file_location("rmd_json_driver", sys.argv[1])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rmd_json_driver"] = mod
    spec.loader.exec_module(mod)
    from teamagent.skills.morning_digest.schema import MorningDigestOutput
    import teamagent.skills.morning_digest.skill as skill_mod

    class FakeSkill:
        def __init__(self, *a, **k):
            pass

        def run(self, inp, ctx):
            return MorningDigestOutput(
                user_email_masked="a***",
                mail_fetch="token_expired",
                calendar_fetch="ok",
                mail_fetch_detail="RefreshError:invalid_grant",
            )

    async def deliver(email, text, blocks):
        return (True, "D0USER")

    skill_mod.MorningDigestSkill = FakeSkill
    mod._resolve_target_users = lambda: ["json-user@vectorinc.co.jp"]
    mod._build_token_store = lambda: object()
    mod._deliver_to_slack = deliver
    sys.exit(mod.main())
    """
)


def test_main_emits_parseable_json_run_summary() -> None:
    env = {k: v for k, v in os.environ.items() if k not in _ENV_KEYS}
    env["STRUCTLOG_FORMAT"] = "json"
    proc = subprocess.run(
        [sys.executable, "-c", _DRIVER, str(SCRIPT_PATH)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(PROJECT_ROOT),
        timeout=90,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    docs = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    done = [d for d in docs if d.get("event") == "morning_digest_run_done"]
    assert len(done) == 1, proc.stdout[-2000:]
    assert done[0]["delivered"] == 1
    assert done[0]["token_expired"] == 1
    assert done[0]["mail_fetch_failed"] == 1
    assert done[0]["level"] == "info"
    # email はログに出さない（件数だけ）。
    assert "json-user" not in proc.stdout
