"""Slack 返信漏れの「判定層」（決定論のみ・LLM を一切使わない）。

データ層（``_shared/slack_unreplied.py`` → ``morning_digest/schema.SlackUnreadItem``）が
集めた事実を、DM に並べられる「カード」へ翻訳する層。描画（``scripts/run_morning_digest_
fargate.py``）はこの層の出力をそのまま並べるだけで済むようにしてある。

設計の芯（ユーザー承認済みモックの原則をコードに落としたもの）:
  - **分類軸は 1 本**: 「相手があなたの返事で止まっているか」。
    ``yours``（あなたの番） / ``watch``（様子見） / ``fyi``（見るだけ）の 3 つだけ。
  - **要約文を作らない**。外に出す引用（:attr:`HandoffCard.request_quote`）は
    **原文の逐語コピー**のみ。この層がやってよいのは「本文のどの文が依頼か」の *選択* と、
    固定語彙テーブルからの *組み合わせ* だけ。自由文生成はしない。
  - **読み取れなかった項目は空欄**。期限が曜日照合で食い違えば ``due_label`` は空にし、
    :attr:`HandoffCard.due_unresolved` を立てる（勝手に日付を埋めない）。
  - **「対応不要」を出力語彙に入れない**（自分に戻ってくる件を消してしまうため）。
    畳む側（watch / fyi）は :attr:`HandoffCard.fold_reason` で必ず理由を名乗る。
    ※ 差出人が本文に「対応不要」と書いている場合の *検知* は別問題なので
      :data:`_CLOSED_RE` に入っている。禁止しているのは **出力語彙** の方。
  - **補足行（``note``）は「原文を見る価値が本当にある件」だけ**。既定は空。

この層は純関数の集まり（I/O 無し・時刻は ``now`` 引数で注入）。``now`` を渡す設計に
しているのは、日付解決と経過日数がテストで固定できないと検証にならないため。
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any

# JST と日付表記は ``morning_digest/calendar_window`` が唯一の真実源（ここで再定義しない）。
# 同じ DM の中で「8/21(金) の予定」と「期限 8/21(金)」の書式が割れないようにするため。
from teamagent.skills.morning_digest.calendar_window import JST, fmt_jst_date, weekday_ja

# ── バケット（分類軸は「相手があなたの返事で止まっているか」の 1 本だけ）────────────
BUCKET_YOURS = "yours"
BUCKET_WATCH = "watch"
BUCKET_FYI = "fyi"
BUCKET_ORDER: tuple[str, ...] = (BUCKET_YOURS, BUCKET_WATCH, BUCKET_FYI)
BUCKET_LABELS: dict[str, str] = {
    BUCKET_YOURS: "あなたの番",
    BUCKET_WATCH: "様子見",
    BUCKET_FYI: "見るだけ",
}

# ── 依頼の型（所要時間・見出しの語尾を決めるキー）────────────────────────────────
KIND_TAKEOVER = "takeover"  # 作業を引き取る（あなたの作業が発生する）
KIND_SCHEDULE = "schedule"  # 日程・来社日などを答える
KIND_REPLY = "reply"  # 返信・確認だけで済む
KIND_UNKNOWN = "unknown"  # 依頼文が特定できなかった（＝推測しない）

#: 所要時間の目安（**固定表**。推測で数字を作らない＝表に無い型は空欄）。
EFFORT_BY_KIND: dict[str, str] = {
    KIND_TAKEOVER: "15分",
    KIND_SCHEDULE: "1分",
    KIND_REPLY: "2分",
    KIND_UNKNOWN: "",
}

#: 会話種別（``channel_kind``）→ 表示ラベル。``unknown`` は空欄（＝推測で埋めない）。
CHANNEL_LABELS: dict[str, str] = {"dm": "DM", "group_dm": "グループDM", "channel": "チャンネル"}

#: 本文の保持上限（``schema.SlackUnreadItem.excerpt_display`` の max_length と対）。
#: ここに達している本文は「途中で切れている」と見なして補足行を出す。
BODY_EXCERPT_CAP = 1500

#: 見出しに使える topic の最大長。超えたら固定の言い換えへ落とす（切り詰めて捏造しない）。
_MAX_TOPIC_LEN = 24

#: 依頼文をそのまま見出しに出せる最大長（型が判らなかったときの逐語フォールバック）。
_MAX_QUOTE_HEADLINE_LEN = _MAX_TOPIC_LEN * 2

# ── 畳んだ理由（固定文言・ここでしか作らない）──────────────────────────────────
REASON_ANSWERED_BY_OTHER = "他の人が先に答えています"
REASON_CLOSED = "この件は終了と書かれています"
REASON_BLOCKED = "いま返信不要"
REASON_DUE_PASSED = "相談日 {date} を過ぎています"
REASON_AMBIGUOUS_ADDRESSEE = "他{count}名も名指しで、あなた宛の依頼文は見つかりませんでした"

# ── 補足行（固定文言・「原文を見る価値が本当にある件」だけに付ける）──────────────
NOTE_DUE_UNRESOLVED = "原文の日付と曜日が食い違うため、期限は空欄にしています"
NOTE_BODY_TRUNCATED = "本文が途中で切れており、未取得の部分があります"
NOTE_NO_REQUEST = "依頼文を特定できませんでした"

# ── 正規表現辞書 ────────────────────────────────────────────────────────────

# 名指しトークン。実 ID は `[A-Z0-9]+` だが、テスト/移行データの `U_ME` 形式も
# 落とせるよう `_` まで許す（宛先表記であって「話題」ではないので topic からは外す）。
#
# ⚠️ ユーザーグループ `<!subteam^S08…>` / `<!here>` と **ラベル無しの** `<#C08…>` も
# ここで落とす。話題の切り出し（読点で割る・末尾を剥がす）はトークンの `<` `>` を
# 平気で分断するので、ここで落としておかないと `subteam^S08DESIGN1>` のような
# **生 ID の断片**が見出しに残り、描画側の畳み込み（_flatten_slack_text）にも
# 掛からなくなる（自前の敵対的入力テストで実測）。ラベル付き `<#C08…|general>` は
# 実在のチャンネル名＝話題になり得るので残す（描画側が `#general` に畳む）。
_MENTION_RE = re.compile(
    r"<@[UWB][A-Za-z0-9_.\-]*(?:\|[^>]*)?>"
    r"|<!subteam\^[A-Za-z0-9_.\-]+(?:\|[^>]*)?>"
    r"|<!(?:here|channel|everyone)(?:\|[^>]*)?>"
    r"|<#[A-Z0-9]+>"
)

#: Slack のマークアップトークン `<…>`（メンション・リンク・チャンネル参照）。
#: 文の区切り探索から中身を隠すのに使う（トークン内の `!` `。` で文を割らないため）。
_SLACK_TOKEN_RE = re.compile(r"<[^<>]*>")

#: 挨拶・結びだけの文（「よろしくお願いします。」等）。依頼文として選ばない。
_PLEASANTRY_RE = re.compile(
    r"^(?:よろしく)?(?:お願い|お世話|お疲れ|おはよう|こんにちは|こんばんは|ありがとう|失礼)"
    r"[^。]{0,12}[。．！!？?]?$"
)

#: 「相手が何かを求めている」文の目印。**この文を原文から選ぶだけ**（要約しない）。
_REQUEST_RE = re.compile(
    r"(ください|下さい|お願い|いただけ|頂け|もらえ|もらって|頂戴|ほしい|欲しい"
    r"|ますか|ませんか|でしょうか|ですか|いかが|どう(?:です|でしょう)"
    r"|教えて|返信|返事|回答|ご確認|確認して|対応して|引き取|引継|引き継|共有して"
    r"|[？?])"
)

#: 依頼の型（**上から順に判定**。作業引き取り＞日程回答＞返信のみ）。
_KIND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        KIND_TAKEOVER,
        re.compile(
            r"(引き取|引取|引継|引き継|巻き取|巻取|移管"
            r"|担当(?:を)?(?:代わ|変わ|お願い|変更)|お任せ|代わりに(?:対応|やって|進めて))"
        ),
    ),
    (
        KIND_SCHEDULE,
        re.compile(
            r"(日程|候補日|来社|訪問|ご都合|都合|空いて|空き|何時"
            r"|いつ(?:が|に|なら|頃)|日時|スケジュール|アポ|調整|リスケ)"
        ),
    ),
    (
        KIND_REPLY,
        re.compile(r"(確認|回答|返信|返事|教え|共有|連絡|意見|レビュー|承認|判断|コメント|お願い)"),
    ),
)

#: 終了宣言。※「対応不要」は **入力の検知語**（出力語彙としては禁止）。
_CLOSED_RE = re.compile(
    r"(一旦(?:クローズ|保留|中断)|クローズします|クローズしました|クローズ済"
    r"|完了しました|解決しました|解決済|決着しました|終了します|終了しました"
    r"|不要になりました|対応不要|見送り|取り下げ)"
)

#: 「前提が他人側にある」条件節の目印（marker）と、その前提の主語になりうる語（subject）。
#: 両方そろって初めて「いまは自分の番ではない」と判定する（「確認したら教えて」＝
#: 前提が自分側の件を誤って畳まないための二段構え）。
_BLOCK_MARKER_RE = re.compile(r"(次第|後に|後で|あとで|ましたら|たら|れば)")
_BLOCK_SUBJECT_RE = re.compile(
    r"(承認|決裁|稟議|審査|先方|クライアント|お客様|情シス|情報システム|上長|部長|役員"
    r"|法務|経理|人事|請求|入金|見積|回答|返答|返信|連絡|確認が取れ|結果|リリース|公開"
    r"|納品|到着|届い)"
)

#: 依頼文の末尾（丁寧表現）を剥がして「話題」だけ残すための辞書。**上から順に適用**。
_TAIL_STRIP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[。．！!？?…、，,\s　]+$"),
    re.compile(r"(?:ませ|ます|まし)?(?:た)?(?:でしょう|だろう)?か$"),
    re.compile(r"(?:して)?(?:いただけ|頂け|もらえ|下さ|くださ)(?:ます|る|れ|い|ませ)?$"),
    re.compile(r"(?:して)?(?:いただき|頂き)(?:たい)?$"),
    re.compile(r"(?:お願い|願い)(?:いた)?(?:し)?(?:ます|致します|たい)?$"),
    re.compile(r"(?:でき|出来)(?:ます|る)?$"),
    re.compile(r"(?:して)?(?:ほしい|欲しい)(?:です)?$"),
    re.compile(r"(?:です|でした|ます|ました|である)$"),
    # 「〜を確認」のように **助詞つき**でだけ動詞名詞を剥がす（「最終確認」を壊さない）。
    re.compile(
        r"[をにがはもでとへ]\s*(?:ご|お)?"
        r"(?:確認|回答|返信|返答|共有|連絡|対応|検討|判断|承認|了承|参加|出席|記入|入力|提出|返却|送付)"
        r"(?:して|し)?$"
    ),
    re.compile(
        r"(?:教えて|お教え|お戻し|お知らせ|ご対応|ご確認|ご返信|ご連絡|ご共有|ご回答|ご検討"
        r"|ご判断|引き取って|引取って|引き継いで|引継いで|巻き取って|送って|出して|進めて"
        r"|対応して|確認して|回答して|返信して|共有して|連絡して)$"
    ),
    re.compile(r"[をのはがにでとへも、]$"),
)

#: 助詞 + 漢字1字で終わる＝て形の動詞を途中で切った痕跡（「見積を出」「資料を送」）。
_DANGLING_STEM_RE = re.compile(r"[をにへ][一-龥]$")

#: 話題の末尾がこの形なら **名詞句として使えない**（条件節・否定・活用の途中）。
#: 固定語尾（「〜を確認」）を足すと日本語が壊れるので、話題ごと捨てて定型文言へ落とす
#: （長すぎる話題を切り詰めずに捨てるのと同じ扱い＝途中で切って捏造しない）。
_UNUSABLE_TOPIC_TAIL_RE = re.compile(
    r"(?:たら|れば|なら|ので|のに|から|けれど|けど|ながら|つつ|ないで|ない|ません|ます"
    r"|です|して|され|られ|せず|ず|でも|ても|たり|そう|よう|べき)$"
)

#: 話題の末尾から落とす飾り（「〜の件」等）。
_TOPIC_SUFFIX_RE = re.compile(r"(?:の件|について|につきまして|に関して|の話|の方)$")

#: サ変名詞で終わる話題（「最終確認」→「最終確認する」。「最終を確認」にしない）。
_SAHEN_TAIL_RE = re.compile(r"(?:確認|回答|返信|返事|連絡|共有|承認|判断|対応|引き取り|引継ぎ)$")

# ── 日付語の辞書（絶対日付・相対日付）──────────────────────────────────────────

_DATE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ymd",
        re.compile(
            r"(?P<y>\d{4})\s*[/／年\-]\s*(?P<m>\d{1,2})\s*[/／月\-]\s*(?P<d>\d{1,2})\s*日?"
            r"\s*(?:[（(](?P<w>[月火水木金土日])(?:曜日?)?[）)])?"
        ),
    ),
    (
        "md",
        re.compile(
            r"(?P<m>\d{1,2})\s*[/／月]\s*(?P<d>\d{1,2})\s*日?"
            r"\s*(?:[（(](?P<w>[月火水木金土日])(?:曜日?)?[）)])?"
        ),
    ),
    # 日だけ＋曜日（「28(金)」）。曜日が無い裸の数字は日付と断定しない。
    ("d", re.compile(r"(?P<d>\d{1,2})\s*日?\s*[（(](?P<w>[月火水木金土日])(?:曜日?)?[）)]")),
)

_RELATIVE_DATE_RE = re.compile(
    r"(今日|本日|明日|明後日|明々後日|週明け|今週中|今週いっぱい|今月中|月末|来週中|来週)"
)

#: 相対語 → 解決関数が無い＝「日付語ではあるが 1 日に絞れない」もの（空欄にする）。
_RELATIVE_UNRESOLVABLE: frozenset[str] = frozenset({"来週", "来週中"})

#: 日付語の **直後** がこの形なら「その日に何かが起きる日付」＝期限・予定日として読む。
#: 「8/17(月)にお願いできますか」「8/28(金)までに」「今月中に」。
#: ⚠️ 裸の「に」は「8/17(月)に関する資料」「8/17(月)にて実施」のような **説明の助詞**にも
#: 使われる。畳む側の誤りは見逃しに直結するので、その形は除外する（狭く取る）。
_DEADLINE_TRAILING_RE = re.compile(
    r"^\s*(?:までに|まで|迄に|迄|中に|には|に(?!関|つい|おけ|おい|て|より|よる|基づ|沿|対)|時点)"
)
#: 日付語の **手前/直後** にこの語があれば、助詞に関係なく期限と読む。
_DEADLINE_WORD_RE = re.compile(r"(期限|締切|締め切り|〆切|〆|期日|デッドライン|納期)")
#: 「8/17(月)に届いた請求書」のように、日付＋に が **連体修飾**（た/だ + 名詞）へ続く形。
#: これは「その日が期限」ではなく「その日に起きた事の説明」なので期限として読まない。
#: ⚠️ 畳む側の誤りは見逃しに直結するので、疑わしい形は期限と見なさない側へ倒す。
_DEADLINE_RENTAI_RE = re.compile(r"^\s*に[^、。．]{1,10}?[ただ](?=[一-龥ァ-ヶA-Za-z0-9])")

#: 「日付だけの断片」判定（話題を選ぶとき、日付の断片を話題にしないため）。
_DATE_ONLY_CHARS_RE = re.compile(r"^[\s　0-9０-９/／\-年月日（）()月火水木金土日曜まで迄中～\-]+$")
_DIGIT_RE = re.compile(r"[0-9０-９]")
_RELATIVE_ONLY_RE = re.compile(
    r"^(?:今日|本日|明日|明後日|明々後日|週明け|今週中|今週いっぱい|今月中|月末|来週中|来週)"
    r"(?:まで|迄|中|に|には|の)?$"
)


# ── 入力（schema からも provider からも独立した正規化データ）──────────────────


@dataclass(frozen=True)
class HandoffSource:
    """判定に必要な事実だけを写した入力（描画・schema に依存しない）。"""

    text: str = ""
    occurred_at: str | None = None
    channel_kind: str = "unknown"
    permalink: str | None = None
    from_user_id: str | None = None
    from_display_name: str | None = None
    mentioned_user_ids: tuple[str, ...] = ()
    answered_by_other: bool = False
    sender_followed_up: bool = False
    thread_message_count: int = 0
    body_truncated: bool | None = None
    """本文が途中で切れているか。None なら本文長（:data:`BODY_EXCERPT_CAP`）から推定。"""


def source_from_item(item: Any) -> HandoffSource:
    """``SlackUnreadItem`` / ``UnrepliedMention`` 等から :class:`HandoffSource` を作る。

    属性名は ``SlackUnreadItem``（描画に渡る形）を正とし、無い属性は
    ``UnrepliedMention``（provider の生値）側の名前で拾う。どちらにも無ければ既定値
    ＝「読み取れなかった」を維持する（推測で埋めない）。
    """

    def _s(*names: str) -> str:
        for n in names:
            v = getattr(item, n, None)
            if isinstance(v, str) and v:
                return v
        return ""

    def _b(name: str) -> bool:
        return bool(getattr(item, name, False))

    mentioned = getattr(item, "mentioned_user_ids", ()) or ()
    truncated = getattr(item, "body_truncated", None)
    return HandoffSource(
        text=_s("excerpt_display", "text"),
        occurred_at=_s("occurred_at") or None,
        channel_kind=_s("channel_kind") or "unknown",
        permalink=_s("permalink") or None,
        from_user_id=_s("from_user_id", "user") or None,
        from_display_name=_s("from_display_name", "user_display") or None,
        mentioned_user_ids=tuple(str(u) for u in mentioned if u),
        answered_by_other=_b("answered_by_other"),
        sender_followed_up=_b("sender_followed_up"),
        thread_message_count=int(getattr(item, "thread_message_count", 0) or 0),
        body_truncated=(bool(truncated) if truncated is not None else None),
    )


# ── 出力 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DueResolution:
    """本文中の日付語を絶対日付へ解決した結果。"""

    due_date: _dt.date | None = None
    label: str = ""
    """期限なら "期限 8/28(金)"、そうでなければ "8/28(金) の記載あり"。未解決は **空**。"""
    source_text: str = ""
    """原文にあった日付語（逐語）。空 = 日付語そのものが無かった。"""
    unresolved: bool = False
    """日付語はあったが 1 日に絞れなかった（曜日不一致・「来週」等）。"""
    is_deadline: bool = False
    """その日付が **期限・予定日** として書かれているか（「までに」「に」「期限」等）。

    ⚠️ False は「本文に日付が出てきただけ」＝請求書の発行日・議事録の開催日など。
    これを期限と呼ぶのも、これで件を畳むのも **原文に無い主張**なのでやらない。
    """

    @property
    def has_date_word(self) -> bool:
        return bool(self.source_text)


@dataclass(frozen=True)
class HandoffCard:
    """DM に 1 行で並べるカード 1 件（描画はこれを並べるだけ）。"""

    bucket: str
    headline: str
    """動詞で終わる短い見出し（固定語彙 × 原文からの切り出しの組み合わせ）。"""
    request_kind: str
    request_quote: str
    """依頼文の **原文逐語コピー**。特定できなければ空。"""
    context: str
    """見出しの手前にあった話題（原文の逐語切り出し・「NTVカードの受け渡し」等）。無ければ空。"""
    channel_label: str
    elapsed_days: int | None
    elapsed_label: str
    due_date: _dt.date | None
    due_label: str
    """"期限 8/28(金)"。**期限として書かれていた**ときだけ非空（時間軸の chip を置き換える）。"""
    date_mention_label: str
    """"8/17(月) の記載あり"。期限ではない日付語が原文にあったときだけ非空。

    畳まない（＝あなたの番のままにする）代わりに、本文に日付が出ていた事実は残す。
    経過日数を消さない位置に置くので「2日経過 ・8/17(月) の記載あり」と並ぶ。
    """
    due_source_text: str
    due_unresolved: bool
    effort_label: str
    """所要時間の目安。``yours`` のときだけ入る（畳んだ件に「あなたの作業」は無い）。"""
    mentioned_others: int
    note: str
    """補足行。「原文を見る価値が本当にある件」だけ非空。"""
    fold_reason: str
    """``watch`` / ``fyi`` に畳んだ理由（固定文言）。``yours`` は空。"""
    permalink: str
    from_display_name: str | None
    source_index: int
    index: int = 0
    """表示順の通し番号（1 始まり・:func:`triage_slack_handoff` が採番）。"""


@dataclass(frozen=True)
class HandoffDigest:
    """判定済みカードの束（バケット順 → バケット内は :func:`sort_key` 順）。"""

    cards: tuple[HandoffCard, ...] = ()

    @property
    def total(self) -> int:
        return len(self.cards)

    def cards_in(self, bucket: str) -> tuple[HandoffCard, ...]:
        return tuple(c for c in self.cards if c.bucket == bucket)

    def count(self, bucket: str) -> int:
        return sum(1 for c in self.cards if c.bucket == bucket)

    def summary_label(self) -> str:
        """ "あなたの番 3・様子見 1・見るだけ 1"（0 件のバケットは出さない）。"""
        parts = [f"{BUCKET_LABELS[b]} {self.count(b)}" for b in BUCKET_ORDER if self.count(b) > 0]
        return "・".join(parts)


# ── 純関数（それぞれ単体でテストできる粒度に割る）────────────────────────────


def _strip_mentions(text: str) -> str:
    return _MENTION_RE.sub("", text or "")


def _mask_slack_tokens(text: str) -> str:
    """`<…>` の中身を同じ長さの `_` に潰す（**オフセットは原文と 1:1** のまま）。

    文の区切り判定にだけ使う。返り値を外に出さないこと（原文ではない）。
    """
    return _SLACK_TOKEN_RE.sub(lambda m: "<" + "_" * (len(m.group(0)) - 2) + ">", text)


def sentences(text: str) -> list[str]:
    """本文を文へ割る（原文の文字はいじらない・空白 trim のみ）。

    ⚠️ 区切り記号の探索は **Slack トークンを潰した写し**の上で行う。`<!subteam^S08…>`
    `<!here>` の `!` や、リンクラベル `<https://x|資料はこちら。詳細>` の `。` で文を切ると、
    トークンが `subteam^S08DESIGN1>` のような **生 ID の断片**に割れて見出しへ流れる
    （自前の敵対的入力テストで実測。断片は描画側の畳み込みにも掛からない）。
    切り出す実体は必ず原文（``raw``）から取る＝引用の逐語性は保つ。
    """
    raw = str(text or "")
    masked = _mask_slack_tokens(raw)
    out: list[str] = []
    seg_start = 0
    for i, ch in enumerate(masked):
        if ch in "\n\r":  # 改行は文の区切りだが文字としては残さない
            chunk, seg_start = raw[seg_start:i], i + 1
        elif ch in "。．！!？?":  # 句点は文の末尾として残す
            chunk, seg_start = raw[seg_start : i + 1], i + 1
        else:
            continue
        if chunk.strip():
            out.append(chunk.strip())
    if raw[seg_start:].strip():
        out.append(raw[seg_start:].strip())
    return out


def extract_request_quote(text: str) -> str:
    """「相手が求めていること」を含む文を **原文から選ぶ**（生成しない）。

    見つからなければ空文字（＝依頼文なし。勝手に作らない）。
    """
    for s in sentences(text):
        bare = _strip_mentions(s).strip()
        if not bare or _PLEASANTRY_RE.match(bare):
            continue
        if _REQUEST_RE.search(bare):
            return s
    return ""


def classify_request_kind(text: str) -> str:
    """依頼の型を固定辞書で判定する（作業引き取り＞日程回答＞返信のみ）。"""
    bare = _strip_mentions(text or "")
    for kind, pat in _KIND_PATTERNS:
        if pat.search(bare):
            return kind
    return KIND_UNKNOWN


def effort_for_kind(kind: str) -> str:
    """所要時間の目安（固定表・表に無い型は空欄）。"""
    return EFFORT_BY_KIND.get(kind, "")


def channel_label(channel_kind: str) -> str:
    """会話種別のラベル。``unknown`` は空欄（＝判定できなかった）。"""
    return CHANNEL_LABELS.get((channel_kind or "").strip(), "")


def _strip_request_tail(sentence: str) -> str:
    """依頼文の末尾（丁寧表現・動詞）を辞書順に剥がして「話題」だけ残す。"""
    s = (sentence or "").strip()
    for _ in range(12):
        before = s
        for pat in _TAIL_STRIP_PATTERNS:
            m = pat.search(s)
            if m is not None and m.start() > 0:
                s = s[: m.start()]
                break
        if s == before:
            break
    return _drop_dangling_stem(s.strip())


def _drop_dangling_stem(topic: str) -> str:
    """「B社案で見積を出」のように、て形の途中で切れた動詞語幹を落とす。

    ``(?:して)?(?:もらえ|いただけ)`` を剥がすと「出してもらえ」の「出」だけが残る。
    助詞 + 漢字1字で終わる形はこの取りこぼしなので、助詞ごと落として名詞で終わらせる
    （日本語として壊れた見出しを外に出さないため。名詞なら手前の助詞で切れて残らない）。
    """
    return _DANGLING_STEM_RE.sub("", topic) if topic else topic


def _is_date_only(segment: str) -> bool:
    """その断片が「日付語だけ」か（話題として採らないための判定）。"""
    s = (segment or "").strip()
    if not s:
        return True
    if _RELATIVE_ONLY_RE.match(s):
        return True
    return bool(_DATE_ONLY_CHARS_RE.match(s)) and bool(_DIGIT_RE.search(s))


def _clean_segment(segment: str) -> str:
    return _TOPIC_SUFFIX_RE.sub("", segment.strip()).strip(" 　\t・:：-—")


def extract_topic_and_context(sentence: str) -> tuple[str, str]:
    """依頼文から「話題」と「その手前の文脈」を **原文の部分文字列として** 切り出す。

    ①名指しトークン（宛先であって話題ではない）を落とす → ②読点で割って
    「日付だけではない最後の断片」を話題に採る → ③その 1 つ手前の断片を文脈に採る
    （「NTVカードの受け渡しの件、来社日を教えて」→ 話題「来社日」／文脈「NTVカードの受け渡し」）。
    ④「〜の件」等の飾りを落とす。**どちらも逐語の切り出しで、要約はしない。**
    """
    s = _strip_request_tail(sentence)
    s = _strip_mentions(s).strip()
    segments = [x.strip() for x in re.split(r"[、，,]", s) if x.strip()]
    if not segments:
        return (_clean_segment(s), "")
    non_date = [x for x in segments if not _is_date_only(x)]
    topic_raw = non_date[-1] if non_date else segments[0]
    pos = segments.index(topic_raw)
    context_raw = ""
    for earlier in reversed(segments[:pos]):
        if not _is_date_only(earlier):
            context_raw = earlier
            break
    context = _clean_segment(context_raw)
    if len(context) > _MAX_TOPIC_LEN:
        context = ""  # 長すぎる文脈は切り詰めずに捨てる（途中で切って捏造しない）
    return (_clean_segment(topic_raw), context)


def extract_topic(sentence: str) -> str:
    """:func:`extract_topic_and_context` の話題だけを返す薄いラッパ。"""
    return extract_topic_and_context(sentence)[0]


def _weekday_ja(day: _dt.date) -> str:
    return weekday_ja(day)


def format_date_ja(day: _dt.date) -> str:
    """ "8/28(金)" 形式（``calendar_window.fmt_jst_date`` と同一実装）。"""
    return fmt_jst_date(day)


def date_label(day: _dt.date, *, is_deadline: bool, word: str = "") -> str:
    """日付 chip の文言。**期限と名乗るのは期限として書かれているときだけ**。

    ``word`` は相対語の逐語（「明日」等）。期限でない日付は「原文に日付の記載がある」
    という事実だけを述べる（請求書の発行日を「期限」と呼ばないため）。
    """
    shown = f"{word}({format_date_ja(day)})" if word else format_date_ja(day)
    return f"期限 {shown}" if is_deadline else f"{shown} の記載あり"


def is_deadline_context(text: str, start: int, end: int) -> bool:
    """本文の ``[start:end)`` にある日付語が **期限・予定日** として書かれているか。

    判定材料は 3 つだけ（推測しない）:
      1. 直後が「までに/まで/中に/に/には/時点」＝その日に何かが起きる書き方
      2. 直前 6 字 or 直後 8 字に「期限/締切/〆/期日/納期」がある
      3. ただし「8/17(月)に届いた請求書」のような **連体修飾** は 1 を取り消す
         （日付が説明の一部であって期限ではない）

    「8/17(月)の請求書」「8/14(金)分の稼働表」のように ``の`` ``分`` が続く形は
    どれにも当たらない＝期限ではない（＝これで件を畳まない）。
    """
    body = text or ""
    after = body[end : end + 12]
    before = body[max(0, start - 6) : start]
    if _DEADLINE_WORD_RE.search(before) or _DEADLINE_WORD_RE.search(after[:8]):
        return True
    if _DEADLINE_TRAILING_RE.match(after) is None:
        return False
    return _DEADLINE_RENTAI_RE.match(after) is None


def _resolve_day_only(day_num: int, today: _dt.date) -> _dt.date | None:
    """「28(金)」のような日だけの表記を、今日以降の最も近い同日へ寄せる。"""
    if not 1 <= day_num <= 31:
        return None
    for months_ahead in (0, 1, 2):
        year = today.year + (today.month - 1 + months_ahead) // 12
        month = (today.month - 1 + months_ahead) % 12 + 1
        try:
            cand = _dt.date(year, month, day_num)
        except ValueError:
            continue
        if cand >= today:
            return cand
    return None


def _resolve_relative(word: str, today: _dt.date) -> _dt.date | None:
    if word in _RELATIVE_UNRESOLVABLE:
        return None
    if word in ("今日", "本日"):
        return today
    if word == "明日":
        return today + _dt.timedelta(days=1)
    if word == "明後日":
        return today + _dt.timedelta(days=2)
    if word == "明々後日":
        return today + _dt.timedelta(days=3)
    if word == "週明け":
        return today + _dt.timedelta(days=(7 - today.weekday()) or 7)
    if word in ("今週中", "今週いっぱい"):
        friday = today + _dt.timedelta(days=4 - today.weekday())
        return max(friday, today)
    if word in ("今月中", "月末"):
        first_next = _dt.date(
            today.year + (1 if today.month == 12 else 0),
            1 if today.month == 12 else today.month + 1,
            1,
        )
        return first_next - _dt.timedelta(days=1)
    return None


def resolve_due(text: str, now: _dt.datetime) -> DueResolution:
    """本文中の日付語を絶対日付へ解決する。**曜日が実際と一致しなければ空欄にする**。

    「28(金)」の (金) は書き手の主張。実際の曜日と食い違うなら、どちらが正しいか
    こちらには決められない → ``due_date=None`` / ``label=""`` / ``unresolved=True``
    として「日付解決不可」を明示する（勝手にどちらかへ寄せない）。
    """
    body = str(text or "")
    today = now.astimezone(JST).date() if now.tzinfo else now.replace(tzinfo=JST).date()

    best: tuple[int, int, str, re.Match[str]] | None = None
    for name, pat in _DATE_PATTERNS:
        m = pat.search(body)
        if m is None:
            continue
        cand = (m.start(), -(m.end() - m.start()), name, m)
        if best is None or cand[:2] < best[:2]:
            best = cand
    rel = _RELATIVE_DATE_RE.search(body)
    if rel is not None and (best is None or rel.start() < best[0]):
        word = rel.group(1)
        rel_day = _resolve_relative(word, today)
        if rel_day is None:
            # 日付語ではあるが 1 日に絞れない（「来週」等）。空欄のまま印だけ付ける。
            return DueResolution(source_text=word, unresolved=True)
        deadline = is_deadline_context(body, rel.start(), rel.end())
        return DueResolution(
            due_date=rel_day,
            label=date_label(rel_day, is_deadline=deadline, word=word),
            source_text=word,
            unresolved=False,
            is_deadline=deadline,
        )
    if best is None:
        return DueResolution()

    _, _, name, m = best
    raw = m.group(0).strip()
    weekday = m.groupdict().get("w") or ""
    day: _dt.date | None
    if name == "d":
        day = _resolve_day_only(int(m.group("d")), today)
    else:
        month = int(m.group("m"))
        dom = int(m.group("d"))
        year = int(m.group("y")) if name == "ymd" else today.year
        try:
            day = _dt.date(year, month, dom)
        except ValueError:
            day = None
        # 年の無い「1/5」を年末に見たら翌年（90 日以上の過去は書き間違いより越年が自然）。
        if day is not None and name == "md" and (today - day).days > 90:
            try:
                day = _dt.date(year + 1, month, dom)
            except ValueError:
                day = None
    if day is None:
        return DueResolution(source_text=raw, unresolved=True)
    if weekday and _weekday_ja(day) != weekday:
        # 曜日照合に落ちた＝解決不可。空欄にして、原文を見る価値がある件として印を付ける。
        return DueResolution(source_text=raw, unresolved=True)
    deadline = is_deadline_context(body, m.start(), m.end())
    return DueResolution(
        due_date=day,
        label=date_label(day, is_deadline=deadline),
        source_text=raw,
        unresolved=False,
        is_deadline=deadline,
    )


def format_date_ja_in(text: str, due: DueResolution) -> bool:
    """畳んだ理由がその期限日をすでに名乗っているか（chip の重複表示を避けるため）。"""
    return bool(due.due_date is not None and format_date_ja(due.due_date) in (text or ""))


def _fold_reason_owns_the_date(fold_reason: str, due: DueResolution) -> bool:
    """畳んだ理由が同じ日付を名乗っているか（chip 側を空にする条件）。"""
    return bool(due.label and fold_reason and format_date_ja_in(fold_reason, due))


def elapsed_days_from(occurred_at: str | None, now: _dt.datetime) -> int | None:
    """メンション日時からの経過日数（暦日差）。読めなければ None。"""
    s = (occurred_at or "").strip()
    if not s:
        return None
    try:
        dt = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    today = now.astimezone(JST).date() if now.tzinfo else now.replace(tzinfo=JST).date()
    return max(0, (today - dt.astimezone(JST).date()).days)


def elapsed_label(days: int | None) -> str:
    if days is None:
        return ""
    return "今日" if days <= 0 else f"{days}日経過"


def count_mentioned_others(mentioned: Sequence[str], me_user_id: str | None) -> int:
    """自分以外に名指しされている人数。``me_user_id`` 不明なら 1 人（自分）を差し引く。"""
    ids = [u for u in dict.fromkeys(mentioned) if u]
    if not ids:
        return 0
    if me_user_id:
        return sum(1 for u in ids if u != me_user_id)
    return max(0, len(ids) - 1)


def _blocked_parts(quote: str) -> tuple[str, str] | None:
    """「前提が他人側にある」条件節を (条件, その後にやること) に割る。無ければ None。"""
    if not quote:
        return None
    m = _BLOCK_MARKER_RE.search(quote)
    if m is None:
        return None
    condition_raw = quote[: m.start()]
    if not _BLOCK_SUBJECT_RE.search(_strip_mentions(condition_raw)):
        return None
    condition = extract_topic(condition_raw)
    action = _strip_request_tail(quote[m.end() :].lstrip(" 　、，,"))
    action = _strip_mentions(action).strip(" 　、，,")
    return (condition, action)


def is_closed_declaration(text: str) -> bool:
    """終了宣言か。**最後の文だけ**を見る。

    「前の件はクローズしました。別件で〜お願いします。」のように、前置きで昔の件を
    締めてから新しい依頼をしてくる本文がある。全文検索で畳むと、その新しい宿題が
    丸ごと消える。畳む側の誤りは見逃しに直結するので、ここは狭く取る。
    """
    sents = sentences(text)
    if not sents:
        return False
    return bool(_CLOSED_RE.search(_strip_mentions(sents[-1])))


def classify_bucket(
    src: HandoffSource, *, quote: str, due: DueResolution, others: int, today: _dt.date
) -> tuple[str, str]:
    """バケットと「畳んだ理由」を決める（``yours`` の理由は空）。

    判定材料は 5 つだけ: 終了宣言 / 前提が他人側にある条件付き依頼 / 他人が代わりに
    答えたか / 相談日が過ぎているか / 名指し人数（＋依頼文の有無）。
    差出人からの催促（``sender_followed_up``）は **畳む判定より強い**
    ＝催促が来ている件は必ず ``yours`` に残す。
    """
    if is_closed_declaration(src.text):
        return (BUCKET_FYI, REASON_CLOSED)
    if _blocked_parts(quote) is not None:
        return (BUCKET_FYI, REASON_BLOCKED)
    if src.sender_followed_up:
        return (BUCKET_YOURS, "")
    if src.answered_by_other:
        return (BUCKET_WATCH, REASON_ANSWERED_BY_OTHER)
    # 過去日で畳むのは、その日付が **期限・予定日として書かれている** ときだけ。
    # 「8/17(月)の請求書をご確認ください」の 8/17 は発行日であって期限ではない。
    # これを畳むと、まだ自分の番の宿題が 🔴 から消える（＝見逃しに直結する）。
    if due.is_deadline and due.due_date is not None and due.due_date < today:
        return (BUCKET_WATCH, REASON_DUE_PASSED.format(date=format_date_ja(due.due_date)))
    if not quote and others >= 1:
        return (BUCKET_WATCH, REASON_AMBIGUOUS_ADDRESSEE.format(count=others))
    return (BUCKET_YOURS, "")


def build_headline(*, bucket: str, fold_reason: str, kind: str, quote: str, others: int) -> str:
    """見出しを **固定語彙 × 原文からの切り出し** で組む（自由文生成をしない）。"""
    if bucket == BUCKET_FYI and fold_reason == REASON_BLOCKED:
        parts = _blocked_parts(quote)
        if parts is not None:
            condition, action = parts
            if condition and action and len(condition) + len(action) <= _MAX_TOPIC_LEN * 2:
                return f"{condition}後に{action}"
            if condition and len(condition) <= _MAX_TOPIC_LEN:
                return f"{condition}を待っている"
        return "相手の対応待ち"

    topic = extract_topic(quote) if quote else ""
    if len(topic) > _MAX_TOPIC_LEN or _UNUSABLE_TOPIC_TAIL_RE.search(topic):
        topic = ""

    if bucket == BUCKET_FYI:  # 終了宣言
        return f"{topic}は終了と書かれている" if topic else "終了と書かれている"
    if bucket == BUCKET_WATCH:
        if fold_reason == REASON_ANSWERED_BY_OTHER:
            return f"{topic}は他の人が答えている" if topic else "他の人が答えている"
        if fold_reason.startswith("相談日"):
            return f"{topic}は当日が過ぎている" if topic else "当日が過ぎている"
        return f"{topic}は宛先が絞れていない" if topic else "宛先が絞れていない"

    # ── あなたの番 ──
    # 型が判らなかった件に固定語尾（「〜を確認」）を足すと、原文に無い述語を作ってしまう
    # （「請求書だけ送ってください」→「請求書だけを確認」）。型が判らないときは
    # **依頼文をそのまま**出す（宛先トークンだけ落とした逐語）。長すぎるなら定型文言へ。
    if kind == KIND_UNKNOWN:
        verbatim = _strip_mentions(quote).strip(" 　").rstrip("。．！!？?、，,")
        if verbatim and len(verbatim) <= _MAX_QUOTE_HEADLINE_LEN:
            return verbatim
        return topic or "原文を見る"
    if not topic:
        return {
            KIND_TAKEOVER: "作業の引き取りを返す",
            KIND_SCHEDULE: "日程を返す",
            KIND_REPLY: "返信する",
        }[kind]
    if _SAHEN_TAIL_RE.search(topic):
        return f"{topic}する"
    if kind == KIND_TAKEOVER:
        return f"{topic}を引き取る"
    if kind == KIND_SCHEDULE:
        return f"{topic}を返す"
    return f"{topic}を確認"


def build_note(*, bucket: str, due: DueResolution, body_truncated: bool, quote: str) -> str:
    """補足行。**「原文を見る価値が本当にある件」だけ**（既定は空）。"""
    if due.unresolved:
        return NOTE_DUE_UNRESOLVED
    if body_truncated:
        return NOTE_BODY_TRUNCATED
    if bucket == BUCKET_YOURS and not quote:
        return NOTE_NO_REQUEST
    return ""


def sort_key(card: HandoffCard) -> tuple[int, int, int, int, int]:
    """並び順（バケット内）。作業発生 → 経過時間 → 名指し人数 → 期限語の有無 → 元順。

    「あなたの作業が発生する件」を先頭に置くのは承認済みモックの並び
    （引き取り 2日経過 が 来社日 3日経過 より上）に合わせたもの。
    """
    work = 1 if card.request_kind == KIND_TAKEOVER else 0
    return (
        -work,
        -(card.elapsed_days or 0),
        -card.mentioned_others,
        -(1 if card.due_source_text else 0),
        card.source_index,
    )


def build_card(
    src: HandoffSource, *, now: _dt.datetime, me_user_id: str | None, index: int
) -> HandoffCard:
    """1 件ぶんの判定（純関数）。"""
    today = now.astimezone(JST).date() if now.tzinfo else now.replace(tzinfo=JST).date()
    quote = extract_request_quote(src.text)
    kind = classify_request_kind(quote or src.text)
    # 表示する期限は依頼文の中の日付を最優先。依頼文に日付語が無いときだけ本文全体へ広げる
    # （「ご確認ください。期限は8/28(金)です。」のように別の文に書かれる形を落とさないため）。
    due_in_quote = resolve_due(quote, now) if quote else DueResolution()
    due = due_in_quote if due_in_quote.has_date_word else resolve_due(src.text, now)
    # ただし **畳む** 判断に使うのは依頼文の中の日付だけ。本文の別の文にある無関係な
    # 過去日（議事録の日付等）で「当日が過ぎている」と決めつけて宿題を消さない。
    due_for_fold = due_in_quote if quote else due
    others = count_mentioned_others(src.mentioned_user_ids, me_user_id)
    bucket, fold_reason = classify_bucket(
        src, quote=quote, due=due_for_fold, others=others, today=today
    )
    headline = build_headline(
        bucket=bucket, fold_reason=fold_reason, kind=kind, quote=quote, others=others
    )
    date_chip = due.label
    # 見出しが既にその日付を含んでいるなら chip では繰り返さない（1 件 1 行・重複させない）。
    if due.source_text and (due.source_text in headline or format_date_ja_in(headline, due)):
        date_mention_chip = ""
    else:
        date_mention_chip = date_chip
    # 文脈は見出しに含まれていないときだけ出す（同じ言葉を 2 回見せない）。
    context = extract_topic_and_context(quote)[1] if quote else ""
    if context and context in headline:
        context = ""
    days = elapsed_days_from(src.occurred_at, now)
    truncated = (
        src.body_truncated
        if src.body_truncated is not None
        else len(src.text or "") >= BODY_EXCERPT_CAP
    )
    return HandoffCard(
        bucket=bucket,
        headline=headline,
        request_kind=kind,
        request_quote=quote,
        context=context,
        channel_label=channel_label(src.channel_kind),
        elapsed_days=days,
        elapsed_label=elapsed_label(days),
        due_date=due.due_date,
        # 畳んだ理由が同じ日付を名乗っているときは chip 側を空にする（1 件 1 行・重複させない）。
        due_label=("" if _fold_reason_owns_the_date(fold_reason, due) else date_chip)
        if due.is_deadline
        else "",
        date_mention_label=(
            ""
            if (due.is_deadline or _fold_reason_owns_the_date(fold_reason, due))
            else date_mention_chip
        ),
        due_source_text=due.source_text,
        due_unresolved=due.unresolved,
        # 畳んだ件に「あなたの作業」は無いので所要時間も出さない。
        effort_label=effort_for_kind(kind) if bucket == BUCKET_YOURS else "",
        mentioned_others=others,
        note=build_note(bucket=bucket, due=due, body_truncated=truncated, quote=quote),
        fold_reason=fold_reason,
        permalink=src.permalink or "",
        from_display_name=src.from_display_name,
        source_index=index,
    )


def triage_slack_handoff(
    items: Iterable[Any],
    *,
    now: _dt.datetime,
    me_user_id: str | None = None,
) -> HandoffDigest:
    """``SlackUnreadItem`` の列を判定済みカードへ変換する（この層の入口）。

    ``now`` は JST の現在時刻（テストで固定できるよう必ず注入する）。
    """
    cards = [
        build_card(source_from_item(it), now=now, me_user_id=me_user_id, index=i)
        for i, it in enumerate(items)
    ]
    ordered: list[HandoffCard] = []
    for bucket in BUCKET_ORDER:
        ordered.extend(sorted((c for c in cards if c.bucket == bucket), key=sort_key))
    return HandoffDigest(cards=tuple(replace(c, index=i + 1) for i, c in enumerate(ordered)))


__all__ = [
    "BUCKET_FYI",
    "BUCKET_LABELS",
    "BUCKET_ORDER",
    "BUCKET_WATCH",
    "BUCKET_YOURS",
    "CHANNEL_LABELS",
    "EFFORT_BY_KIND",
    "KIND_REPLY",
    "KIND_SCHEDULE",
    "KIND_TAKEOVER",
    "KIND_UNKNOWN",
    "NOTE_BODY_TRUNCATED",
    "NOTE_DUE_UNRESOLVED",
    "NOTE_NO_REQUEST",
    "REASON_AMBIGUOUS_ADDRESSEE",
    "REASON_ANSWERED_BY_OTHER",
    "REASON_BLOCKED",
    "REASON_CLOSED",
    "REASON_DUE_PASSED",
    "DueResolution",
    "HandoffCard",
    "HandoffDigest",
    "HandoffSource",
    "build_card",
    "build_headline",
    "build_note",
    "channel_label",
    "classify_bucket",
    "classify_request_kind",
    "count_mentioned_others",
    "date_label",
    "effort_for_kind",
    "elapsed_days_from",
    "elapsed_label",
    "extract_request_quote",
    "extract_topic",
    "extract_topic_and_context",
    "format_date_ja",
    "format_date_ja_in",
    "is_closed_declaration",
    "is_deadline_context",
    "resolve_due",
    "sentences",
    "sort_key",
    "source_from_item",
    "triage_slack_handoff",
]
