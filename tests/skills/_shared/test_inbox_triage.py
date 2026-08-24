"""_shared/inbox_triage.py（受信箱トリアージの判定層）のユニットテスト。

I/O 無し・LLM 無し・課金 0。時刻は ``now`` 引数で固定する。

このテストが守っている裁定（2026-08-21）:
  - 一覧段階では本文を使わない（データクラスに本文の置き場が無いことを型で担保）
  - 件名は原文のまま（要約しない・長い時だけ末尾を落とす）
  - 曖昧な選択は推測せず None（＝聞き返す）
"""

from __future__ import annotations

import datetime as _dt

from teamagent.skills._shared.inbox_triage import (
    MSG_EMPTY,
    MSG_FOOTER,
    MSG_TRUNCATED,
    SUBJECT_DISPLAY_MAX,
    InboxMailMeta,
    TriageCandidate,
    parse_selection,
    rank_candidates,
    render_triage_message,
)

_JST = _dt.timezone(_dt.timedelta(hours=9))
NOW = _dt.datetime(2026, 8, 24, 10, 0, tzinfo=_JST)
_MS_PER_DAY = 24 * 60 * 60 * 1000


def _ms_days_ago(days: int, *, now: _dt.datetime = NOW) -> int:
    """``now`` から ``days`` 日前ちょうどの epoch ミリ秒。"""
    return int(now.timestamp() * 1000) - days * _MS_PER_DAY


def _mail(
    thread_id: str,
    *,
    subject: str = "打ち合わせの件",
    sender_name: str = "山田太郎",
    sender_email: str = "yamada@example.co.jp",
    days_ago: int = 3,
    sole: bool = False,
    unreplied: bool = True,
    bulk: bool = False,
) -> InboxMailMeta:
    return InboxMailMeta(
        thread_id=thread_id,
        subject=subject,
        sender_name=sender_name,
        sender_email=sender_email,
        received_at_ms=_ms_days_ago(days_ago),
        is_sole_recipient=sole,
        is_unreplied=unreplied,
        is_bulk=bulk,
    )


# ── 本文を渡す手段が無いこと（裁定 A の型レベル担保）─────────────────────────


def test_meta_has_no_body_field() -> None:
    """一覧段階のデータクラスに本文・snippet の置き場が無い。"""
    fields = set(InboxMailMeta.__dataclass_fields__)
    assert not (fields & {"body", "snippet", "body_text", "excerpt", "payload"})


# ── rank_candidates: 並び順 ─────────────────────────────────────────────────


def test_rank_orders_by_score_then_idle() -> None:
    """自分ひとり宛＋依頼語の件が、より古いだけの CC 件より上に来る。"""
    cands = rank_candidates(
        [
            _mail("t_old_cc", subject="議事録共有", days_ago=10, sole=False),
            _mail("t_sole", subject="ご確認をお願いします", days_ago=3, sole=True),
        ],
        now=NOW,
    )
    assert [c.mail.thread_id for c in cands] == ["t_sole", "t_old_cc"]
    # 10点 < 3+12+8=23点
    assert cands[0].score > cands[1].score


def test_rank_sole_recipient_beats_cc_at_same_age() -> None:
    """同じ日数・同じ件名なら、自分ひとり宛の方が先。"""
    cands = rank_candidates(
        [
            _mail("t_cc", subject="共有", days_ago=5, sole=False),
            _mail("t_sole", subject="共有", days_ago=5, sole=True),
        ],
        now=NOW,
    )
    assert [c.mail.thread_id for c in cands] == ["t_sole", "t_cc"]


def test_rank_ties_broken_by_thread_id_deterministically() -> None:
    """完全同点は thread_id 昇順で固定（呼ぶたび『1番』が入れ替わらない）。"""
    a = _mail("t_b", subject="共有", days_ago=5)
    b = _mail("t_a", subject="共有", days_ago=5)
    assert [c.mail.thread_id for c in rank_candidates([a, b], now=NOW)] == ["t_a", "t_b"]
    assert [c.mail.thread_id for c in rank_candidates([b, a], now=NOW)] == ["t_a", "t_b"]


