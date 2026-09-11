"""社外 MTG 判定・企業名抽出・注記の決定論テスト（変異テストの対象を含む）。

フェイクは本番の失敗モードを再現すること。とくに
``attendees=[self, organizer]`` × ``guestsCanSeeOtherGuests=false`` は
「Google がゲストリスト非表示時に空配列ではなく本人＋主催者を返す」実挙動の再現で、
``attendee_list_available = bool(attendees)` 実装を赤にするためにある。
"""

from __future__ import annotations

from teamagent.adapters.gcalendar_client import extract_events
from teamagent.skills.pre_meeting_brief.classify import (
    classify_external,
    external_use,
    extract_client,
    is_usable_partial,
    ng_note,
    normalize_company,
)
from teamagent.skills.pre_meeting_brief.signals import BriefSignals, build_signal_input

INTERNAL = frozenset({"vectorinc.co.jp"})


def _sig(**kw: object) -> BriefSignals:
    base: dict[str, object] = {"attendee_list_available": True}
    base.update(kw)
    return BriefSignals(**base)  # type: ignore[arg-type]


# ── S1: タイトルの強シグナル ──────────────────────────────────────────
def test_s1_bracket_gaishutsu_is_external() -> None:
    sig = _sig(title="【社外（外出）】初田製作所様_新卒採用")
    assert classify_external(sig, internal_domains=INTERNAL) == "external"


def test_s1_each_strong_word_is_external() -> None:
    for word in ("社外", "外出", "訪問", "来社", "商談", "打合せ", "テレカン"):
        sig = _sig(title=f"【{word}】ABC")
        assert classify_external(sig, internal_domains=INTERNAL) == "external", word


# ── S3 が X より先（評価順の固定） ────────────────────────────────────
def test_s3_beats_exclusion_word() -> None:
    """『週次ヨミ会』でも社外ドメイン参加者がいれば external。

    変異: 除外語(X)を S3 より先に評価すると internal になり赤。
    """
    sig = _sig(title="週次ヨミ会", attendee_domains=("dentsu.co.jp",))
    assert classify_external(sig, internal_domains=INTERNAL) == "external"


def test_internal_domain_only_is_not_external_by_s3() -> None:
    sig = _sig(title="定例ミーティング", attendee_domains=("vectorinc.co.jp",))
    assert classify_external(sig, internal_domains=INTERNAL) == "internal"


# ── W1 が X より先 ────────────────────────────────────────────────────
def test_honorific_beats_exclusion_word() -> None:
    """『田中様 提出物確認』は uncertain。

    変異: 除外語(X)を W1 より先に評価すると internal になり赤。
    """
    sig = _sig(title="田中様 提出物確認", attendee_domains=("vectorinc.co.jp",))
    assert classify_external(sig, internal_domains=INTERNAL) == "uncertain"


def test_exclusion_word_without_honorific_is_internal() -> None:
    sig = _sig(title="提出物確認", attendee_domains=("vectorinc.co.jp",))
    assert classify_external(sig, internal_domains=INTERNAL) == "internal"


# ── 参加者リスト不可視（Google の実挙動）───────────────────────────────
def test_guests_hidden_returns_self_and_organizer_not_empty() -> None:
    """本番の失敗モードの再現: 非表示でも attendees は空にならない。

    変異: ``attendee_list_available = bool(attendees)`` にすると True になり、
    判定が uncertain → internal へ落ちて赤。
    """
    raw = {
        "id": "e1",
        "summary": "打ち合わせ",
        "start": {"dateTime": "2026-09-11T14:00:00+09:00"},
        "end": {"dateTime": "2026-09-11T15:00:00+09:00"},
        "organizer": {"email": "boss@vectorinc.co.jp"},
        "guestsCanSeeOtherGuests": False,
        "attendees": [
            {"email": "me@vectorinc.co.jp", "self": True},
            {"email": "boss@vectorinc.co.jp"},
        ],
    }
    (detail,) = extract_events([raw], want_description=True)
    assert detail.attendees  # 空ではない（ここが従来実装を騙していた）
    assert detail.attendee_list_available is False
    sig = build_signal_input(detail)
    assert classify_external(sig, internal_domains=INTERNAL) == "uncertain"


