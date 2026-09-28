"""判定の入力（BriefSignals）を作る **唯一の変換点**。

死守ライン: 生 ``description`` を読むのはこのモジュールだけ。schema にも出力にも
Scheduler 入力にもログにも生 description は出さない。ここから先へ渡るのは
「クライアント行があったか」「そこから読めた企業名/代理店」の派生値だけ。

なぜ 1 本に絞るか: 定期便（runner）と on-demand（tool）が別々の抽出器を持つと、
同じ予定が経路によって違う判定になる。「朝の DM には出たのに聞き直すと出ない」は
利用者から見て機能が壊れているのと同じなので、構造で止める。

2 経路の接続:
  - tool 経路   : ``build_signal_input(CalendarEventDetail)`` ← 生 items から直接
  - 定期便経路  : morning_digest が同じ ``build_signal_input`` を通して
                 ``CalendarEventItem`` へ **派生値だけ** 写し、``signals_from_item``
                 が単純な field コピーで復元する（再計算しない）

``signals_from_item`` にロジックを持たせないこと。持たせた瞬間に 2 経路が分岐する。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

# 説明欄の「クライアント行」。全角/半角コロン・「得意先」表記ゆれを吸収する。
_CLIENT_LINE_RE = re.compile(r"^[\s　]*(?:クライアント|得意先|顧客|CL)[\s　]*[:：](?P<body>.*)$")
# 「代理店：青葉広告（山田様）」「代理店: 桜通エージェンシー」。
_AGENCY_LINE_RE = re.compile(r"^[\s　]*(?:代理店|代理店名|AG)[\s　]*[:：](?P<body>.*)$")

# クライアント行の中に「／代理店：…」が同居する書き方（9/11 実物のテスト送信で確認済み）。
_INLINE_AGENCY_RE = re.compile(r"[／/]\s*(?:代理店|AG)\s*[:：]\s*(?P<agency>.+)$")

# 複数社の連記に使われる区切り。
# ⚠️ 「・」「･」は **入れない**。中黒は社名そのものに出る（ingest 側の
# ``derive_knowledge_client_name``（adapters/form_mappings.py）も同じ理由で中黒を
# 分割しない）。ここだけ流儀を変えると、中黒入りの社名が 2 社に割れたまま SQL の
# client_name / ILIKE パラメタと表示用社名に流れる。DELTA §3 の「・」は **表示側の
# 連記記号** であって入力側の区切りではない。
_CLIENT_SPLIT_CHARS = "／/,、"

#: 社名トークンの打ち切り位置。ここから後ろは「注記・自由文」とみなして捨てる。
#: 予定の説明欄は社外の主催者（代理店/クライアント）が書ける **第三者入力** なので、
#: 「クライアント：A社 ※値引き条件は社外秘。資料 https://…」の後半が丸ごと
#: agency_hint に入ると、本人 DM に任意テキストと任意クリック先が差し込める。
_NAME_CUT_RE = re.compile(r"[\s　※＊*。｡｜|]")

#: 「ホスト名らしさ」の判定。``http`` / ``://`` だけを見ていた頃は
#: ``代理店：evil.example.com/steal-this-token`` が素通りし、Slack の自動リンク化で
#: **第三者が書いた任意のクリック先が本人 DM に出ていた**（harden は < > @ & しか潰さない）。
#:
#: 形: 「ASCII 英数字 or ハイフン」＋「.」＋「ASCII 英字 2 文字以上」＋「ラベル文字でない何か」。
#: 末尾を ``[/:?#]|$`` ではなく否定先読みにしているのは ``evil.example.com)`` ``a@b.com,``
#: のように閉じ括弧・読点が付いた形も止めるため。
#:
#: 実測した線引き（tests/skills/pre_meeting_brief/test_classify.py）:
#:   捨てる: evil.example.com/x / bit.ly/xYz9 / drive.google.com / www.a.jp / evil.example.com)
#:   通す  : 株式会社A.B.C（TLD 側が 1 文字）/ ドコモ.com（ドット直前が非 ASCII）/
#:           Co.,Ltd. / No.1 / 中央・製紙 / 白水飲料(飲料/健康)
#: 巻き込む社名（例: ``P.A.Works`` ``Sony.Inc``）は残るが、**通すより捨てる方が安全** で、
#: 日本語社名・中黒・括弧注記・1 文字区切りの英字略称は上のとおり生き残る。
_HOSTLIKE_RE = re.compile(r"[A-Za-z0-9-]\.[A-Za-z]{2,}(?![A-Za-z0-9.\-])")

#: ホスト名 **＋そのパス** を 1 かたまりとして消すための形。
#:
#: なぜ ``_HOSTLIKE_RE`` だけでは足りないか（2026-09-11 実測）: クライアント行は
#: ``_split_top_level`` が ``／`` ``/`` ``,`` ``、`` で切る。``クライアント：bit.ly/xYz9``
#: は **切ってから** ``tighten_name`` に渡るので、``bit.ly`` は捨てられるのに
#: 残った ``xYz9`` が社名として本人 DM に出る（``クライアント：xYz9``）。
#: リンクにはならないが、第三者の文字列が社名欄に居座る。
#:
#: 左は **ホスト名ラベル文字だけ** 伸ばす（``A社／evil.example.com/x`` の ``A社`` を
#: 巻き込まない）。右は ``/`` ``:`` ``?`` ``#`` で始まるパスだけを、区切り・注記記号に
#: 当たるまで伸ばす。結果、区切りで切る **前** に URL だけがきれいに消える。
_URLISH_RE = re.compile(
    r"[A-Za-z0-9.\-]*[A-Za-z0-9-]\.[A-Za-z]{2,}(?![A-Za-z0-9.\-])"
    r"(?:[/:?#][^\s　／、｜|※＊*。｡]*)?"
)

#: ``scheme://…`` はホスト名らしさに関係なく丸ごと消す（``_URLISH_RE`` より先に掛ける）。
#: 残すと ``//`` が ``_split_top_level`` の ``/`` 区切りに食われ、``https:`` と後続の語が
#: 別々の「社名」として出てくる（実測: ``split_clients`` が ``['北都リゾート', '参照']``）。
_SCHEME_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s　※＊*。｡｜|]*")

#: P4（参加者ドメインを取引先名の代わりに置く経路）専用の形式検査。
#: ``tighten_name`` はホスト名らしい文字列を丸ごと捨てるので、そのまま通すと P4 が
#: 永久に空になる。ここは **Google が返した参加者アドレスのドメイン部** だけが入る
#: 経路で、パス・クエリ・自由文が混ざる余地が無い（混ざっていたら捨てる）。
_PLAIN_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")

_MAX_CLIENTS = 2  # 名寄せ・引き当ての対象にする最大社数（表示は全社）
_MAX_NAME = 40  # 社名・代理店名 1 トークンの上限
_MAX_TEXT = 200  # 派生値の字数上限（description 全文を持ち回らせない）


def normalize_text(raw: str | None) -> str:
    """NFKC 正規化＋制御文字/改行除去。判定・抽出の共通前処理。

    NFKC で【社外】の全角・半角、コロンの全半角、㈱などの表記ゆれを吸収する。
    """
    if not raw:
        return ""
    text = unicodedata.normalize("NFKC", str(raw))
    # 改行はタブ/スペースへ（行構造は _lines 側で扱う）。制御文字は落とす。
    return "".join(ch for ch in text if ch == "\n" or (ch >= " " and ch != "\x7f"))


@dataclass(frozen=True)
class BriefSignals:
    """社外判定・企業名抽出に必要な材料（生 description を **含まない**）。

    ``attendee_list_available=False`` は「社外参加者ゼロ」ではなく「参加者リストが
    見えていない」。この 2 つを混同すると、ゲストリスト非表示の商談が毎回 internal に
    落ちて消える（Google は非表示時に空配列ではなく本人＋主催者を返す）。
    """

    title: str = ""
    start_at: str | None = None
    end_at: str | None = None
    all_day: bool = False
    has_client_line: bool = False
    client_hint: str = ""  # クライアント行 / タイトルから読めた企業名（連記のまま）
    agency_hint: str = ""  # 「青葉広告（山田様）」等の表示用（担当者名まで含む）
    attendee_domains: tuple[str, ...] = field(default_factory=tuple)
    attendee_list_available: bool = False


def _lines(text: str) -> list[str]:
    return [ln for ln in text.split("\n") if ln.strip()]


def tighten_name(raw: str) -> str:
    """自由文の断片 → **社名 1 トークン**（抽出直後に必ず通す）。

    死守ラインの実装: 生 ``description`` から捕った文字列は、この関数を通してから
    しか外へ出さない。表示の直前で絞るのでは遅い（schema・ログ・Scheduler 入力に
    自由文が載ってから消すことになる）。

    - 空白（半角/全角）・``※`` ``＊`` ``。`` ``｜`` 以降は注記とみなして捨てる
    - ``http`` / ``://`` を含むもの、および **スキーム無しでもホスト名に見えるもの**
      （``_HOSTLIKE_RE``: ``evil.example.com/x`` ``bit.ly/xYz9`` ``drive.google.com``）は
      **丸ごと破棄**。裸 URL もホスト名も Slack が自動リンク化するため 1 文字も通さない
      （``harden`` は ``<`` ``>`` ``@`` ``&`` しか潰さない＝無害化の当てにならない）
    - 40 字上限

    ⚠️ 参加者ドメインを取引先名の代わりに置く P4 経路は、この関数ではなく
    ``domain_label`` を使う（ここを緩めて P4 を通そうとすると、説明欄の自由文にも
    同じ緩和が効いてしまう）。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    cut = _NAME_CUT_RE.search(text)
    if cut:
        text = text[: cut.start()]
    text = text.strip()
    lowered = text.lower()
    if "http" in lowered or "://" in lowered:
        return ""
    if _HOSTLIKE_RE.search(text):
        return ""
    return text[:_MAX_NAME]


