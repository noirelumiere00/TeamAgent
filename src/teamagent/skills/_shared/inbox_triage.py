"""受信箱の「返信が止まっている候補」を決定論で選び、Slack 文面へ組む判定層。

ユーザー裁定（2026-08-21）の実装土台。「お客様名を教えてください」と聞き返すのをやめ、
**全体を見て候補を提示し、選ばれた 1 件だけを深掘りする**という流れに変えるための、
**判定と描画だけ**を担う層（I/O 無し・LLM 不使用・時刻は ``now`` 引数で注入）。

死守ライン（裁定 A/B/C をコードの制約として固定する）:
  - **一覧段階で本文を扱わない**。入力 :class:`InboxMailMeta` には本文・snippet を
    入れる場所が無い（＝この層に本文を渡す手段が無い）。Gmail 側は
    ``threads.get(format="metadata")`` のヘッダだけで足りる。
      ⚠️ ``GmailMessage.snippet`` は format='metadata' でも本文抜粋が返る。
        呼び出し側はこれを本データクラスへ詰め替えないこと。
  - **要約しない**。件名は原文のまま出す（長い時に末尾を落とすだけ）。
    差出人も原文のまま。この層に自由文生成は無く、文言は全て本モジュール内の固定文字列。
  - **推測で決めない**。:func:`parse_selection` は曖昧なら ``None`` を返す（＝聞き返す）。

Slack 描画について:
  - 出力は **受信箱の本人に返す** 前提（マスクしない）。他人へ転送する用途には使わない。
  - 件名・差出人は攻撃者が自由に書ける文字列なので、``&`` ``<`` ``>`` を必ず
    エスケープしてから mrkdwn に載せる（``<https://evil|クリック>`` のリンク偽装対策。
    Slack 側で元の文字に戻って表示されるため「原文のまま」は保たれる）。
"""

from __future__ import annotations

import datetime as _dt
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

# ── 時刻 ────────────────────────────────────────────────────────────────────

_JST = _dt.timezone(_dt.timedelta(hours=9))
_MS_PER_DAY = 24 * 60 * 60 * 1000


# ── 入力（メタデータのみ・本文の置き場は無い）────────────────────────────────


@dataclass(frozen=True)
class InboxMailMeta:
    """一覧段階で扱ってよいメール 1 件のメタデータ。**本文は含まない**。

    呼び出し側（スキル層）が Gmail の ``format="metadata"`` レスポンスから詰め替える。
    既存の型に依存しないのは、この層を I/O から完全に切り離してテスト可能にするため。
    """

    thread_id: str
    """スレッド ID。後段（選択された 1 件を深掘りする工程）の入口キー。空なら候補外。"""
    subject: str = ""
    """件名（原文）。要約・整形はしない。"""
    sender_name: str = ""
    """差出人の表示名（From ヘッダの display-name 部分。無ければ空）。"""
    sender_email: str = ""
    """差出人のメールアドレス。"""
    received_at_ms: int | None = None
    """相手から最後に来た日時（epoch ミリ秒＝Gmail の internalDate）。None は不明。"""
    is_sole_recipient: bool = False
    """自分ひとり宛か（To が自分だけ・Cc 無し）。「あなたの番」度合いの最も強い材料。"""
    is_unreplied: bool = True
    """まだ自分が返していないか。False の件は候補にしない。"""
    is_bulk: bool = False
    """一斉配信・自動送信か（呼び出し側が ``should_skip_mail`` 等のヘッダ判定で立てる）。"""


@dataclass(frozen=True)
class TriageCandidate:
    """候補 1 件。:func:`rank_candidates` の出力であり、描画・選択解析の入力。"""

    mail: InboxMailMeta
    idle_days: int
    """相手から最後に来てからの経過日数（``now`` 基準・負にはしない）。"""
    score: int
    """並び順の根拠（下の固定テーブルの合算）。表示はしない（説明可能性・テスト用）。"""
    reasons: tuple[str, ...] = field(default_factory=tuple)
    """加点理由の固定ラベル（``sole_recipient`` / ``request_word`` / ``urgent_word``）。"""


# ── 点数表（**固定**。推測で係数を作らない）──────────────────────────────────

#: 放置日数の加点は 1 日 1 点。ここで頭打ちにする（古すぎる件が上位を占め続けないため）。
IDLE_POINT_CAP = 30
#: 自分ひとり宛（To が自分だけ）＝相手はあなたの返事だけを待っている。
SOLE_RECIPIENT_BONUS = 12
#: 件名に依頼語がある。
REQUEST_WORD_BONUS = 8
#: 件名に催促・期限の語がある（「至急」「リマインド」等）。
URGENT_WORD_BONUS = 6

