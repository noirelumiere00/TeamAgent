"""_shared/mail_compose.py の純粋ヘルパーのユニットテスト（課金0・I/O無し）。"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from teamagent.skills._shared.mail_compose import (
    build_cc,
    build_thread_history,
    env_bool,
    env_int,
    is_bulk_delivery,
    is_excluded_subject,
    is_mass_or_impersonal,
    should_skip_mail,
)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


@dataclass
class _Msg:
    id: str
    headers: dict[str, str]
    payload: dict[str, Any]
    internal_date_ms: int | None = None
    thread_id: str = "T1"


# ── build_cc ────────────────────────────────────────────────────────────────


def test_build_cc_includes_others_excludes_self_and_to() -> None:
    headers = {
        "From": "alice@ext.com",
        "To": "me@vectorinc.co.jp, carol@ext.com",
        "Cc": "dave@ext.com",
    }
    cc = build_cc(headers, "me@vectorinc.co.jp", "alice@ext.com")
    assert cc is not None
    addrs = {a.strip() for a in cc.split(",")}
    assert addrs == {"carol@ext.com", "dave@ext.com"}
    assert "me@vectorinc.co.jp" not in cc  # 本人除外
    assert "alice@ext.com" not in cc  # 主宛先除外


def test_build_cc_dedupes_case_insensitive() -> None:
    headers = {"From": "alice@ext.com", "To": "Carol@Ext.com, carol@ext.com", "Cc": ""}
    cc = build_cc(headers, "me@vectorinc.co.jp", "alice@ext.com")
    assert cc is not None
    assert cc.lower().count("carol@ext.com") == 1


def test_build_cc_returns_none_when_only_self_and_to() -> None:
    headers = {"From": "alice@ext.com", "To": "me@vectorinc.co.jp", "Cc": ""}
    assert build_cc(headers, "me@vectorinc.co.jp", "alice@ext.com") is None


def test_build_cc_excludes_bcc() -> None:
    headers = {
        "From": "alice@ext.com",
        "To": "me@vectorinc.co.jp",
        "Cc": "carol@ext.com",
        "Bcc": "secret@ext.com",
    }
    cc = build_cc(headers, "me@vectorinc.co.jp", "alice@ext.com")
    assert cc == "carol@ext.com"
    assert "secret@ext.com" not in (cc or "")


def test_build_cc_internal_only_filters_external() -> None:
    headers = {
        "From": "alice@ext.com",
        "To": "me@vectorinc.co.jp, colleague@vectorinc.co.jp",
        "Cc": "dave@ext.com",
    }
    cc = build_cc(
        headers,
        "me@vectorinc.co.jp",
        "alice@ext.com",
        internal_only_cc=True,
        company_domains=frozenset({"vectorinc.co.jp"}),
    )
    assert cc == "colleague@vectorinc.co.jp"


def test_build_cc_truncates_when_over_max() -> None:
    many = ", ".join(f"u{i}@ext.com" for i in range(25))
    headers = {"From": "alice@ext.com", "To": "me@vectorinc.co.jp", "Cc": many}
    assert build_cc(headers, "me@vectorinc.co.jp", "alice@ext.com", max_cc=20) is None


# ── build_thread_history ─────────────────────────────────────────────────────


def test_build_thread_history_excludes_target_and_orders_chronologically() -> None:
    msgs = [
        _Msg(
            id="m-new",
            headers={"From": "alice@ext.com"},
            payload={"mimeType": "text/plain", "body": {"data": _b64("最新メッセージ")}},
            internal_date_ms=200,
        ),
        _Msg(
            id="m-old",
            headers={"From": "me@vectorinc.co.jp"},
            payload={"mimeType": "text/plain", "body": {"data": _b64("最初の依頼です")}},
            internal_date_ms=100,
        ),
    ]
    hist = build_thread_history(msgs, exclude_id="m-new", requester="me@vectorinc.co.jp")
    assert "最初の依頼です" in hist
    assert "最新メッセージ" not in hist  # 返信対象は除外
    assert "自分(営業)" in hist  # 本人発の履歴は 自分 と表示


def test_build_thread_history_empty_when_only_target() -> None:
    msgs = [
        _Msg(
            id="m0",
            headers={"From": "alice@ext.com"},
            payload={"mimeType": "text/plain", "body": {"data": _b64("本文")}},
            internal_date_ms=100,
        )
    ]
    assert build_thread_history(msgs, exclude_id="m0", requester="me@vectorinc.co.jp") == ""


# ── env helpers ──────────────────────────────────────────────────────────────


def test_env_bool_and_int(monkeypatch: Any) -> None:
    monkeypatch.delenv("X_FLAG", raising=False)
    assert env_bool("X_FLAG", True) is True
    monkeypatch.setenv("X_FLAG", "off")
    assert env_bool("X_FLAG", True) is False
    monkeypatch.setenv("X_FLAG", "1")
    assert env_bool("X_FLAG", False) is True
    monkeypatch.delenv("X_NUM", raising=False)
    assert env_int("X_NUM", 1200) == 1200
    monkeypatch.setenv("X_NUM", "500")
    assert env_int("X_NUM", 1200) == 500
    monkeypatch.setenv("X_NUM", "bad")
    assert env_int("X_NUM", 1200) == 1200


# ── is_mass_or_impersonal ────────────────────────────────────────────────────


def test_mass_detected_by_bulk_headers() -> None:
    assert is_mass_or_impersonal({"From": "info@x.com", "List-Unsubscribe": "<u>"}, "本文") is True
    assert is_mass_or_impersonal({"From": "a@x.com", "Precedence": "bulk"}, "本文") is True
    assert (
        is_mass_or_impersonal({"From": "a@x.com", "Auto-Submitted": "auto-generated"}, "x") is True
    )


def test_mass_detected_by_noreply_sender() -> None:
    assert is_mass_or_impersonal({"From": "no-reply@x.com"}, "本文") is True
    assert is_mass_or_impersonal({"From": "DoNotReply@x.com"}, "本文") is True


def test_mass_detected_by_generic_salutation() -> None:
    assert is_mass_or_impersonal({"From": "a@x.com"}, "各位\nお世話になります。") is True
    assert is_mass_or_impersonal({"From": "a@x.com"}, "ご担当者様\n…") is True
    assert is_mass_or_impersonal({"From": "a@x.com"}, "\n\nみなさま\n…") is True


def test_personal_email_not_mass() -> None:
    assert (
        is_mass_or_impersonal({"From": "tanaka@x.com"}, "小俣様\nお世話になっております。") is False
    )


# ── 読む系の除外判定 ──────────────────────────────────────────────


def test_bulk_delivery_header_names_are_case_insensitive() -> None:
    assert is_bulk_delivery({"LIST-UNSUBSCRIBE": "<https://example.com/unsubscribe>"}) is True
    assert is_bulk_delivery({"List-ID": "sales.example.com"}) is True
    assert is_bulk_delivery({"precedence": " list "}) is True


def test_auto_submitted_no_is_not_bulk_delivery() -> None:
    assert is_bulk_delivery({"AUTO-SUBMITTED": " no ", "FROM": "person@example.com"}) is False


def test_excluded_subject_normalizes_width_and_case(monkeypatch: Any) -> None:
    monkeypatch.setenv("MAIL_EXCLUDE_SUBJECT_KEYWORDS", "ABC")
    assert is_excluded_subject({"subject": "ａｂｃ 進捗報告"}) is True


def test_bulk_kill_switch_only_disables_reading_filter(monkeypatch: Any) -> None:
    headers = {"LIST-ID": "sales.example.com"}
    monkeypatch.setenv("MAIL_EXCLUDE_BULK", "false")

    assert should_skip_mail(headers) is True
    assert (env_bool("MAIL_EXCLUDE_BULK", True) and should_skip_mail(headers)) is False
    assert is_mass_or_impersonal(headers, "本文") is True


def test_generic_salutation_is_excluded_only_from_drafts(monkeypatch: Any) -> None:
    monkeypatch.delenv("MAIL_EXCLUDE_SUBJECT_KEYWORDS", raising=False)
    headers = {"From": "person@example.com", "Subject": "全社連絡"}

    assert should_skip_mail(headers) is False
    assert is_mass_or_impersonal(headers, "各位\nお知らせします。") is True


def test_empty_subject_keywords_disable_subject_filter(monkeypatch: Any) -> None:
    monkeypatch.setenv("MAIL_EXCLUDE_SUBJECT_KEYWORDS", "")
    headers = {"From": "person@example.com", "Subject": "営業日報"}

    assert is_excluded_subject(headers) is False
    assert should_skip_mail(headers) is False