def domain_label(raw: str) -> str:
    """参加者ドメイン（P4）→ 表示してよい **素のドメイン** 。形が崩れていれば空文字。

    ``tighten_name`` は ``aoba-ad.co.jp`` のようなホスト名を丸ごと捨てるため、P4 が
    そのまま使うと取引先名が永久に空になる。ここは Google の参加者リスト由来の
    ドメイン部だけが通る経路なので、**パス・クエリ・記号・自由文が 1 文字でも混ざれば
    捨てる** 形式検査に限定する（自由文を通す穴にしない）。
    """
    text = normalize_text(raw).strip().lower()
    if not text or len(text) > 60:
        return ""
    return text if _PLAIN_DOMAIN_RE.match(text) else ""


def _split_top_level(text: str) -> list[str]:
    """区切り文字で切る。ただし **括弧の中では切らない**。

    「白水飲料（飲料/健康）」のように括弧注記へ ``/`` が入る書き方があり、素朴に
    split すると「白水飲料(飲料」「健康)」という壊れた社名が SQL と表示へ流れる。
    """
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in text:
        if ch in "（(［[【":
            depth += 1
        elif ch in "）)］]】":
            depth = max(0, depth - 1)
        if depth == 0 and ch in _CLIENT_SPLIT_CHARS:
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    out.append("".join(buf))
    return out