REASON_SOLE_RECIPIENT = "sole_recipient"
REASON_REQUEST_WORD = "request_word"
REASON_URGENT_WORD = "urgent_word"

#: 既定の提示件数（裁定の文面が「この3件」を前提にしている）。
DEFAULT_LIMIT = 3

#: 件名の表示上限。超えた分は末尾を落として「…」を付ける（**要約はしない**）。
SUBJECT_DISPLAY_MAX = 60
#: 差出人の表示上限。
SENDER_DISPLAY_MAX = 40
#: 差出人が名前もアドレスも読めなかったときの固定文言（架空の名前を作らない）。
SENDER_UNKNOWN = "差出人不明"


# ── 語彙テーブル（NFKC 正規化＋小文字化した件名に対して照合）────────────────

#: 「相手が何かを求めている」件名の目印。
_REQUEST_WORD_RE = re.compile(
    r"(ご確認|確認|お願い|御願い|ご返信|返信|ご回答|回答|ご対応|対応|ご検討|検討"
    r"|ご相談|相談|ご連絡|依頼|お伺い|伺い|教えて|ご意見|要返信|承認|ご承認"
    r"|いかが|どうでしょう|ますか|ませんか|でしょうか|[？?])"
)

#: 催促・期限の目印（「至急」はここ。加点される点は依頼語と同じ扱いでよい）。
_URGENT_WORD_RE = re.compile(
    r"(至急|大至急|急ぎ|緊急|リマインド|再送|再度|催促|期限|締切|締め切り|本日中|明日まで)"
)

#: 差出人アドレス/表示名に出る「返信しても届かない」マーカー。
#: ``mail_compose._NO_REPLY_MARKERS`` と同じ意図だが、この層はヘッダ辞書を受け取らない
#: ため独自に持つ（呼び出し側がヘッダ判定できない経路でも最低限の防波堤を残す）。
_NO_REPLY_MARKERS: tuple[str, ...] = (
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "no_reply",
    "mailer-daemon",
    "postmaster",
)


def _norm(text: str) -> str:
    """照合用の正規化（全角/半角ゆれ・大小文字を吸収）。表示には使わない。"""
    return unicodedata.normalize("NFKC", text or "").lower()


def _to_ms(now: _dt.datetime) -> int:
    """``now`` を epoch ミリ秒へ。naive は JST として解釈する（社内利用前提）。"""
    aware = now if now.tzinfo is not None else now.replace(tzinfo=_JST)
    return int(aware.timestamp() * 1000)


def idle_days_of(received_at_ms: int | None, now_ms: int) -> int:
    """経過日数（切り捨て）。不明・未来日時は 0（``mail_followup._idle_days`` と同一意味論）。"""
    if not received_at_ms:
        return 0
    delta = now_ms - int(received_at_ms)
    if delta < 0:
        return 0
    return delta // _MS_PER_DAY


def looks_bulk(mail: InboxMailMeta) -> bool:
    """一斉配信・自動送信らしさ。呼び出し側のヘッダ判定 OR 差出人のマーカー。"""
    if mail.is_bulk:
        return True
    haystack = _norm(f"{mail.sender_email} {mail.sender_name}")
    return any(marker in haystack for marker in _NO_REPLY_MARKERS)


def _score(mail: InboxMailMeta, idle: int) -> tuple[int, tuple[str, ...]]:
    """点数と加点理由を返す（**本文は見ない**。件名・宛先・日数のみ）。"""
    points = min(idle, IDLE_POINT_CAP)
    reasons: list[str] = []
    if mail.is_sole_recipient:
        points += SOLE_RECIPIENT_BONUS
        reasons.append(REASON_SOLE_RECIPIENT)
    subject = _norm(mail.subject)
    if _REQUEST_WORD_RE.search(subject):
        points += REQUEST_WORD_BONUS
        reasons.append(REASON_REQUEST_WORD)
    if _URGENT_WORD_RE.search(subject):
        points += URGENT_WORD_BONUS
        reasons.append(REASON_URGENT_WORD)
    return (points, tuple(reasons))