def test_attendees_omitted_flag_marks_list_unavailable() -> None:
    raw = {
        "id": "e2",
        "summary": "商談準備",
        "start": {"dateTime": "2026-09-11T10:00:00+09:00"},
        "end": {"dateTime": "2026-09-11T11:00:00+09:00"},
        "attendeesOmitted": True,
        "organizer": {"email": "boss@vectorinc.co.jp"},
        "attendees": [{"email": "x@dentsu.co.jp"}],
    }
    (detail,) = extract_events([raw], want_description=True)
    assert detail.attendee_list_available is False


def test_all_day_event_is_out_of_scope() -> None:
    sig = _sig(title="【社外】終日イベント", all_day=True)
    assert classify_external(sig, internal_domains=INTERNAL) == "internal"


# ── 企業名の抽出（P1 → P2 → P3 → P4）──────────────────────────────────
def test_p1_description_beats_title() -> None:
    sig = _sig(
        title="【社外】B社様_案件",
        has_client_line=True,
        client_hint="A社",
    )
    assert extract_client(sig).clients == ("A社",)


def test_p2_underscore_suffix() -> None:
    sig = _sig(title="【社外（外出）】初田様_初田製作所")
    assert extract_client(sig).clients == ("初田製作所",)


def test_p3_before_honorific() -> None:
    sig = _sig(title="【社外】花王様 打合せ")
    assert extract_client(sig).clients == ("花王",)


def test_no_signal_yields_nothing() -> None:
    sig = _sig(title="打合せ", attendee_list_available=False)
    assert extract_client(sig).clients == ()


def test_agency_is_kept_with_person_name() -> None:
    """「代理店：電通（吉田様）」は担当者名まで残す（DELTA §3 差分 2）。"""
    raw = {
        "id": "e3",
        "summary": "【社外】電通吉田様",
        "start": {"dateTime": "2026-09-11T14:00:00+09:00"},
        "end": {"dateTime": "2026-09-11T15:00:00+09:00"},
        "description": "クライアント：富士急／代理店：電通（吉田様）",
    }
    (detail,) = extract_events([raw], want_description=True)
    hint = extract_client(build_signal_input(detail))
    assert hint.clients == ("富士急",)
    assert hint.agency_display == "電通(吉田様)"


def test_multiple_clients_up_to_two() -> None:
    raw = {
        "id": "e4",
        "summary": "【外出】電通浦部さま",
        "start": {"dateTime": "2026-09-11T15:00:00+09:00"},
        "end": {"dateTime": "2026-09-11T16:00:00+09:00"},
        "description": "クライアント：すかいらーく・ヤクルト／代理店：電通（浦部さま）",
    }
    (detail,) = extract_events([raw], want_description=True)
    hint = extract_client(build_signal_input(detail))
    assert hint.clients == ("すかいらーく", "ヤクルト")


# ── 名寄せ ────────────────────────────────────────────────────────────
def test_normalize_company_strips_corp_forms_and_notes() -> None:
    assert normalize_company("株式会社初田製作所様（代理店：電通）") == "初田製作所"
    assert normalize_company("㈱花王") == "花王"


def test_kao_two_chars_is_not_usable_as_partial_but_exact_is_allowed() -> None:
    """「花王」は 2 文字。部分一致だけに最小長を掛ける（完全一致は長さを問わない）。"""
    assert is_usable_partial("花王") is False
    assert is_usable_partial("初田製作所") is True


def test_stoplist_blocks_short_generic_words() -> None:
    for word in ("PR", "AI", "SNS", "IR", "DX"):
        assert is_usable_partial(word) is False


# ── 対外利用可否（3 値）──────────────────────────────────────────────
def test_external_use_three_values() -> None:
    assert external_use("NG") == "ng"
    assert external_use("対外利用不可") == "ng"
    assert external_use("confidential") == "ng"
    assert external_use("OK") == "ok"
    assert external_use("") == "unknown"
    assert external_use(None) == "unknown"
    assert external_use("要確認") == "unknown"


def test_unknown_does_not_use_warning_sign() -> None:
    """変異: unknown にも ⚠ を付けると赤（全件 ⚠ の狼少年化を止める）。"""
    note = ng_note("unknown", "")
    assert "⚠" not in note
    assert note == "（対外利用可否は資料で確認）"


def test_ng_note_shows_reason() -> None:
    assert ng_note("ng", "事例集フォルダが展開NG") == "⚠事例集フォルダが展開NG"
    assert ng_note("ng", "") == "⚠対外利用NG"
    assert ng_note("ok", "何か") == ""
