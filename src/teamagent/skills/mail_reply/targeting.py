"""返信先スレッドの一意化（純粋関数のみ・Gmail/LLM 非依存）。

## なぜ要るか（2026-09-07 本番実測）

利用者「返信下書きを作って」→ Aico「✅ 日本教育財団の PR 関連メール（石川さん・クオラス経由）
への返信下書きを保存」→ 利用者「**それじゃない！【日本教育財団様_PR関連のご提案について】
ベクトル徳野**」。同じ話題のスレッドが複数あり、従来の ``mail_reply`` は
``"<client>" newer_than:30d`` で拾った **最新の 1 通** に無条件で下書きを作っていた。

## 方針（利用者に選ばせるのは最終手段）

1. 依頼文に件名【…】・差出人・日付の手がかりがあれば、それで候補を **1 件に絞る**。
   Gmail の検索演算子（``subject:`` / ``from:`` / ``after:``）は再現率のための粗い絞りに使い、
   最終判定は本モジュールの **ローカル照合**（正規化した部分一致）で行う。
   Gmail の CJK 分かち書きは信用できないので、演算子で 0 件なら演算子なしで引き直す。
2. 絞っても 2 件以上残れば **下書きを作らず** 候補（件名・差出人・日時・冒頭 1 行）を最大
   3 件返す。手がかりが無く 1 件だけなら従来どおり作る。
3. 候補の描画は決定論（要約しない・順序を変えない）。ログには件数と手がかりの有無だけ。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Final

from teamagent.observability import scrub_value
from teamagent.skills._shared.client_name_guard import to_gmail_phrase
from teamagent.skills.mail_reply.schema import ReplyThreadCandidate

JST: Final = _dt.timezone(_dt.timedelta(hours=9))

#: 曖昧なときに見せる候補の上限（多く見せても選べない・ephemeral の縦幅にも収める）。
MAX_CANDIDATES: Final[int] = 3

SUBJECT_DISPLAY_MAX: Final[int] = 60
SENDER_DISPLAY_MAX: Final[int] = 40
PREVIEW_DISPLAY_MAX: Final[int] = 60

# 照合前に落とす文字（括弧・区切り・空白・引用符）。件名の【】や「_」の有無で取りこぼさない。
_MATCH_DROP_RE: Final[re.Pattern[str]] = re.compile(
    r"[\s\"'`\\\[\]【】（）()「」『』<>＜＞〈〉《》_＿・,、.。!！?？:：;；/／|｜\-ー―–—~〜]+"
)
# 差出人ヒントから拾う「名前らしい断片」: 漢字 2 文字以上の連続 / 英数 3 文字以上の連続。
# 「ベクトル徳野」（会社＋姓・区切り無し）を From「徳野 太郎 <tokuno@…>」へ当てるため。
_KANJI_RUN_RE: Final[re.Pattern[str]] = re.compile(r"[一-鿿㐀-䶿]{2,}")
_ASCII_RUN_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]{3,}")
_DATE_RE: Final[re.Pattern[str]] = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
# Gmail クエリの被演算子から落とす文字。フレーズを閉じる `"` と `\` は**消す**（語を割らない）、
# 括弧類は空白に置く（【…】の中身を独立した語として渡す）。
_OPERAND_QUOTE_RE: Final[re.Pattern[str]] = re.compile(r"[\"\\]+")
_OPERAND_BRACKET_RE: Final[re.Pattern[str]] = re.compile(r"[\[\]【】（）()「」『』<>＜＞]+")


# ── 正規化・照合 ───────────────────────────────────────────────────────────


def normalize_for_match(text: str | None) -> str:
    """NFKC → 小文字 → 括弧/区切り/空白の除去。照合専用（表示には使わない）。"""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", str(text)).lower()
    return _MATCH_DROP_RE.sub("", s)


def has_hint(text: str | None) -> bool:
    """正規化して何か残るか（空白や括弧だけの値を「手がかりあり」と数えない）。"""
    return bool(normalize_for_match(text))


def subject_matches(hint: str, subject: str) -> bool:
    """件名ヒントが件名に（正規化後の部分一致で）含まれるか。ヒントが空なら False。"""
    key = normalize_for_match(hint)
    if not key:
        return False
    return key in normalize_for_match(subject)


def sender_matches(hint: str, from_header: str) -> bool:
    """差出人ヒントが From ヘッダに当たるか。

    1. 正規化した全体が From に含まれれば真（「徳野」「tokuno@」「石川 花子」）。
    2. 全体で当たらなければ、ヒント内の **漢字 2 文字以上の連続** または **英数 3 文字以上の
       連続** のいずれかが含まれれば真（「ベクトル徳野」→「徳野」）。
       会社名（カタカナ）だけでは当てない＝差出人の実体で照合する。
    """
    key = normalize_for_match(hint)
    if not key:
        return False
    haystack = normalize_for_match(from_header)
    if not haystack:
        return False
    if key in haystack:
        return True
    tokens = [*_KANJI_RUN_RE.findall(key), *_ASCII_RUN_RE.findall(key)]
    return any(tok in haystack for tok in tokens)


def parse_received_after_ms(value: str | None) -> int | None:
    """``YYYY-MM-DD`` を JST 0 時の epoch ミリ秒へ。不正・空は None（＝絞らない）。"""
    if not value:
        return None
    m = _DATE_RE.match(value.strip())
    if not m:
        return None
    try:
        day = _dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=JST)
    except ValueError:
        return None
    return int(day.timestamp() * 1000)


def gmail_after_clause(value: str | None) -> str:
    """``YYYY-MM-DD`` → ``after:YYYY/MM/DD``。不正・空は空文字。"""
    if not value:
        return ""
    m = _DATE_RE.match(value.strip())
    if not m or parse_received_after_ms(value) is None:
        return ""
    return f"after:{m.group(1)}/{m.group(2)}/{m.group(3)}"


def gmail_operand(text: str) -> str:
    """検索演算子の被演算子（``subject:"…"`` の中身）用に整える。空なら空文字。"""
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = _OPERAND_QUOTE_RE.sub("", s)
    s = _OPERAND_BRACKET_RE.sub(" ", s)
    return " ".join(s.split())


def build_search_query(
    *,
    client_phrase: str,
    subject_hint: str,
    from_hint: str,
    received_after: str,
    lookback_days: int,
    with_hint_operators: bool,
) -> str:
    """Gmail 検索クエリを組む。

    ``client_phrase`` は client_name_guard 検査済みキーワード（生の client_name を渡さない）。
    ``with_hint_operators=False`` は「演算子で 0 件だった」ときの引き直し用で、ヒントは
    ローカル照合に委ねる（``after:`` は日付なので常に付ける）。
    """
    parts: list[str] = []
    if client_phrase:
        parts.append(to_gmail_phrase(client_phrase))
    if with_hint_operators:
        frm = gmail_operand(from_hint)
        if frm:
            parts.append("from:" + to_gmail_phrase(frm))
        subj = gmail_operand(subject_hint)
        if subj:
            parts.append("subject:" + to_gmail_phrase(subj))
    after = gmail_after_clause(received_after)
    if after:
        parts.append(after)
    parts.append(f"newer_than:{lookback_days}d -in:sent in:inbox")
    return " ".join(parts)


# ── 候補（メタデータのみ・本文は Gmail の抜粋を 1 行だけ）─────────────────────


@dataclass(frozen=True)
class ThreadCandidateMeta:
    """候補スレッドの「最新の受信 1 通」のメタデータ。"""

    thread_id: str
    message_id: str
    subject: str = ""
    from_header: str = ""
    received_at_ms: int | None = None
    snippet: str = ""


def group_newest_per_thread(metas: Iterable[ThreadCandidateMeta]) -> list[ThreadCandidateMeta]:
    """newest-first の並びを保ったまま、スレッドごとに最初（＝最新）の 1 通だけ残す。"""
    seen: set[str] = set()
    out: list[ThreadCandidateMeta] = []
    for meta in metas:
        key = meta.thread_id or f"msg:{meta.message_id}"
        if key in seen:
            continue
        seen.add(key)
        out.append(meta)
    return out


def filter_by_hints(
    cands: Sequence[ThreadCandidateMeta],
    *,
    subject_hint: str = "",
    from_hint: str = "",
    received_after: str = "",
) -> list[ThreadCandidateMeta]:
    """手がかりを **全部** 満たす候補だけ残す（AND）。手がかりが無い項目は素通し。

    受信日時が不明（None）な候補は日付では落とさない（Gmail 側の ``after:`` が既に効いている）。
    """
    use_subject = has_hint(subject_hint)
    use_from = has_hint(from_hint)
    after_ms = parse_received_after_ms(received_after)
    out: list[ThreadCandidateMeta] = []
    for c in cands:
        if use_subject and not subject_matches(subject_hint, c.subject):
            continue
        if use_from and not sender_matches(from_hint, c.from_header):
            continue
        if after_ms is not None and c.received_at_ms is not None and c.received_at_ms < after_ms:
            continue
        out.append(c)
    return out


# ── 表示（決定論・要約なし）────────────────────────────────────────────────


def _clip(text: str, limit: int) -> str:
    s = (text or "").strip()
    return s if len(s) <= limit else s[:limit] + "…"


def sender_display(from_header: str) -> str:
    """From ヘッダ → 表示名（無ければアドレス）。本人の取引相手なので to_display と同じ扱い。"""
    name, addr = parseaddr(from_header or "")
    raw = (name or "").strip() or (addr or "").strip() or (from_header or "").strip()
    return _clip(raw, SENDER_DISPLAY_MAX)


def format_received(ms: int | None) -> str:
    if ms is None:
        return ""
    return _dt.datetime.fromtimestamp(ms / 1000, tz=JST).strftime("%m/%d %H:%M")


def first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def to_output_candidates(cands: Sequence[ThreadCandidateMeta]) -> list[ReplyThreadCandidate]:
    """候補を戻り値の形へ（件名・冒頭はマスク＋短縮。生 messageId は載せない）。"""
    out: list[ReplyThreadCandidate] = []
    for i, c in enumerate(cands[:MAX_CANDIDATES], start=1):
        out.append(
            ReplyThreadCandidate(
                number=i,
                thread_id=c.thread_id,
                subject=_clip(str(scrub_value(c.subject or "")), SUBJECT_DISPLAY_MAX),
                from_display=sender_display(c.from_header),
                received_at=format_received(c.received_at_ms),
                preview=_clip(str(scrub_value(first_line(c.snippet))), PREVIEW_DISPLAY_MAX),
            )
        )
    return out


def render_candidates(cands: Sequence[ReplyThreadCandidate], *, head: str, hidden: int) -> str:
    """本人向けの候補一覧文（番号・件名・差出人・日時・冒頭）。"""
    lines = [head]
    for c in cands:
        parts = [f"{c.number}. 「{c.subject or '（件名なし）'}」", c.from_display or "差出人不明"]
        if c.received_at:
            parts.append(c.received_at)
        line = " — ".join(parts)
        if c.preview:
            line += f" — 冒頭: {c.preview}"
        lines.append(line)
    if hidden > 0:
        lines.append(f"（他 {hidden} 件は省略。件名や差出人を教えていただければ絞り込めます）")
    lines.append("番号でお知らせください（件名や差出人の名前でも構いません）。")
    return "\n".join(lines)


def thread_hash(thread_id: str | None) -> str:
    """ログ用のスレッド識別（生 thread_id は出さない）。"""
    tid = str(thread_id or "")
    return hashlib.sha256(tid.encode("utf-8")).hexdigest()[:12] if tid else ""