def rank_candidates(
    items: Iterable[InboxMailMeta],
    *,
    now: _dt.datetime,
    limit: int = DEFAULT_LIMIT,
) -> tuple[TriageCandidate, ...]:
    """「あなたの返事で止まっている」候補を決定論で選ぶ。

    除外するもの（＝候補にしない）:
      - ``is_unreplied=False``（もう自分が返している）
      - :func:`looks_bulk` が真（メルマガ・自動送信＝返信する相手がいない）
      - ``thread_id`` が空（選ばれても深掘りの入口が無い）

    並び順は ``(score 降順, 放置日数 降順, thread_id 昇順)``。第 3 キーまで固定して
    いるのは、同点の件で呼び出しごとに順番が入れ替わると「1番」の指し先が変わり、
    :func:`parse_selection` の結果が壊れるため。

    本文は一切使わない（そもそも入力に無い）。LLM も呼ばない＝課金 0。
    """
    if limit <= 0:
        return ()
    now_ms = _to_ms(now)
    scored: list[TriageCandidate] = []
    for mail in items:
        if not mail.thread_id.strip():
            continue
        if not mail.is_unreplied:
            continue
        if looks_bulk(mail):
            continue
        idle = idle_days_of(mail.received_at_ms, now_ms)
        points, reasons = _score(mail, idle)
        scored.append(TriageCandidate(mail=mail, idle_days=idle, score=points, reasons=reasons))
    scored.sort(key=lambda c: (-c.score, -c.idle_days, c.mail.thread_id))
    return tuple(scored[:limit])


# ── 描画（Slack mrkdwn・文言は全てこのモジュール内の固定文字列）────────────────

MSG_EMPTY = "返信が止まっているものはありませんでした（直近{window}日・{scanned}件を確認）"
MSG_HEADER = (
    "受信箱を見たところ、返信が止まっているのはこの{count}件でした。"
    "（直近{window}日・{scanned}件を確認）"
)
MSG_FOOTER = (
    "下書きを作りますか？ 番号でお知らせください。"
    "作成時のご指示（トーン・入れたい内容）があれば一緒にどうぞ。"
)
#: 打ち切りの開示。**どちら側を切ったか**まで書く（「上限で切った」だけだと、
#: 切られたのが古い側だと読まれ、放置検出の結果として真逆に受け取られる）。
MSG_TRUNCATED = (
    "※ 件数が多いため、放置が長い（古い）側から上限まで確認しました。新しい側の一部は見ていません。"
)


