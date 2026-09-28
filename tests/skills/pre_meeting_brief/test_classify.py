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
    sig = _sig(title="【社外（外出）】東光製作所様_新卒採用")
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
    sig = _sig(title="【社外（外出）】初田様_東光製作所")
    assert extract_client(sig).clients == ("東光製作所",)


def test_p3_before_honorific() -> None:
    sig = _sig(title="【社外】花王様 打合せ")
    assert extract_client(sig).clients == ("花王",)


def test_no_signal_yields_nothing() -> None:
    sig = _sig(title="打合せ", attendee_list_available=False)
    assert extract_client(sig).clients == ()


def test_agency_is_kept_with_person_name() -> None:
    """「代理店：青葉広告（山田様）」は担当者名まで残す（DELTA §3 差分 2）。"""
    raw = {
        "id": "e3",
        "summary": "【社外】青葉広告山田様",
        "start": {"dateTime": "2026-09-11T14:00:00+09:00"},
        "end": {"dateTime": "2026-09-11T15:00:00+09:00"},
        "description": "クライアント：北都リゾート／代理店：青葉広告（山田様）",
    }
    (detail,) = extract_events([raw], want_description=True)
    hint = extract_client(build_signal_input(detail))
    assert hint.clients == ("北都リゾート",)
    assert hint.agency_display == "青葉広告(山田様)"


def test_multiple_clients_up_to_two() -> None:
    raw = {
        "id": "e4",
        "summary": "【外出】青葉広告鈴木さま",
        "start": {"dateTime": "2026-09-11T15:00:00+09:00"},
        "end": {"dateTime": "2026-09-11T16:00:00+09:00"},
        "description": "クライアント：緑川フーズ／白水飲料／代理店：青葉広告（鈴木さま）",
    }
    (detail,) = extract_events([raw], want_description=True)
    hint = extract_client(build_signal_input(detail))
    assert hint.clients == ("緑川フーズ", "白水飲料")


def test_middle_dot_is_not_a_client_separator() -> None:
    """中黒は社名そのものに出る（ingest 側と同じ流儀）。

    変異: ``_CLIENT_SPLIT_CHARS`` へ「・」を戻すと、社名が 2 社に割れて赤。
    """
    from teamagent.skills.pre_meeting_brief.signals import split_clients

    assert split_clients("中央・製紙") == ["中央・製紙"]
    assert split_clients("中央・製紙／白水飲料") == ["中央・製紙", "白水飲料"]


def test_paren_notes_do_not_split_the_company_name() -> None:
    """括弧の中の「/」で社名が割れない。

    変異: ``_split_top_level`` を素朴な split に戻すと ('白水飲料(飲料', '健康)') で赤。
    """
    from teamagent.skills.pre_meeting_brief.signals import split_clients

    assert split_clients("白水飲料（飲料/健康）") == ["白水飲料(飲料/健康)"]


# ── スキーム無しのホスト名（第三者が書いたクリック先）────────────────────
#: 説明欄は社外の主催者が書ける **第三者入力**。裸のホスト名も Slack が自動リンク化する
#: ので、``http`` / ``://`` だけを見ていた旧実装は
#: ``代理店：evil.example.com/steal-this-token`` を素通りさせていた（2026-09-11 実証）。
#: ``harden`` は ``<`` ``>`` ``@`` ``&`` しか潰さないため無害化の当てにならない。
HOSTLIKE_MUST_BE_DISCARDED = [
    "evil.example.com/steal-this-token",
    "evil.example.com/x",
    "bit.ly/xYz9",
    "drive.google.com",
    "www.a.jp",
    "evil.example.com)",  # 閉じ括弧付き
    "a@b.com,",  # 読点付き
    "https://evil.example/steal",  # 旧実装でも止まっていた形（退行させない）
]

#: 捨てすぎていないことの担保。ここが全部空になると機能が死ぬ。
#: repo 内の実クライアント名 227 件（``株式会社``/``㈱`` を含む表記）を通して
#: 捨てられたのは 0 件（2026-09-11 実測）。
LEGITIMATE_NAMES_MUST_SURVIVE = [
    "北都リゾート",
    "青葉広告（山田様）",
    "株式会社A.B.C",  # ドットの後ろが 1 文字なのでホスト名に見えない
    "ドコモ.com",  # ドットの直前が非 ASCII
    "Co.,Ltd.",
    "No.1",
    "中央・製紙",
    "白水飲料(飲料/健康)",
    "花王株式会社",
    "ユニ・チャーム",
]


def test_hostlike_strings_are_discarded_entirely() -> None:
    """スキーム無しのホスト名は **1 文字も通さない**（丸ごと破棄）。

    変異: ``signals.py`` の ``if _HOSTLIKE_RE.search(text): return ""`` を落とすと、
    ``evil.example.com/steal-this-token`` がそのまま返って赤。
    """
    from teamagent.skills.pre_meeting_brief.signals import tighten_name

    assert [tighten_name(s) for s in HOSTLIKE_MUST_BE_DISCARDED] == [""] * len(
        HOSTLIKE_MUST_BE_DISCARDED
    )