def _from_description(description: str) -> tuple[bool, str, str]:
    """説明欄から (クライアント行の有無, 企業名連記, 代理店表示) を読む。"""
    text = normalize_text(description)
    if not text:
        return (False, "", "")
    has_line = False
    client = ""
    agency = ""
    for line in _lines(text):
        m = _CLIENT_LINE_RE.match(line)
        if m and not client:
            has_line = True
            body = m.group("body").strip()
            inline = _INLINE_AGENCY_RE.search(body)
            if inline:
                agency = agency or tighten_name(inline.group("agency"))
                body = body[: inline.start()].strip()
            # ⚠️ 抽出 **直後** に社名トークンへ絞る（schema にも載らなくなる）。
            client = "／".join(split_clients(body))
            continue
        a = _AGENCY_LINE_RE.match(line)
        if a and not agency:
            agency = tighten_name(a.group("body"))
    return (has_line, client[:_MAX_TEXT], agency[:_MAX_NAME])


def build_signal_input(event: Any) -> BriefSignals:
    """``CalendarEventDetail``（または同形の object）→ BriefSignals。

    ⚠️ ここが生 description に触れる唯一の場所。戻り値に description は載らない。
    """
    description = str(getattr(event, "description", "") or "")
    has_client_line, client_hint, agency_hint = _from_description(description)
    domains = tuple(str(d) for d in (getattr(event, "attendee_domains", ()) or ()))
    return BriefSignals(
        title=normalize_text(str(getattr(event, "summary", "") or ""))[:_MAX_TEXT],
        start_at=str(getattr(event, "start", "") or "") or None,
        end_at=str(getattr(event, "end", "") or "") or None,
        all_day=bool(getattr(event, "all_day", False)),
        has_client_line=has_client_line,
        client_hint=client_hint,
        agency_hint=agency_hint,
        attendee_domains=domains[:10],
        attendee_list_available=bool(getattr(event, "attendee_list_available", False)),
    )