def test_rank_idle_days_computed_from_now() -> None:
    cands = rank_candidates([_mail("t1", days_ago=7)], now=NOW)
    assert cands[0].idle_days == 7


def test_rank_future_timestamp_is_zero_not_negative() -> None:
    """受信日時が未来でも負の日数にしない（表示が壊れる）。"""
    future = InboxMailMeta(
        thread_id="t1",
        subject="共有",
        received_at_ms=_ms_days_ago(-5),
    )
    assert rank_candidates([future], now=NOW)[0].idle_days == 0


# ── rank_candidates: 除外 ───────────────────────────────────────────────────


def test_rank_excludes_replied_bulk_and_empty_thread() -> None:
    cands = rank_candidates(
        [
            _mail("t_replied", days_ago=20, unreplied=False),
            _mail("t_bulk", days_ago=20, bulk=True),
            _mail("", days_ago=20),
            _mail("t_ok", days_ago=1),
        ],
        now=NOW,
    )
    assert [c.mail.thread_id for c in cands] == ["t_ok"]


def test_rank_excludes_no_reply_sender_without_header_flag() -> None:
    """呼び出し側が bulk を立て損ねても、no-reply アドレスは候補にしない。"""
    mail = _mail(
        "t_news",
        subject="【至急】ご確認ください",
        sender_email="no-reply@news.example.com",
        days_ago=30,
    )
    assert rank_candidates([mail], now=NOW) == ()


# ── rank_candidates: 件数上限 ───────────────────────────────────────────────


def test_rank_limit_defaults_to_three() -> None:
    mails = [_mail(f"t{i}", days_ago=i + 1) for i in range(6)]
    assert len(rank_candidates(mails, now=NOW)) == 3


def test_rank_limit_is_configurable_and_zero_returns_empty() -> None:
    mails = [_mail(f"t{i}", days_ago=i + 1) for i in range(6)]
    assert len(rank_candidates(mails, now=NOW, limit=5)) == 5
    assert rank_candidates(mails, now=NOW, limit=0) == ()


def test_rank_empty_input_returns_empty() -> None:
    assert rank_candidates([], now=NOW) == ()


# ── rank_candidates: 依頼語の効き ───────────────────────────────────────────


def test_request_word_in_subject_raises_rank() -> None:
    """同じ日数・同じ宛先条件なら、依頼語のある件名が先に来る。"""
    cands = rank_candidates(
        [
            _mail("t_plain", subject="先日の資料", days_ago=4),
            _mail("t_request", subject="ご返信のお願い", days_ago=4),
        ],
        now=NOW,
    )
    assert [c.mail.thread_id for c in cands] == ["t_request", "t_plain"]
    assert "request_word" in cands[0].reasons
    assert cands[1].reasons == ()


def test_urgent_word_in_subject_raises_rank() -> None:
    cands = rank_candidates(
        [
            _mail("t_plain", subject="資料の件", days_ago=4),
            _mail("t_urgent", subject="至急", days_ago=4),
        ],
        now=NOW,
    )
    assert [c.mail.thread_id for c in cands] == ["t_urgent", "t_plain"]
    assert "urgent_word" in cands[0].reasons


def test_request_word_matches_fullwidth_question_mark() -> None:
    """全角？も依頼語として効く（NFKC 正規化）。"""
    cands = rank_candidates(
        [
            _mail("t_plain", subject="資料", days_ago=4),
            _mail("t_q", subject="いつ頃になりそう？", days_ago=4),
        ],
        now=NOW,
    )
    assert cands[0].mail.thread_id == "t_q"


def test_request_word_only_looks_at_subject_not_sender() -> None:
    """依頼語の照合対象は件名だけ（差出人名の偶然の一致で加点しない）。"""
    cands = rank_candidates([_mail("t1", subject="資料", sender_name="確認 太郎")], now=NOW)
    assert cands[0].reasons == ()


