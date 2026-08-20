"""Slack 返信漏れ「判定層」（_shared/slack_handoff.py）の単体テスト。

外部 I/O 無し・時刻は全て注入（``_NOW`` 固定）。攻める境界:
3 バケットの代表ケース / 期限の曜日照合が合わない / 依頼文が無い / 並び順 /
「要約を作らない」（引用は原文の部分文字列）/ 出力語彙に「対応不要」を出さない。

そして **承認済みモックの 5 件を fixture 化**し、バケット・見出し・期限・所要時間・
並びがモックどおりに落ちることを固定する（この層の受け入れ条件そのもの）。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import fields
from types import SimpleNamespace
from typing import Any

import pytest

from teamagent.skills._shared.slack_handoff import (
    BUCKET_FYI,
    BUCKET_WATCH,
    BUCKET_YOURS,
    EFFORT_BY_KIND,
    KIND_REPLY,
    KIND_SCHEDULE,
    KIND_TAKEOVER,
    KIND_UNKNOWN,
    NOTE_BODY_TRUNCATED,
    NOTE_DUE_UNRESOLVED,
    NOTE_NO_REQUEST,
    REASON_ANSWERED_BY_OTHER,
    REASON_BLOCKED,
    REASON_CLOSED,
    HandoffCard,
    build_card,
    channel_label,
    classify_request_kind,
    count_mentioned_others,
    effort_for_kind,
    elapsed_days_from,
    elapsed_label,
    extract_request_quote,
    extract_topic,
    extract_topic_and_context,
    resolve_due,
    sentences,
    source_from_item,
    triage_slack_handoff,
)

_JST = dt.timezone(dt.timedelta(hours=9))
# 2026-08-20 は木曜日。fixture の 8/17=月 / 8/21=金 / 8/28=金 はすべて実カレンダー。
_NOW = dt.datetime(2026, 8, 20, 9, 30, tzinfo=_JST)
_ME = "U_ME"


def _item(**kw: Any) -> SimpleNamespace:
    """``SlackUnreadItem`` と同じ属性名を持つ最小のスタブ（schema 依存を持ち込まない）。"""
    base: dict[str, Any] = {
        "excerpt_display": "",
        "occurred_at": None,
        "channel_kind": "unknown",
        "permalink": "",
        "from_user_id": None,
        "from_display_name": None,
        "mentioned_user_ids": [],
        "answered_by_other": False,
        "sender_followed_up": False,
        "thread_message_count": 0,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _card(text: str, **kw: Any) -> HandoffCard:
    return build_card(
        source_from_item(_item(excerpt_display=text, **kw)), now=_NOW, me_user_id=_ME, index=0
    )


# ── 承認済みモックの 5 件（この層の受け入れ条件）─────────────────────────────

#: 本文を :data:`BODY_EXCERPT_CAP`(1500) 超へ伸ばす詰め物。
#: 依頼表現も日付も digit も含まないので、依頼文選択・期限解決に影響しない。
_FILLER = "経緯は前回の議事録に記載しています。" * 90


def _mock_items() -> list[SimpleNamespace]:
    return [
        _item(  # ① 作業引き取り（DM・2日経過・15分）
            excerpt_display=(
                f"<@{_ME}> おはようございます。引継ぎタスク3件を引き取ってもらえますか？"
                "リストは共有済みです。"
            ),
            occurred_at="2026-08-18T10:12:00+09:00",
            channel_kind="dm",
            from_user_id="U_MORITA",
            from_display_name="森田",
            mentioned_user_ids=[_ME],
            permalink="https://x.slack.com/archives/D1/p1",
        ),
        _item(  # ② 日程回答（DM・3日経過・1分）
            excerpt_display=f"<@{_ME}> NTVカードの受け渡しの件、来社日を教えてください。",
            occurred_at="2026-08-17T15:02:00+09:00",
            channel_kind="dm",
            mentioned_user_ids=[_ME],
            permalink="https://x.slack.com/archives/D2/p2",
        ),
        _item(  # ③ 返信のみ＋期限（グループDM・2分）＋本文が途中で切れている
            excerpt_display=f"<@{_ME}> 28(金)の条件変更をご確認お願いします。" + _FILLER,
            occurred_at="2026-08-18T18:40:00+09:00",
            channel_kind="group_dm",
            mentioned_user_ids=[_ME],
            permalink="https://x.slack.com/archives/G3/p3",
        ),
        _item(  # ④ 様子見（相談日が過ぎている・他1名も名指し）
            excerpt_display=f"<@{_ME}> <@U_TANAKA> AI相談の件、8/17(月)にお願いできますか？",
            occurred_at="2026-08-18T09:00:00+09:00",
            channel_kind="group_dm",
            mentioned_user_ids=[_ME, "U_TANAKA"],
            permalink="https://x.slack.com/archives/G4/p4",
        ),
        _item(  # ⑤ 見るだけ（前提が他人側にある＝いま返信不要）
            excerpt_display=(
                f"<@{_ME}> 情シスの承認後に、あなたから再依頼をお願いします。今は待ちで大丈夫です。"
            ),
            occurred_at="2026-08-19T11:00:00+09:00",
            channel_kind="dm",
            mentioned_user_ids=[_ME],
            permalink="https://x.slack.com/archives/D5/p5",
        ),
    ]


def test_mock_five_items_buckets_and_order() -> None:
    d = triage_slack_handoff(_mock_items(), now=_NOW, me_user_id=_ME)
    assert [c.bucket for c in d.cards] == [
        BUCKET_YOURS,
        BUCKET_YOURS,
        BUCKET_YOURS,
        BUCKET_WATCH,
        BUCKET_FYI,
    ]
    assert [c.index for c in d.cards] == [1, 2, 3, 4, 5]
    assert d.summary_label() == "あなたの番 3・様子見 1・見るだけ 1"
    assert (d.count(BUCKET_YOURS), d.count(BUCKET_WATCH), d.count(BUCKET_FYI)) == (3, 1, 1)
    # 並びの根拠: ①は作業発生（3日経過の②より上）／②③は経過時間の降順。
    assert [c.source_index for c in d.cards] == [0, 1, 2, 3, 4]


def test_mock_five_items_headlines() -> None:
    d = triage_slack_handoff(_mock_items(), now=_NOW, me_user_id=_ME)
    assert [c.headline for c in d.cards] == [
        "引継ぎタスク3件を引き取る",
        "来社日を返す",
        "28(金)の条件変更を確認",
        "AI相談は当日が過ぎている",
        "情シスの承認後にあなたから再依頼",
    ]
    # ②だけ「手前の話題」を逐語で持つ（モックの「（NTVカード受渡）」に相当）。
    assert [c.context for c in d.cards] == ["", "NTVカードの受け渡し", "", "", ""]


def test_mock_five_items_kind_effort_elapsed_due() -> None:
    d = triage_slack_handoff(_mock_items(), now=_NOW, me_user_id=_ME)
    assert [c.request_kind for c in d.cards] == [
        KIND_TAKEOVER,
        KIND_SCHEDULE,
        KIND_REPLY,
        KIND_REPLY,
        KIND_REPLY,
    ]
    # 所要時間は「あなたの番」だけ（畳んだ件に自分の作業は無い）。
    assert [c.effort_label for c in d.cards] == ["15分", "1分", "2分", "", ""]
    assert [c.elapsed_label for c in d.cards] == [
        "2日経過",
        "3日経過",
        "2日経過",
        "2日経過",
        "1日経過",
    ]
    assert [c.channel_label for c in d.cards] == [
        "DM",
        "DM",
        "グループDM",
        "グループDM",
        "DM",
    ]
    # ③「28(金)の条件変更」の 28(金) は **期限として書かれていない**（「の」で名詞に係る）。
    # 期限を騙ると本物の滞留時間（2日経過）を chip から押し出すので、期限とは名乗らない。
    assert d.cards[2].due_label == ""
    assert d.cards[2].due_date == dt.date(2026, 8, 28)
    assert d.cards[2].elapsed_label == "2日経過"  # 経過日数が消えない
    # 日付は見出し（"28(金)の条件変更を確認"）に出ているので chip では繰り返さない。
    assert d.cards[2].date_mention_label == ""
    # ④は畳んだ理由が同じ日付を名乗るので chip 側は重複させない。
    assert d.cards[3].due_date == dt.date(2026, 8, 17)
    assert d.cards[3].due_label == ""
    assert d.cards[3].fold_reason == "相談日 8/17(月) を過ぎています"
    assert d.cards[3].mentioned_others == 1
    assert d.cards[4].fold_reason == REASON_BLOCKED


def test_mock_five_items_notes_only_where_worth_opening() -> None:
    """補足行は「原文を見る価値が本当にある件」だけ＝③（本文が途中で切れている）のみ。"""
    d = triage_slack_handoff(_mock_items(), now=_NOW, me_user_id=_ME)
    assert [c.note for c in d.cards] == ["", "", NOTE_BODY_TRUNCATED, "", ""]


def test_mock_quotes_are_verbatim_substrings_of_the_original() -> None:
    """引用は原文の逐語コピー（＝要約文を作っていないことの機械的な証明）。"""
    items = _mock_items()
    d = triage_slack_handoff(items, now=_NOW, me_user_id=_ME)
    by_index = {c.source_index: c for c in d.cards}
    for i, it in enumerate(items):
        quote = by_index[i].request_quote
        assert quote, f"item {i} の依頼文が空"
        assert quote in it.excerpt_display


# ── バケット判定（3 つの代表ケース）──────────────────────────────────────────


def test_bucket_yours_plain_dm_request() -> None:
    c = _card(f"<@{_ME}> 見積の条件をご確認ください。", channel_kind="dm", mentioned_user_ids=[_ME])
    assert c.bucket == BUCKET_YOURS
    assert c.fold_reason == ""
    assert c.headline == "見積の条件を確認"
    assert c.effort_label == "2分"


def test_bucket_watch_when_someone_else_answered() -> None:
    c = _card(
        f"<@{_ME}> 見積の条件をご確認ください。",
        channel_kind="channel",
        mentioned_user_ids=[_ME],
        answered_by_other=True,
    )
    assert c.bucket == BUCKET_WATCH
    assert c.fold_reason == REASON_ANSWERED_BY_OTHER
    assert c.headline == "見積の条件は他の人が答えている"
    assert c.effort_label == ""


def test_bucket_watch_when_consult_date_has_passed() -> None:
    c = _card(
        f"<@{_ME}> AI相談の件、8/17(月)にお願いできますか？",
        channel_kind="group_dm",
        mentioned_user_ids=[_ME, "U_TANAKA"],
    )
    assert c.bucket == BUCKET_WATCH
    assert c.fold_reason == "相談日 8/17(月) を過ぎています"


def test_bucket_fyi_on_closing_declaration() -> None:
    c = _card(f"<@{_ME}> 例の件ですが、一旦クローズします。", channel_kind="dm")
    assert c.bucket == BUCKET_FYI
    assert c.fold_reason == REASON_CLOSED
    assert c.headline == "終了と書かれている"


def test_bucket_fyi_when_blocked_on_someone_else() -> None:
    c = _card(f"<@{_ME}> 情シスの承認後に、あなたから再依頼をお願いします。", channel_kind="dm")
    assert c.bucket == BUCKET_FYI
    assert c.fold_reason == REASON_BLOCKED
    assert c.headline == "情シスの承認後にあなたから再依頼"


def test_condition_on_your_own_side_stays_yours() -> None:
    """「確認したら教えて」は前提が **自分側**＝畳まない（他人待ちと取り違えない）。"""
    c = _card(f"<@{_ME}> 内容を確認したら教えてください。", channel_kind="dm")
    assert c.bucket == BUCKET_YOURS
    assert c.fold_reason == ""


def test_sender_follow_up_beats_folding() -> None:
    """差出人が催促してきた件は、期限切れでも他人が答えていても「あなたの番」に残す。"""
    c = _card(
        f"<@{_ME}> AI相談の件、8/17(月)にお願いできますか？",
        channel_kind="group_dm",
        mentioned_user_ids=[_ME, "U_TANAKA"],
        answered_by_other=True,
        sender_followed_up=True,
    )
    assert c.bucket == BUCKET_YOURS
    assert c.fold_reason == ""


# ── 期限の解決（曜日照合）────────────────────────────────────────────────────


def test_due_absolute_with_matching_weekday() -> None:
    """日付は解決するが、**期限として書かれていない**ので「期限」とは名乗らない。"""
    due = resolve_due("28(金)の条件変更をご確認お願いします。", _NOW)
    assert (due.due_date, due.label, due.unresolved) == (
        dt.date(2026, 8, 28),
        "8/28(金) の記載あり",
        False,
    )
    assert due.source_text == "28(金)"
    assert due.is_deadline is False


def test_due_is_a_deadline_only_when_written_as_one() -> None:
    """「までに」「期限」を伴う日付だけが期限。「の」で名詞に係る日付は期限ではない。"""
    assert resolve_due("8/28(金)までにご確認ください。", _NOW).is_deadline is True
    assert resolve_due("8/28(金)にご確認ください。", _NOW).is_deadline is True
    assert resolve_due("期限は8/28(金)です。", _NOW).is_deadline is True
    assert resolve_due("8/28(金)の請求書をご確認ください。", _NOW).is_deadline is False
    assert resolve_due("8/28(金)分の稼働表を提出してください。", _NOW).is_deadline is False
    # 「その日に起きた事」の説明（連体修飾）は、助詞が「に」でも期限ではない。
    assert resolve_due("8/28(金)に届いた請求書をご確認ください。", _NOW).is_deadline is False
    assert resolve_due("8/28(金)に関する資料をご確認ください。", _NOW).is_deadline is False
    assert resolve_due("8/28(金)についてご確認ください。", _NOW).is_deadline is False


@pytest.mark.parametrize(
    "body",
    [
        "8/17(月)の請求書をご確認ください。",
        "先週の8/14(金)分の稼働表を提出してください。",
        "8/19(水)のオリエン議事録に追記をお願いします。",
        "8/17(月)に届いた請求書をご確認ください。",
    ],
)
def test_past_date_that_is_not_a_deadline_never_folds(body: str) -> None:
    """過去日でも **期限として書かれていなければ** 畳まない（🔴 に残す）。

    請求書の発行日・稼働表の対象週・議事録の開催日を「相談日」と名乗って様子見へ落とすと、
    まだ自分の番の宿題が 🔴 から消える＝この機能が潰そうとしている見逃しそのものになる。
    """
    c = _card(f"<@{_ME}> {body}", channel_kind="dm", mentioned_user_ids=[_ME])
    assert c.bucket == BUCKET_YOURS
    assert c.fold_reason == ""
    assert "相談日" not in c.headline and "当日が過ぎている" not in c.headline


def test_past_date_that_is_not_a_deadline_is_still_shown_as_a_chip() -> None:
    """畳まない代わりに「日付の記載がある」事実は残す（経過日数は消さない）。"""
    c = _card(
        f"<@{_ME}> 条件をご確認ください。なお8/17(月)の議事録も共有します。",
        channel_kind="dm",
        occurred_at="2026-08-18T09:00:00+09:00",
        mentioned_user_ids=[_ME],
    )
    assert c.bucket == BUCKET_YOURS
    assert c.due_label == ""  # 期限とは名乗らない
    assert c.date_mention_label == "8/17(月) の記載あり"
    assert c.elapsed_label == "2日経過"  # 本物の時間軸を押し出さない


def test_due_is_blanked_when_weekday_does_not_match() -> None:
    """「8/28(木)」は実際には金曜。どちらが正しいか決められない＝空欄にして印を付ける。"""
    due = resolve_due("8/28(木)までにお願いします。", _NOW)
    assert due.due_date is None
    assert due.label == ""
    assert due.unresolved is True
    assert due.source_text == "8/28(木)"


def test_day_only_with_mismatching_weekday_is_unresolved() -> None:
    due = resolve_due("21(木)までに回答をください。", _NOW)  # 8/21 は金曜
    assert (due.due_date, due.label, due.unresolved) == (None, "", True)


def test_card_notes_the_unresolved_due_and_keeps_it_out_of_the_chips() -> None:
    c = _card(f"<@{_ME}> 8/28(木)までに条件をご確認ください。", channel_kind="dm")
    assert c.due_label == ""
    assert c.due_unresolved is True
    assert c.note == NOTE_DUE_UNRESOLVED
    assert c.bucket == BUCKET_YOURS  # 解決できない期限で勝手に畳まない


@pytest.mark.parametrize(
    ("body", "expected_date", "expected_label"),
    [
        ("明日までにご確認ください。", dt.date(2026, 8, 21), "期限 明日(8/21(金))"),
        ("今週中にご確認ください。", dt.date(2026, 8, 21), "期限 今週中(8/21(金))"),
        ("週明けにご確認ください。", dt.date(2026, 8, 24), "期限 週明け(8/24(月))"),
        ("本日中にご確認ください。", dt.date(2026, 8, 20), "期限 本日(8/20(木))"),
    ],
)
def test_due_relative_words(body: str, expected_date: dt.date, expected_label: str) -> None:
    due = resolve_due(body, _NOW)
    assert (due.due_date, due.label, due.unresolved) == (expected_date, expected_label, False)


def test_due_week_ahead_is_a_date_word_but_not_resolvable() -> None:
    """「来週」は 1 日に絞れない＝日付語ありのまま空欄（勝手に月曜へ寄せない）。"""
    due = resolve_due("来週ご確認ください。", _NOW)
    assert (due.due_date, due.label, due.unresolved) == (None, "", True)
    assert due.has_date_word is True


def test_due_absent() -> None:
    due = resolve_due("条件をご確認ください。", _NOW)
    assert (due.due_date, due.label, due.source_text, due.unresolved) == (None, "", "", False)
    assert due.has_date_word is False


def test_bare_number_is_not_a_date() -> None:
    """「引継ぎタスク3件」の 3 を日付にしない（曜日つきでなければ日付と断定しない）。"""
    assert resolve_due("引継ぎタスク3件を引き取ってもらえますか？", _NOW).source_text == ""


# ── 依頼文の抽出 ─────────────────────────────────────────────────────────────


def test_sentences_do_not_split_inside_slack_tokens() -> None:
    """🔴 トークン内の `!` `。` で文を割らない（生 ID の断片が見出しへ流れる）。

    `<!subteam^S08…>` の `!` は文末記号と同じ文字。素朴に割ると
    `subteam^S08DESIGN1> の件…` が「文」になり、そのまま依頼文＝見出しの材料になる。
    断片は描画側の畳み込み（`<!subteam^…>` → 種別語）にも掛からないので、
    生のユーザーグループ ID が本人の DM に出る（自前の敵対的入力テストで実測）。
    """
    body = f"<@{_ME}> <!subteam^S08DESIGN1> の件をご確認ください。"
    assert sentences(body) == [body]
    assert "S08DESIGN1" not in extract_topic(extract_request_quote(body))
    # リンクラベルの `。` でも割らない（ラベルが千切れて別の文になるのを防ぐ）。
    linked = "<https://x.com/a|資料はこちら。詳細版> をご確認ください。"
    assert sentences(linked) == [linked]
    # 通常の文分割・改行分割は従来どおり。
    assert sentences("お疲れ様です。\n確認お願いします。\n最後の行") == [
        "お疲れ様です。",
        "確認お願いします。",
        "最後の行",
    ]


def test_addressing_tokens_never_become_the_topic() -> None:
    """宛先トークン（メンション・ユーザーグループ・ラベル無しチャンネル）は話題ではない。"""
    for token in ("<!subteam^S08DESIGN1>", "<!here>", "<#C08SECRET77>"):
        c = _card(f"<@{_ME}> {token} 見積の条件をご確認ください。", channel_kind="dm")
        assert "S08DESIGN1" not in c.headline
        assert "C08SECRET77" not in c.headline
        assert "subteam" not in c.headline
    # ラベル付きチャンネル参照は実在の名前＝話題になり得るので残す（描画が #general に畳む）。
    assert "general" in extract_topic("<#C08GENERAL9|general> の運用をご確認ください。")


def test_request_quote_is_selected_not_generated() -> None:
    body = f"<@{_ME}> おはようございます。引継ぎタスク3件を引き取ってもらえますか？"
    quote = extract_request_quote(body)
    assert quote == "引継ぎタスク3件を引き取ってもらえますか？"
    assert quote in body


def test_request_quote_skips_pleasantries() -> None:
    assert extract_request_quote("お世話になっております。よろしくお願いします。") == ""


def test_no_request_sentence_leaves_everything_empty() -> None:
    c = _card(f"<@{_ME}> 明日の資料、こちらです。", channel_kind="dm", mentioned_user_ids=[_ME])
    assert c.request_quote == ""
    assert c.request_kind == KIND_UNKNOWN
    assert c.effort_label == ""  # 推測で数字を作らない
    assert c.headline == "原文を見る"
    assert c.note == NOTE_NO_REQUEST
    assert c.bucket == BUCKET_YOURS


def test_no_request_sentence_with_multiple_names_is_watched() -> None:
    c = _card(
        f"<@{_ME}> <@U_TANAKA> 明日の資料、こちらです。",
        channel_kind="group_dm",
        mentioned_user_ids=[_ME, "U_TANAKA"],
    )
    assert c.bucket == BUCKET_WATCH
    assert c.fold_reason == "他1名も名指しで、あなた宛の依頼文は見つかりませんでした"
    assert c.headline == "宛先が絞れていない"


# ── 見出しの組み立て（固定語彙 × 原文の切り出し）───────────────────────────


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("<@U_ME> 引継ぎタスク3件を引き取ってもらえますか？", "引継ぎタスク3件を引き取る"),
        ("<@U_ME> 来社日を教えてください。", "来社日を返す"),
        ("<@U_ME> 条件変更をご確認お願いします。", "条件変更を確認"),
        # サ変名詞で終わる話題は「最終を確認」にせず「最終確認する」にする。
        ("<@U_ME> 最終確認をお願いします。", "最終確認する"),
    ],
)
def test_headline_is_topic_plus_fixed_suffix(body: str, expected: str) -> None:
    assert _card(body, channel_kind="dm").headline == expected


def test_topic_is_a_verbatim_slice_of_the_body() -> None:
    body = "NTVカードの受け渡しの件、来社日を教えてください。"
    topic, context = extract_topic_and_context(body)
    assert topic == "来社日"
    assert context == "NTVカードの受け渡し"
    assert topic in body
    assert context in body


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # 「送る」を「確認」に化けさせない。型が判らない依頼は **依頼文をそのまま** 出す。
        ("<@U_ME> 請求書だけ送ってください。", "請求書だけ送ってください"),
        ("<@U_ME> B社案で見積を出してもらえますか？", "B社案で見積を出してもらえますか"),
        # 「触るな・待て」を「あなたの番」の語尾で上書きしない。
        (
            "<@U_ME> この案件は今は動かさないでください。",
            "この案件は今は動かさないでください",
        ),
    ],
)
def test_unknown_kind_headline_is_the_request_verbatim(body: str, expected: str) -> None:
    """型が判らない依頼に固定語尾を足すと、原文に無い述語を作ってしまう（生成＝禁止）。"""
    c = _card(body, channel_kind="dm")
    assert c.request_kind == KIND_UNKNOWN
    assert c.headline == expected
    assert c.headline in body  # 逐語（宛先トークンと句点だけ落とす）


def test_headline_never_ends_with_a_broken_verb_stem() -> None:
    """て形の途中で切れた語幹（「見積を出」）を見出しに出さない。"""
    assert extract_topic("B社案で見積を出してもらえますか？") == "B社案で見積"


def test_conjunctive_topic_falls_back_instead_of_gluing_a_suffix() -> None:
    """「確認したら」+「を確認」のような壊れた見出しを作らない（定型文言へ落とす）。"""
    c = _card(f"<@{_ME}> 確認したら教えてください。", channel_kind="dm")
    assert c.headline == "返信する"


def test_long_topic_falls_back_to_fixed_wording_instead_of_truncating() -> None:
    body = (
        "<@U_ME> " + "とても長い前置きがここに延々と続く案件名" * 3 + "を引き取ってもらえますか？"
    )
    c = _card(body, channel_kind="dm")
    assert c.headline == "作業の引き取りを返す"  # 途中で切って捏造しない


# ── 並び順 ───────────────────────────────────────────────────────────────────


def test_order_work_first_then_elapsed_then_names_then_due_word() -> None:
    items = [
        _item(  # 0: 返信のみ・1日経過・期限語あり
            excerpt_display=f"<@{_ME}> 明日までに条件をご確認ください。",
            occurred_at="2026-08-19T09:00:00+09:00",
            channel_kind="dm",
            mentioned_user_ids=[_ME],
        ),
        _item(  # 1: 返信のみ・4日経過
            excerpt_display=f"<@{_ME}> 条件をご確認ください。",
            occurred_at="2026-08-16T09:00:00+09:00",
            channel_kind="dm",
            mentioned_user_ids=[_ME],
        ),
        _item(  # 2: 作業引き取り・0日経過（＝作業発生が最優先）
            excerpt_display=f"<@{_ME}> 運用タスクを引き取ってもらえますか？",
            occurred_at="2026-08-20T08:00:00+09:00",
            channel_kind="dm",
            mentioned_user_ids=[_ME],
        ),
        _item(  # 3: 返信のみ・1日経過・他2名も名指し（0 と同経過→名指し多い方が上）
            excerpt_display=f"<@{_ME}> <@U_A> <@U_B> 条件をご確認ください。",
            occurred_at="2026-08-19T09:00:00+09:00",
            channel_kind="group_dm",
            mentioned_user_ids=[_ME, "U_A", "U_B"],
            sender_followed_up=True,  # 名指し複数でも催促があるので yours に残る
        ),
    ]
    d = triage_slack_handoff(items, now=_NOW, me_user_id=_ME)
    assert [c.source_index for c in d.cards] == [2, 1, 3, 0]


def test_buckets_are_ordered_yours_then_watch_then_fyi() -> None:
    items = [
        _item(excerpt_display=f"<@{_ME}> 一旦クローズします。", channel_kind="dm"),
        _item(
            excerpt_display=f"<@{_ME}> 条件をご確認ください。",
            channel_kind="dm",
            answered_by_other=True,
        ),
        _item(excerpt_display=f"<@{_ME}> 条件をご確認ください。", channel_kind="dm"),
    ]
    d = triage_slack_handoff(items, now=_NOW, me_user_id=_ME)
    assert [c.bucket for c in d.cards] == [BUCKET_YOURS, BUCKET_WATCH, BUCKET_FYI]
    assert [c.source_index for c in d.cards] == [2, 1, 0]


# ── 語彙・小さな純関数 ───────────────────────────────────────────────────────


def test_forbidden_vocabulary_never_reaches_the_output() -> None:
    """「対応不要」は出力語彙に入れない（自分に戻ってくる件を消してしまうため）。

    差出人が本文にその語を書いていても、こちらの出力語彙には決して現れないこと。
    """
    items = [
        *_mock_items(),
        _item(excerpt_display=f"<@{_ME}> こちらは対応不要です。", channel_kind="dm"),
        _item(
            excerpt_display=f"<@{_ME}> 条件をご確認ください。",
            channel_kind="dm",
            answered_by_other=True,
        ),
    ]
    d = triage_slack_handoff(items, now=_NOW, me_user_id=_ME)
    assert d.cards[-1].bucket == BUCKET_FYI  # 検知自体はできている
    emitted: list[str] = []
    for c in d.cards:
        for f in fields(c):
            if f.name == "request_quote":
                continue  # 引用は原文の逐語コピー（相手の言葉であって当方の語彙ではない）
            v = getattr(c, f.name)
            if isinstance(v, str):
                emitted.append(v)
    emitted.extend(EFFORT_BY_KIND.values())
    assert not [s for s in emitted if "対応不要" in s]


@pytest.mark.parametrize(
    ("kind", "expected"),
    [(KIND_TAKEOVER, "15分"), (KIND_SCHEDULE, "1分"), (KIND_REPLY, "2分"), (KIND_UNKNOWN, "")],
)
def test_effort_comes_from_a_fixed_table(kind: str, expected: str) -> None:
    assert effort_for_kind(kind) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("引き取ってもらえますか", KIND_TAKEOVER),
        ("来社日を教えてください", KIND_SCHEDULE),
        ("条件をご確認ください", KIND_REPLY),
        ("資料はこちらです", KIND_UNKNOWN),
    ],
)
def test_kind_dictionary(body: str, expected: str) -> None:
    assert classify_request_kind(body) == expected


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("dm", "DM"),
        ("group_dm", "グループDM"),
        ("channel", "チャンネル"),
        ("unknown", ""),
        ("", ""),
    ],
)
def test_channel_label_leaves_unknown_blank(kind: str, expected: str) -> None:
    assert channel_label(kind) == expected


def test_elapsed_days_and_label() -> None:
    assert elapsed_days_from("2026-08-18T10:00:00+09:00", _NOW) == 2
    assert elapsed_label(2) == "2日経過"
    assert elapsed_label(0) == "今日"
    assert elapsed_days_from(None, _NOW) is None
    assert elapsed_days_from("not-a-date", _NOW) is None
    assert elapsed_label(None) == ""


def test_naive_occurred_at_is_treated_as_jst() -> None:
    """offset 無しは JST とみなす（UTC 解釈だと 9 時間ずれて経過日数が狂う）。"""
    assert elapsed_days_from("2026-08-20T02:00:00", _NOW) == 0


def test_count_mentioned_others() -> None:
    assert count_mentioned_others([_ME, "U_A"], _ME) == 1
    assert count_mentioned_others([_ME], _ME) == 0
    assert count_mentioned_others([], _ME) == 0
    # 自分の id が分からないときは「1 人は自分」とみなして差し引く。
    assert count_mentioned_others(["U_X", "U_Y"], None) == 1


def test_source_from_item_accepts_provider_field_names() -> None:
    """provider の ``UnrepliedMention``（text / user / user_display）からも読める。"""
    from teamagent.skills._shared.slack_unreplied import UnrepliedMention

    m = UnrepliedMention(
        channel_id="D1",
        channel_name="dm",
        ts="1000.1",
        text=f"<@{_ME}> 条件をご確認ください。",
        permalink="https://x.slack.com/archives/D1/p1",
        occurred_at="2026-08-18T10:00:00+09:00",
        user="U_A",
        user_display="山田",
        channel_kind="dm",
        mentioned_user_ids=(_ME,),
    )
    src = source_from_item(m)
    assert src.text == f"<@{_ME}> 条件をご確認ください。"
    assert (src.from_user_id, src.from_display_name, src.channel_kind) == ("U_A", "山田", "dm")
    c = build_card(src, now=_NOW, me_user_id=_ME, index=0)
    assert (c.bucket, c.headline, c.effort_label) == (BUCKET_YOURS, "条件を確認", "2分")


def test_missing_fields_stay_blank_instead_of_being_guessed() -> None:
    c = build_card(source_from_item(_item()), now=_NOW, me_user_id=_ME, index=0)
    assert (c.channel_label, c.elapsed_label, c.due_label, c.request_quote) == ("", "", "", "")
    assert c.headline == "原文を見る"


def test_empty_input_has_no_summary() -> None:
    d = triage_slack_handoff([], now=_NOW, me_user_id=_ME)
    assert (d.total, d.cards, d.summary_label()) == (0, (), "")


def test_body_truncation_flag_can_be_given_explicitly() -> None:
    c = _card(f"<@{_ME}> 条件をご確認ください。", channel_kind="dm", body_truncated=True)
    assert c.note == NOTE_BODY_TRUNCATED


def test_extract_topic_wrapper_matches_pair_version() -> None:
    body = "NTVカードの受け渡しの件、来社日を教えてください。"
    assert extract_topic(body) == extract_topic_and_context(body)[0]


# ── 畳む側の誤りを防ぐ線（見逃しに直結するので狭く取る）────────────────────


def test_closing_declaration_only_counts_in_the_last_sentence() -> None:
    """前置きで昔の件を締めてから新しい依頼が来る本文で、宿題を消さない。"""
    c = _card(
        f"<@{_ME}> 前の件はクローズしました。別件で、条件をご確認ください。",
        channel_kind="dm",
    )
    assert c.bucket == BUCKET_YOURS
    assert c.fold_reason == ""
    assert c.headline == "条件を確認"


def test_past_date_outside_the_request_does_not_fold_the_item() -> None:
    """依頼文の外にある過去日（議事録の日付等）を「相談日が過ぎている」と読まない。"""
    c = _card(
        f"<@{_ME}> 8/17の議事録を共有します。あわせて条件をご確認ください。",
        channel_kind="dm",
        mentioned_user_ids=[_ME],
    )
    assert c.bucket == BUCKET_YOURS
    assert c.fold_reason == ""


def test_past_date_inside_the_request_still_folds() -> None:
    """同じ日付でも、依頼文の中にあるなら「当日が過ぎている」と読む（対の証明）。"""
    c = _card(f"<@{_ME}> 8/17(月)までに条件をご確認ください。", channel_kind="dm")
    assert c.bucket == BUCKET_WATCH
    assert c.fold_reason == "相談日 8/17(月) を過ぎています"


def test_deadline_written_in_a_later_sentence_is_still_shown() -> None:
    """依頼文に日付が無いときは本文全体へ広げて期限を拾う（実在の期限を落とさない）。"""
    c = _card(f"<@{_ME}> 条件をご確認ください。期限は8/28(金)です。", channel_kind="dm")
    assert c.due_label == "期限 8/28(金)"
    assert c.bucket == BUCKET_YOURS


def test_deadline_prefers_the_date_inside_the_request_over_an_earlier_one() -> None:
    """本文に日付が 2 つあるとき、期限は **依頼文の中の日付**（後ろでも）を採る。

    本文全体の先頭一致で取ると、議事録の日付 8/17 を期限として掲げてしまう。
    """
    c = _card(
        f"<@{_ME}> 8/17の議事録を共有します。8/28(金)までに条件をご確認ください。",
        channel_kind="dm",
    )
    assert c.due_label == "期限 8/28(金)"
    assert c.due_date == dt.date(2026, 8, 28)
    assert c.bucket == BUCKET_YOURS