def _title_signal(item: Any) -> str:
    """``CalendarEventItem`` から判定用の予定名を **写す**（再計算しない）。"""
    signal = str(getattr(item, "title_signal", "") or "")
    if signal:
        return signal[:_MAX_TEXT]
    raw = str(getattr(item, "summary_display", "") or getattr(item, "summary_scrubbed", "") or "")
    return normalize_text(raw)[:_MAX_TEXT]


def signals_from_item(item: Any) -> BriefSignals:
    """``CalendarEventItem``（定期便の写し）→ BriefSignals。**field コピーのみ**。

    ⚠️ ここで再計算・再抽出をしないこと。ここに 1 行でもロジックが入ると
    「定期便と on-demand で結果が違う」が再発する（round-trip テストが赤になる）。
    """
    return BriefSignals(
        # ⚠️ 第一候補は **判定用に写された派生値**（title_signal）。display から作り直すと
        #   切り位置（生 120 字）と NFKC の順序が build_signal_input（NFKC → 200 字）と
        #   食い違い、121 字目以降に除外語や「様」がある予定の判定が経路で割れる。
        #   display へのフォールバックは title_signal を持たない古い/手組みの item 用で、
        #   ここが使われると round-trip テストが赤になる（＝写し忘れの検知口）。
        title=_title_signal(item),
        start_at=getattr(item, "start_at", None),
        end_at=getattr(item, "end_at", None),
        all_day=bool(getattr(item, "all_day", False)),
        has_client_line=bool(getattr(item, "has_client_line", False)),
        client_hint=str(getattr(item, "client_hint_display", "") or ""),
        agency_hint=str(getattr(item, "agency_display", "") or ""),
        attendee_domains=tuple(str(d) for d in (getattr(item, "attendee_domains", ()) or ())),
        attendee_list_available=bool(getattr(item, "attendee_list_available", False)),
    )


def split_clients(raw: str) -> list[str]:
    """連記された企業名を最大 2 社まで切り出す（各社は ``tighten_name`` 済み）。

    冪等: ``split_clients("／".join(split_clients(x))) == split_clients(x)``。
    ``_from_description`` が結果を「／」で連結して持ち回るので、ここが冪等でないと
    経路によって社名が変わる。

    ⚠️ URL は **区切りで切る前** に消す。``/`` は社名の連記区切りでもあり URL の
    パス区切りでもあるので、先に切ると ``bit.ly/xYz9`` が ``bit.ly``（捨てられる）と
    ``xYz9``（社名として残る）に割れ、第三者の文字列が社名欄に出る。
    """
    if not raw:
        return []
    text = _URLISH_RE.sub(" ", _SCHEME_URL_RE.sub(" ", normalize_text(raw)))
    out: list[str] = []
    for part in _split_top_level(text):
        name = tighten_name(part)
        if name and name not in out:
            out.append(name)
    return out[:_MAX_CLIENTS]


__all__ = [
    "BriefSignals",
    "build_signal_input",
    "domain_label",
    "normalize_text",
    "signals_from_item",
    "split_clients",
    "tighten_name",
]