# ── render_triage_message ──────────────────────────────────────────────────


def test_render_lists_candidates_with_footer() -> None:
    cands = rank_candidates(
        [
            _mail("t1", subject="ご確認のお願い", sender_name="電通 佐藤", days_ago=5, sole=True),
            _mail("t2", subject="請求書の件", sender_name="花王 田中", days_ago=2),
        ],
        now=NOW,
    )
    text = render_triage_message(cands, scanned=30, truncated=False, window_days=14)
    assert text == (
        "受信箱を見たところ、返信が止まっているのはこの2件でした。（直近14日・30件を確認）\n"
        "1. 電通 佐藤「ご確認のお願い」 ・5日経過\n"
        "2. 花王 田中「請求書の件」 ・2日経過\n" + MSG_FOOTER
    )


def test_render_numbers_follow_candidate_order() -> None:
    cands = rank_candidates(
        [
            _mail("t_low", subject="共有", days_ago=1),
            _mail("t_high", subject="共有", days_ago=9),
        ],
        now=NOW,
    )
    text = render_triage_message(cands, scanned=2, truncated=False, window_days=14)
    lines = text.split("\n")
    assert lines[1].startswith("1. ") and "9日経過" in lines[1]
    assert lines[2].startswith("2. ") and "1日経過" in lines[2]


def test_render_empty_has_no_draft_prompt() -> None:
    text = render_triage_message([], scanned=12, truncated=False, window_days=7)
    assert text == MSG_EMPTY.format(window=7, scanned=12)
    assert MSG_FOOTER not in text


def test_render_truncated_note_appended_in_both_branches() -> None:
    cands = rank_candidates([_mail("t1", days_ago=3)], now=NOW)
    with_items = render_triage_message(cands, scanned=50, truncated=True, window_days=14)
    empty = render_triage_message([], scanned=50, truncated=True, window_days=14)
    assert with_items.endswith(MSG_TRUNCATED)
    assert empty.endswith(MSG_TRUNCATED)
    assert MSG_TRUNCATED not in render_triage_message(
        cands, scanned=50, truncated=False, window_days=14
    )


def test_render_keeps_subject_verbatim() -> None:
    """件名は要約も言い換えもせず、原文の文字列がそのまま出る。"""
    subject = "【重要】8/25(火) 定例MTGの議題確認とご返信のお願い"
    cands = rank_candidates([_mail("t1", subject=subject, days_ago=3)], now=NOW)
    text = render_triage_message(cands, scanned=10, truncated=False, window_days=14)
    assert f"「{subject}」" in text


def test_render_clips_long_subject_from_the_tail() -> None:
    """長い件名は末尾を落として「…」。頭は原文のまま残す。"""
    subject = "あ" * (SUBJECT_DISPLAY_MAX + 20)
    cands = rank_candidates([_mail("t1", subject=subject, days_ago=3)], now=NOW)
    text = render_triage_message(cands, scanned=10, truncated=False, window_days=14)
    assert f"「{'あ' * SUBJECT_DISPLAY_MAX}…」" in text
    assert "あ" * (SUBJECT_DISPLAY_MAX + 1) not in text


def test_render_escapes_mrkdwn_control_chars_in_subject() -> None:
    """件名に仕込まれたリンク偽装が mrkdwn として解釈されない。"""
    cands = rank_candidates(
        [_mail("t1", subject="<https://evil.example|クリック> & 確認", days_ago=3)],
        now=NOW,
    )
    text = render_triage_message(cands, scanned=10, truncated=False, window_days=14)
    assert "<https://evil.example|クリック>" not in text
    assert "&lt;https://evil.example|クリック&gt; &amp; 確認" in text


