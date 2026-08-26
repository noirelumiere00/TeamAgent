"""「連携」依頼の決定論判定（純粋関数のみ・IO/LLM/DB 非依存）。

## なぜ要るか（本番実測・2026-08）

パイロット利用者が DM で「連携」と言ったのに ``oauth_connect`` が **1 度も呼ばれなかった**。
接続・認証・身元解決はすべて健全で、外側 LLM が **ツールを選ばなかった**だけだった
（1・2 ターン目はツール呼び出しゼロ、3 ターン目は ``search`` に落ちて資料検索が返った）。

プロンプト（SOUL.md）の強化だけでは「LLM の気分」に依存し続ける。そこで
**LLM の tool 選択を待たない決定論の受け口**をここに置き、MCP 境界
（:func:`teamagent.mcp_gateway.server.dispatch_tool`）が自由文引数を見て
``oauth_connect`` へ寄せる。

## 判定方式: 残差法（単純部分一致は禁止）

「連携」を部分一致で拾うと **「〇〇社との連携について提案書を作って」まで奪う**。
そこで :mod:`teamagent.skills._shared.client_name_guard` と同じ残差法を採る:

1. 正規化（NFKC → メンション/URL 除去 → 記号・絵文字・空白の除去 → casefold）
2. 依頼の**外側**から、丁寧語・サービス名の前置きと、依頼語尾の後置きを削る
3. 残差が **連携語ちょうど 1 個と完全一致** したときだけ発火する

## 残差一致だけでは足りない（2026-08 レッドチーム実測）

判定対象は利用者の生発話ではなく **外側 LLM が要約した tool 引数**である。LLM は文脈語尾を
落として名詞句へ凝縮するため、誤爆を防いでいた語尾・長さの手掛かりが判定時点で消えている。
実際、残差一致だけだと ``メール認証`` ``コネクト`` ``連動`` ``authorization``（＝資料検索の
素キーワード）や ``連携とは``（＝話題提示。裸助詞が多段で剥がれる）まで発火した。

そこで連携語を **強／弱** に割り、剥がした後置きが**依頼マーカー**
（:data:`_REQUEST_MARKERS`＝「して」「したい」「お願い」「リンク」等。裸助詞とコピュラは含めない）
だったかを持ち回り、発火条件を次に絞る:

    残差 ∈ :data:`_CORE_TERMS` かつ
    （依頼マーカーを剥がした **または**
    残差 ∈ :data:`_STRONG_TERMS` かつ後置きを 1 つも剥がしていない）

- ``連携`` ``Google連携`` … 強語＋前置きのみ → 発火
- ``連携して`` ``認証して`` ``接続したい`` ``連携リンク`` … マーカーあり → 発火
- ``メール認証`` ``コネクト`` ``連動`` ``authorization`` … 弱語＋マーカー無し → **発火しない**
- ``連携とは`` ``コネクトの`` … 助詞だけ → **発火しない**

これで :func:`detect_connect_intent_in_args` が ``client_name`` を見ない理由
（「コネクト」「連動」は実在社名）と、``query`` 欄での挙動が初めて一致する。

加えて「短い依頼文で連携語が主辞」であることを、正規化後の長さ上限
（:data:`_MAX_NORMALIZED_LEN`）で明示する。長文の中に「連携して」が現れるだけの文
（「先方と連携して進める件、議事録まとめて」）は 2 の段階で既に落ちるが、
**主辞であることは長さでも宣言しておく**（設計意図を機械で固定する）。

## 誤爆のコスト非対称性

発火しすぎ（＝資料検索の依頼を連携リンクで潰す）は、発火しなさすぎ（＝従来どおり
LLM 判断に委ねる＝現状維持）より明確に悪い。判定に迷う入力は**発火させない**側へ倒す。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final

# ── 正規化 ───────────────────────────────────────────────────────────────────

# Slack のメンション/チャンネル参照/特殊メンション（``<@U…>`` ``<#C…|name>`` ``<!here>``）。
# 記号除去より **先に** 落とす（後だと `<@U0B990FG03T>` が "u0b990fg03t" として残る）。
_SLACK_REF_RE: Final[re.Pattern[str]] = re.compile(r"<[@#!][^>]*>")

# 素の「@名前」表記と URL。どちらも記号除去で本文へ溶けるので先に落とす。
_AT_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"@\S+")
_URL_RE: Final[re.Pattern[str]] = re.compile(r"https?://\S+")

# 残す Unicode 一般カテゴリ。L=文字 / N=数字 / M=結合文字。
# P（約物）・S（記号・**絵文字**）・Z（空白）・C（制御）は落とす。
# 「ー」は Lm なので残る（カタカナ語を壊さない）。
_KEEP_CATEGORIES: Final[tuple[str, ...]] = ("L", "N", "M")

# ── 連携語（残差がこの集合と完全一致したときだけ発火）─────────────────────────
#
# 動詞の活用は接辞剥がし（``_SUFFIXES``）で吸収しきれないので、実際に使われる形を
# そのまま並べる（「つないで」→ ×「つな」＋「いで」）。
_CORE_TERMS: Final[frozenset[str]] = frozenset(
    {
        # 日本語・名詞
        "連携",
        "再連携",
        "連動",
        "接続",
        "再接続",
        "コネクト",
        "認証",
        "認可",
        # 日本語・動詞（活用形をそのまま列挙）
        "つなぐ",
        "繋ぐ",
        "つないで",
        "繋いで",
        "つなげる",
        "繋げる",
        "つなげて",
        "繋げて",
        "つなぎ",
        "繋ぎ",
        # 英語
        "connect",
        "reconnect",
        "authorize",
        "authorise",
        "authorization",
        "authorisation",
    }
)

# 強い連携語＝「これ単体で依頼になりうる」語。資料検索の素キーワードとしては滅多に打たれない。
_STRONG_TERMS: Final[frozenset[str]] = frozenset(
    {
        "連携",
        "再連携",
        "つなぐ",
        "繋ぐ",
        "つないで",
        "繋いで",
        "つなげる",
        "繋げる",
        "つなげて",
        "繋げて",
        "connect",
        "reconnect",
    }
)

# 弱い連携語＝**資料検索の素キーワードとしても普通に打たれる**語（社名「コネクト」を含む）。
# これらは依頼語尾（_REQUEST_MARKERS）を伴うときだけ連携依頼と見なす。
_WEAK_TERMS: Final[frozenset[str]] = _CORE_TERMS - _STRONG_TERMS

# ── 前置き（外側から削る）────────────────────────────────────────────────────
# サービス名・所有格・丁寧語の枕。**削った結果が連携語ちょうどのときだけ**発火するので、
# ここを広げても「連携事例を検索して」のような文には効かない。
_PREFIXES: Final[tuple[str, ...]] = (
    # 自分の名前（@ 付きは正規化で落ちるが、素の「Aico 連携」も拾えるようにする）
    "aico",
    "エイコ",
    "google",
    "グーグル",
    "gmail",
    "ジーメール",
    "メール",
    "mail",
    "slack",
    "スラック",
    "カレンダー",
    "calendar",
    "アカウント",
    "account",
    "ワークスペース",
    "workspace",
    "自分の",
    "私の",
    "僕の",
    "俺の",
    "すみません",
    "すいません",
    "ちょっと",
    "そろそろ",
    "もう一度",
    "もう一回",
    "改めて",
    "再度",
    "ぜひ",
    "please",
    "canyou",
    "couldyou",
    "iwantto",
    "wantto",
    "letme",
    "の",
    "を",
    "と",
    "は",
    "が",
)

# ── 後置き（外側から削る）────────────────────────────────────────────────────
# 依頼語尾。**「して」は入れるが「て」は入れない**（「まとめて」「教えて」を
# 連携語まで削り込ませないため＝主辞判定を語尾で薄めない）。
_SUFFIXES: Final[tuple[str, ...]] = (
    "していただけますか",
    "していただきたい",
    "してもらえますか",
    "しなおしたいです",
    "してくださいませ",
    "おねがいいたします",
    "お願いいたします",
    "よろしくお願いします",
    "したいのですが",
    "したいんですが",
    "してくださいます",
    "してほしいです",
    "して欲しいです",
    "してください",
    "して下さい",
    "してほしい",
    "して欲しい",
    "しなおしたい",
    "し直したい",
    "やり直したい",
    "できますか",
    "お願いします",
    "おねがいします",
    "お願いしたい",
    "お願い",
    "おねがい",
    "したいです",
    "したい",
    "しないと",
    "しますね",
    "します",
    "するには",
    "する",
    "して",
    "せよ",
    "ください",
    "下さい",
    "くれる",
    "ほしい",
    "欲しい",
    "たいです",
    "たい",
    "です",
    "ます",
    "リンク",
    "url",
    "please",
    "pls",
    "me",
    "now",
    "の",
    "を",
    "は",
    "が",
    "に",
    "へ",
    "と",
    "ね",
    "よ",
    "な",
)

# 依頼マーカー＝「〜して」「〜したい」「〜お願い」「リンク」等、**依頼・要求**を表す語尾。
# 助詞・コピュラ（の/を/は/が/に/へ/と/ね/よ/な/です/ます）は **含めない**——
# 含めると「連携とは」「認証を」のような話題提示まで依頼に化ける。
_REQUEST_MARKERS: Final[frozenset[str]] = frozenset(_SUFFIXES) - frozenset(
    {"の", "を", "は", "が", "に", "へ", "と", "ね", "よ", "な", "です", "ます"}
)

# 「短い依頼文で連携語が主辞」を長さでも宣言する（正規化後＝記号・空白を除いた文字数）。
# 実際の誤爆防止は残差の完全一致が担うが、設計意図を機械で固定しておく。
_MAX_NORMALIZED_LEN: Final[int] = 32

# 判定理由（ログ用の決定論コード。**本文・顧客名は決して含めない**）。
REASON_EMPTY: Final[str] = "empty"
REASON_TOO_LONG: Final[str] = "too_long"
REASON_NO_MATCH: Final[str] = "no_core_residual"
REASON_CORE_ONLY: Final[str] = "core_only"
REASON_AFFIX_STRIPPED: Final[str] = "affix_stripped"

# 自由文が載りうる引数名。ここに無いキー（``client_name`` 等）は見ない
# ——「コネクト」「連動」という実在社名を連携依頼に化けさせないため。
FREE_TEXT_FIELDS: Final[tuple[str, ...]] = ("query", "goal", "text", "message", "prompt")


@dataclass(frozen=True)
class ConnectIntent:
    """連携依頼かどうかの判定結果（純粋な値）。

    Attributes:
        matched: 決定論分岐を発火してよいか。
        reason: 判定理由の決定論コード（ログ用。入力本文は含まない）。
        field: 一致した引数名（引数からの判定時のみ。本文は含まない）。
    """

    matched: bool
    reason: str
    field: str | None = None


def normalize_connect_text(raw: str | None) -> str:
    """NFKC → メンション/URL 除去 → 記号・絵文字・空白除去 → casefold。

    残すのは Unicode カテゴリ L/N/M だけなので、絵文字（So）・約物（P*）・空白（Z*）は
    まとめて落ちる。日本語は分かち書きしないため空白は復元せず詰める。
    """
    if not raw:
        return ""
    s = unicodedata.normalize("NFKC", str(raw))
    s = _SLACK_REF_RE.sub(" ", s)
    s = _URL_RE.sub(" ", s)
    s = _AT_TOKEN_RE.sub(" ", s)
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] in _KEEP_CATEGORIES)
    return s.casefold()


def _strip_once(text: str, affixes: tuple[str, ...], *, prefix: bool) -> str:
    """最長一致の接辞を 1 つだけ削る（全部消える削り方はしない）。"""
    for affix in sorted(affixes, key=len, reverse=True):
        if len(text) <= len(affix):
            continue
        if prefix and text.startswith(affix):
            return text[len(affix) :]
        if not prefix and text.endswith(affix):
            return text[: -len(affix)]
    return text


def _strip_with_flags(normalized: str) -> tuple[str, bool, bool]:
    """残差と「後置きを削ったか」「依頼マーカーを削ったか」を返す。"""
    text = normalized
    previous = ""
    suffix_stripped = False
    marker_stripped = False
    while text and text != previous:
        previous = text
        text = _strip_once(text, _PREFIXES, prefix=True)
        before = text
        text = _strip_once(text, _SUFFIXES, prefix=False)
        if text != before:
            suffix_stripped = True
            if before[len(text) :] in _REQUEST_MARKERS:
                marker_stripped = True
    return text, suffix_stripped, marker_stripped


def strip_connect_affixes(normalized: str) -> str:
    """正規化文字列から前置き・後置きを外側から繰り返し削り、残差を返す。"""
    return _strip_with_flags(normalized)[0]


def detect_connect_intent(raw: str | None) -> ConnectIntent:
    """本文が「連携してほしい」という依頼そのものかを判定する純粋関数。

    発火するのは **短い依頼文で連携語が主辞**のときだけ。長文・修飾された文
    （「〇〇社との連携について提案書を」）では発火しない。さらに弱い連携語
    （「認証」「連動」「コネクト」等＝資料検索の素キーワードにもなる語）は、
    依頼マーカーを伴うときだけ発火する（モジュール docstring の判定式を参照）。
    """
    normalized = normalize_connect_text(raw)
    if not normalized:
        return ConnectIntent(matched=False, reason=REASON_EMPTY)
    if len(normalized) > _MAX_NORMALIZED_LEN:
        return ConnectIntent(matched=False, reason=REASON_TOO_LONG)
    if normalized in _STRONG_TERMS:
        return ConnectIntent(matched=True, reason=REASON_CORE_ONLY)
    residual, suffix_stripped, marker_stripped = _strip_with_flags(normalized)
    if residual not in _CORE_TERMS:
        return ConnectIntent(matched=False, reason=REASON_NO_MATCH)
    # 依頼マーカー付きなら強弱を問わず依頼。マーカーが無い場合は、強い連携語が
    # 前置きだけを伴って現れたとき（「Google連携」）に限る。弱い連携語の素出し
    # （「コネクト」「メール認証」「連動」）と助詞だけの話題提示（「連携とは」）は落とす。
    if marker_stripped:
        return ConnectIntent(matched=True, reason=REASON_AFFIX_STRIPPED)
    if residual in _STRONG_TERMS and not suffix_stripped:
        return ConnectIntent(matched=True, reason=REASON_AFFIX_STRIPPED)
    return ConnectIntent(matched=False, reason=REASON_NO_MATCH)


def detect_connect_intent_in_args(skill_args: dict[str, object]) -> ConnectIntent:
    """tool 引数の自由文フィールドだけを見て連携依頼かを判定する。

    ``client_name`` のような固有名詞欄は **見ない**（「コネクト」という社名を
    連携依頼に化けさせない）。最初に一致したフィールド名を :attr:`ConnectIntent.field`
    に載せる（値そのものは載せない＝ログへ本文を出さないため）。
    """
    for field in FREE_TEXT_FIELDS:
        value = skill_args.get(field)
        if not isinstance(value, str):
            continue
        intent = detect_connect_intent(value)
        if intent.matched:
            return ConnectIntent(matched=True, reason=intent.reason, field=field)
    return ConnectIntent(matched=False, reason=REASON_NO_MATCH)


__all__ = [
    "FREE_TEXT_FIELDS",
    "REASON_AFFIX_STRIPPED",
    "REASON_CORE_ONLY",
    "REASON_EMPTY",
    "REASON_NO_MATCH",
    "REASON_TOO_LONG",
    "ConnectIntent",
    "detect_connect_intent",
    "detect_connect_intent_in_args",
    "normalize_connect_text",
    "strip_connect_affixes",
]