def _slack_escape(text: str) -> str:
    """Slack mrkdwn の制御文字だけを実体参照へ（表示上は原文のまま）。"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clip(text: str, limit: int) -> str:
    """表示上限で末尾を落とす。**要約はしない**（削るだけ・削ったら「…」を付ける）。"""
    stripped = (text or "").strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit] + "…"


def format_subject(subject: str) -> str:
    """件名の表示文字列。原文を保ち、長いときだけ末尾を落とす。"""
    # 先に切ってからエスケープする（逆順だと `&amp;` の途中で切れて壊れる）。
    return _slack_escape(_clip(subject, SUBJECT_DISPLAY_MAX))


def format_sender(mail: InboxMailMeta) -> str:
    """差出人の表示文字列。表示名 → アドレス → 固定文言 の順で落とす。"""
    raw = (mail.sender_name or "").strip() or (mail.sender_email or "").strip()
    if not raw:
        return SENDER_UNKNOWN
    return _slack_escape(_clip(raw, SENDER_DISPLAY_MAX))


def render_triage_message(
    cands: Sequence[TriageCandidate],
    *,
    scanned: int,
    truncated: bool,
    window_days: int,
) -> str:
    """候補一覧の Slack 文面を組む（決定論・要約なし）。

    Args:
        cands: :func:`rank_candidates` の出力（**表示順のまま** 1 始まりで番号を振る）。
        scanned: 実際に確認したメール件数（母数の正直な開示）。
        truncated: 走査上限に当たって打ち切ったか。真なら末尾に但し書きを付ける。
        window_days: 遡った日数。
    """
    lines: list[str] = []
    if not cands:
        lines.append(MSG_EMPTY.format(window=window_days, scanned=scanned))
        if truncated:
            lines.append(MSG_TRUNCATED)
        return "\n".join(lines)

    lines.append(MSG_HEADER.format(count=len(cands), window=window_days, scanned=scanned))
    for index, cand in enumerate(cands, start=1):
        lines.append(
            f"{index}. {format_sender(cand.mail)}"
            f"「{format_subject(cand.mail.subject)}」"
            f" ・{cand.idle_days}日経過"
        )
    lines.append(MSG_FOOTER)
    if truncated:
        lines.append(MSG_TRUNCATED)
    return "\n".join(lines)


# ── 選択の解析（曖昧なら None ＝聞き返す）────────────────────────────────────

#: 番号の抽出。NFKC 正規化後なので全角数字・丸数字（①→1）も同じ形になる。
_NUMBER_RE = re.compile(r"\d+")

#: 名前照合に使わない一般語（これだけで一致させると誤爆する）。
_NAME_STOPWORDS = frozenset({"の件", "さん", "様", "御中", "株式会社", "有限会社"})

#: 名前照合に使うキーの最小長（1 文字キーは誤爆するので採らない）。
_MIN_NAME_KEY_LEN = 2

#: 件名そのもので照合するときの最小長。
_MIN_SUBJECT_KEY_LEN = 4

#: 表示名・アドレスを語へ割る区切り。
_NAME_SPLIT_RE = re.compile(r"[\s　,、／/｜|・（）\(\)\[\]【】<>＜＞\"'@.]+")


def _name_keys(mail: InboxMailMeta) -> tuple[str, ...]:
    """候補を名前で指すときの手掛かり（差出人名の語・アドレスの局所部/ドメイン名）。"""
    keys: list[str] = []
    for token in _NAME_SPLIT_RE.split(_norm(mail.sender_name)):
        if len(token) >= _MIN_NAME_KEY_LEN and token not in _NAME_STOPWORDS:
            keys.append(token)
    email = _norm(mail.sender_email)
    local, _, domain = email.partition("@")
    if len(local) >= _MIN_NAME_KEY_LEN:
        keys.append(local)
    labels = [lab for lab in domain.split(".") if lab]
    # co / jp / com のような TLD・属性ラベルは手掛かりにならない（誤爆源）。
    for label in labels[:-1] if len(labels) > 1 else labels:
        if len(label) >= _MIN_NAME_KEY_LEN and label not in ("co", "ne", "or", "ac", "www"):
            keys.append(label)
    subject = _norm(mail.subject)
    if len(subject) >= _MIN_SUBJECT_KEY_LEN:
        keys.append(subject)
    # 順序を保ったまま重複排除（テストで並びが安定するように）。
    return tuple(dict.fromkeys(keys))


def mentions_number(text: str) -> bool:
    """返事が **番号で位置を指している**か（``"1番で"`` ``"①"`` ``"1と3"``）。

    番号は「利用者が見た一覧の何行目か」という**位置**でしかないので、その一覧を
    再現できないまま解釈すると別の相手を指す。呼び出し側が「一覧を再現できない場合は
    番号を受け付けない」と判断するための述語（:func:`parse_selection` と同じ正規化を使う）。
    """
    return bool(_NUMBER_RE.search(_norm(text)))


def parse_selection(
    text: str,
    cands: Sequence[TriageCandidate],
) -> tuple[TriageCandidate, ...] | None:
    """利用者の返事から候補を特定する。**曖昧なら ``None``**（推測で決めない）。

    受け付ける形:
      - 番号: ``"1"`` / ``"2番"`` / ``"1と3"`` / ``"1、3"`` / ``"①"``（NFKC で数字化）
      - 名前: ``"電通の件"`` のように差出人名・アドレス・件名の一部を含む文

    ``None`` を返すのは次のいずれか（＝呼び出し側は聞き返す）:
      - 手掛かりが無い / 候補が空
      - 番号が範囲外（``"8/21"`` のような日付混入もここで落ちる）
      - 名前が 2 件以上に当たった

    番号が読めた場合は名前照合を行わない（番号の方が意図として強い）。
    戻り値は **``cands`` の並び順**（利用者が書いた順ではない・重複排除済み）。
    """
    if not cands:
        return None
    normalized = _norm(text)
    if not normalized.strip():
        return None

    numbers = [int(m) for m in _NUMBER_RE.findall(normalized)]
    if numbers:
        picked: set[int] = set()
        for number in numbers:
            if not (1 <= number <= len(cands)):
                return None  # 範囲外が 1 つでもあれば「読み違い」とみなして聞き返す
            picked.add(number - 1)
        return tuple(cands[i] for i in sorted(picked))

    matched = [
        cand for cand in cands if any(key and key in normalized for key in _name_keys(cand.mail))
    ]
    if len(matched) != 1:
        return None  # 0 件＝手掛かり無し / 2 件以上＝曖昧。どちらも聞き返す
    return (matched[0],)


__all__ = [
    "DEFAULT_LIMIT",
    "IDLE_POINT_CAP",
    "MSG_EMPTY",
    "MSG_FOOTER",
    "MSG_HEADER",
    "MSG_TRUNCATED",
    "REASON_REQUEST_WORD",
    "REASON_SOLE_RECIPIENT",
    "REASON_URGENT_WORD",
    "REQUEST_WORD_BONUS",
    "SENDER_DISPLAY_MAX",
    "SENDER_UNKNOWN",
    "SOLE_RECIPIENT_BONUS",
    "SUBJECT_DISPLAY_MAX",
    "URGENT_WORD_BONUS",
    "InboxMailMeta",
    "TriageCandidate",
    "format_sender",
    "format_subject",
    "idle_days_of",
    "looks_bulk",
    "mentions_number",
    "parse_selection",
    "rank_candidates",
    "render_triage_message",
]