def test_render_falls_back_to_email_then_unknown_sender() -> None:
    cands = rank_candidates(
        [
            _mail("t1", sender_name="", sender_email="a@example.com", days_ago=9),
            InboxMailMeta(thread_id="t2", subject="件名", received_at_ms=_ms_days_ago(1)),
        ],
        now=NOW,
    )
    text = render_triage_message(cands, scanned=2, truncated=False, window_days=14)
    assert "1. a@example.com「打ち合わせの件」" in text
    assert "2. 差出人不明「件名」" in text


# ── parse_selection ────────────────────────────────────────────────────────


def _three() -> tuple[TriageCandidate, ...]:
    return rank_candidates(
        [
            _mail("t1", subject="ご確認のお願い", sender_name="電通 佐藤", days_ago=9, sole=True),
            _mail("t2", subject="請求書の件", sender_name="花王 田中", days_ago=5),
            _mail("t3", subject="納品スケジュール", sender_name="森ビル 鈴木", days_ago=2),
        ],
        now=NOW,
    )


def test_parse_selection_single_number() -> None:
    cands = _three()
    picked = parse_selection("2", cands)
    assert picked is not None
    assert [c.mail.thread_id for c in picked] == ["t2"]


def test_parse_selection_number_with_japanese_suffix() -> None:
    cands = _three()
    picked = parse_selection("2番でお願いします", cands)
    assert picked is not None
    assert [c.mail.thread_id for c in picked] == ["t2"]


def test_parse_selection_fullwidth_and_circled_numbers() -> None:
    cands = _three()
    for text in ("３", "③"):
        picked = parse_selection(text, cands)
        assert picked is not None
        assert [c.mail.thread_id for c in picked] == ["t3"]


def test_parse_selection_multiple_numbers_returns_candidate_order() -> None:
    cands = _three()
    picked = parse_selection("3と1", cands)
    assert picked is not None
    assert [c.mail.thread_id for c in picked] == ["t1", "t3"]


def test_parse_selection_dedupes_repeated_numbers() -> None:
    cands = _three()
    picked = parse_selection("1と1", cands)
    assert picked is not None
    assert [c.mail.thread_id for c in picked] == ["t1"]


def test_parse_selection_by_client_name() -> None:
    cands = _three()
    picked = parse_selection("電通の件でお願い", cands)
    assert picked is not None
    assert [c.mail.thread_id for c in picked] == ["t1"]


def test_parse_selection_by_sender_domain() -> None:
    cands = rank_candidates(
        [
            _mail("t1", sender_name="", sender_email="sato@dentsu.co.jp", days_ago=9),
            _mail("t2", sender_name="", sender_email="tanaka@kao.co.jp", days_ago=5),
        ],
        now=NOW,
    )
    picked = parse_selection("dentsu の方に返したい", cands)
    assert picked is not None
    assert [c.mail.thread_id for c in picked] == ["t1"]


def test_parse_selection_out_of_range_returns_none() -> None:
    cands = _three()
    assert parse_selection("5", cands) is None
    assert parse_selection("1と9", cands) is None
    assert parse_selection("0", cands) is None


def test_parse_selection_date_like_text_returns_none() -> None:
    """『8/21 の件』のような日付混入を番号と読み違えない。"""
    cands = _three()
    assert parse_selection("8/21 の件", cands) is None


def test_parse_selection_ambiguous_name_returns_none() -> None:
    """2 件に当たる名前は推測で決めず None。"""
    cands = rank_candidates(
        [
            _mail("t1", sender_name="電通 佐藤", sender_email="sato@a.example", days_ago=9),
            _mail("t2", sender_name="電通 田中", sender_email="tanaka@b.example", days_ago=5),
        ],
        now=NOW,
    )
    assert parse_selection("電通の件", cands) is None


def test_parse_selection_unknown_text_returns_none() -> None:
    cands = _three()
    assert parse_selection("よろしく", cands) is None
    assert parse_selection("", cands) is None
    assert parse_selection("   ", cands) is None


def test_parse_selection_empty_candidates_returns_none() -> None:
    assert parse_selection("1", []) is None