def test_legitimate_company_names_survive_the_hostname_filter() -> None:
    """正規の社名まで巻き込んでいないこと（捨てすぎると機能が死ぬ）。"""
    from teamagent.skills.pre_meeting_brief.signals import tighten_name

    # ⚠️ ``tighten_name`` は NFKC しない（正規化は ``normalize_text`` の役目）。
    #    全角括弧はそのまま残るのが正（半角化は呼び出し側の normalize_text 経由）。
    assert [tighten_name(s) for s in LEGITIMATE_NAMES_MUST_SURVIVE] == [
        "北都リゾート",
        "青葉広告（山田様）",
        "株式会社A.B.C",
        "ドコモ.com",
        "Co.,Ltd.",
        "No.1",
        "中央・製紙",
        "白水飲料(飲料/健康)",
        "花王株式会社",
        "ユニ・チャーム",
    ]


def test_scheme_less_host_in_agency_line_never_reaches_the_hint() -> None:
    """説明欄 → ``agency_hint`` → ``agency_display`` の経路で丸ごと消える。

    旧実装の実測: ``sig.agency_hint == 'evil.example.com/steal-this-token'``、
    描画結果 ``  クライアント：北都リゾート／代理店：evil.example.com/steal-this-token``。
    """
    raw = {
        "id": "e-host",
        "summary": "打合せ",
        "start": {"dateTime": "2026-09-11T14:00:00+09:00"},
        "end": {"dateTime": "2026-09-11T15:00:00+09:00"},
        "description": "クライアント：北都リゾート\n代理店：evil.example.com/steal-this-token",
    }
    (detail,) = extract_events([raw], want_description=True)
    sig = build_signal_input(detail)
    assert sig.agency_hint == ""
    hint = extract_client(sig)
    assert hint.agency_display == ""
    assert hint.clients == ("北都リゾート",)


def test_scheme_less_host_in_client_line_never_reaches_the_hint() -> None:
    """代理店行だけでなく **クライアント行・インライン代理店** も同じ扱い。

    ⚠️ ``bit.ly/xYz9`` は「``/`` が社名の連記区切りでもある」ため、素朴に
    ``tighten_name`` だけ直すと ``bit.ly``（捨てる）と ``xYz9``（**社名として残る**）に
    割れて、第三者の文字列が ``クライアント：xYz9`` として本人 DM に出る（実測）。
    変異: ``split_clients`` の ``_URLISH_RE.sub`` を落とすと ``'xYz9'`` が残って赤。
    """
    raw = {
        "id": "e-host2",
        "summary": "打合せ",
        "start": {"dateTime": "2026-09-11T14:00:00+09:00"},
        "end": {"dateTime": "2026-09-11T15:00:00+09:00"},
        "description": "クライアント：bit.ly/xYz9／代理店：drive.google.com",
    }
    (detail,) = extract_events([raw], want_description=True)
    sig = build_signal_input(detail)
    assert sig.client_hint == ""
    assert sig.agency_hint == ""
    hint = extract_client(sig)
    assert hint.clients == ()
    assert hint.agency_display == ""


def test_url_strip_does_not_eat_the_company_next_to_it() -> None:
    """URL を消しても、区切りの反対側にある正当な社名は残る（捨てすぎない）。"""
    from teamagent.skills.pre_meeting_brief.signals import split_clients

    assert split_clients("北都リゾート／evil.example.com/steal") == ["北都リゾート"]
    assert split_clients("北都リゾート ※資料 https://drive.google.com/a 参照") == ["北都リゾート"]
    # 冪等（``_from_description`` が「／」で連結して持ち回る）。
    joined = "／".join(split_clients("緑川フーズ／白水飲料"))
    assert split_clients(joined) == split_clients("緑川フーズ／白水飲料")


def test_attendee_domain_path_still_works() -> None:
    """P4（参加者ドメイン）は ``domain_label`` を通るので生き残る。

    ``tighten_name`` へ寄せると ``aoba-ad.co.jp`` まで捨てられて P4 が永久に空になる。
    変異: ``classify.py`` の ``domain_label`` を ``tighten_name`` へ戻すと赤。
    """
    from teamagent.skills.pre_meeting_brief.signals import domain_label

    sig = _sig(attendee_domains=("aoba-ad.co.jp",), attendee_list_available=True)
    assert extract_client(sig).clients == ("aoba-ad.co.jp",)
    # 自由文が 1 文字でも混ざれば捨てる（ここを緩めると説明欄の穴に戻る）。
    assert domain_label("evil.example.com/steal") == ""
    assert domain_label("evil.example.com steal") == ""
    assert domain_label("aoba-ad.co.jp") == "aoba-ad.co.jp"


# ── 名寄せ ────────────────────────────────────────────────────────────
def test_normalize_company_strips_corp_forms_and_notes() -> None:
    assert normalize_company("株式会社東光製作所様（代理店：青葉広告）") == "東光製作所"
    assert normalize_company("㈱花王") == "花王"


def test_kao_two_chars_is_not_usable_as_partial_but_exact_is_allowed() -> None:
    """「花王」は 2 文字。部分一致だけに最小長を掛ける（完全一致は長さを問わない）。"""
    assert is_usable_partial("花王") is False
    assert is_usable_partial("東光製作所") is True


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
