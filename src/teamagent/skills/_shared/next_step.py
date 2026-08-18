"""「次の一手」の提案文をサーバ側で決定論的に作る共通部品。

方針（ユーザー裁定 2026-08-18）:
  - 提案文の生成は **スキル側の決定論コード**で行う。LLM に「気を利かせて提案させる」と、
    出来ないことを約束したり、毎回違う言い回しになったり、勝手に実行したりする。
  - 提案は **実在し現在 ON のツールでできること**に限る（受け皿ツールの env gate を見る）。
  - **1 応答につき最大 1 個**。提案しただけでは何も実行しない（実行は利用者の明示 YES 後）。
  - 利用者の依頼が既に完結しているとき（＝その次の一手を自分で頼んでいるとき）は付けない。

env:
  - ``SUGGEST_NEXT_STEP``: 全体 kill switch（**既定 ON**）。イメージ再ビルドなしで止められる。
  - 受け皿ツールの gate（``USE_CALENDAR_EVENT_TOOL`` / ``USE_KNOWLEDGE_DELIVER``）は
    ``infra/openclaw/effective-tool-scope.json`` の ``enabledBy`` と同じ env 名を見る。
"""

from __future__ import annotations

import os
import re

# ── 提案文（固定文言・ここでしか作らない）──────────────────────────────────────

# slack_summary → calendar_event(freeform) が受け皿。
CALENDAR_SUGGESTION = "📅 この予定をカレンダーに追加しますか？（「追加して」で登録します）"
# search → knowledge_deliver が受け皿。
DELIVER_SUGGESTION = "📎 実ファイルをお送りしますか？（「送って」で添付します）"
# attachment_assist の他モード（同じツールの別 mode ＝必ず実在する）。
ATTACHMENT_MODE_SUGGESTION = (
    "✍️ 修正案（revise）・議事録化（minutes）・英訳（translate）もできます。"
)

_SUGGESTION_PREFIXES = ("📅 ", "📎 ", "✍️ ")

# ── 発火条件（純関数・決定論）────────────────────────────────────────────────

# 「日付らしき表現」。年月日・スラッシュ・曜日つき・相対日 + 時刻のいずれか。
_DATE_PATTERNS = (
    re.compile(r"\d{4}[-/年]\s?\d{1,2}[-/月]\s?\d{1,2}日?"),
    re.compile(r"\d{1,2}\s?[/月]\s?\d{1,2}\s?日?"),
    re.compile(r"[（(][月火水木金土日][）)]"),
    re.compile(r"(明日|明後日|来週|再来週|今週|翌週|来月)\D{0,6}\d{1,2}\s?(時|:\d{2})"),
)

# 「決定事項らしき表現」。日付だけの言及（雑談の中の「8/20 は忙しい」等）では出さない。
_DECISION_RE = re.compile(
    r"(決定|確定|決ま(り|った|りました)|開催|実施|開始|締切|期限|納期|アポ|訪問|"
    r"打(ち)?合わ?せ|ミーティング|MTG|定例|会議|レビュー|面談|商談|キックオフ)",
    re.IGNORECASE,
)

# 利用者が既に「ファイルを出して」と頼んでいる＝依頼が完結しており提案は不要（むしろ邪魔）。
_DELIVERY_REQUEST_RE = re.compile(
    r"(送って|送付|出して|ちょうだい|下さい|ください|添付|共有して|アップ(して|ロード))"
)


def suggestions_enabled() -> bool:
    """次の一手の提案そのものが有効か（``SUGGEST_NEXT_STEP``・既定 ON）。"""
    return os.environ.get("SUGGEST_NEXT_STEP", "true").strip().lower() in ("1", "true", "yes", "on")


def tool_enabled(env_name: str) -> bool:
    """受け皿ツールが今 ON か（scope 台帳の ``enabledBy`` と同じ env・既定 OFF）。

    既定 OFF なのは「出来ない約束をしない」ため。env が読めない環境では提案しない側へ倒す。
    """
    return os.environ.get(env_name, "").strip().lower() in ("1", "true", "yes", "on")


def has_scheduling_cue(text: str) -> bool:
    """本文に「決定事項 + 日付らしき表現」が **両方**あるか（カレンダー提案の発火条件）。

    片方だけでは出さない。日付だけなら雑談、決定語だけなら日時が決まっていない
    （どちらも「予定を登録しますか？」が的外れになる）。
    """
    if not text:
        return False
    if not _DECISION_RE.search(text):
        return False
    return any(pattern.search(text) for pattern in _DATE_PATTERNS)


def asks_for_delivery(query: str) -> bool:
    """利用者が既に「ファイルを送って」と頼んでいるか（提案を **抑止**する条件）。"""
    return bool(query) and bool(_DELIVERY_REQUEST_RE.search(query))


def append_suggestion(message: str, suggestion: str) -> str:
    """message の末尾に提案を 1 個だけ足す。

    - ``suggestion`` が空、または既に何らかの提案が付いている message には足さない
      （**1 応答につき最大 1 個**の不変条件をここで担保する）。
    - message が空なら提案だけを返しても意味が無いので素通し（提案は本文の付随物）。
    """
    if not suggestion or not message:
        return message
    if any(line.startswith(_SUGGESTION_PREFIXES) for line in message.splitlines()):
        return message
    return f"{message}\n\n{suggestion}"


__all__ = [
    "ATTACHMENT_MODE_SUGGESTION",
    "CALENDAR_SUGGESTION",
    "DELIVER_SUGGESTION",
    "append_suggestion",
    "asks_for_delivery",
    "has_scheduling_cue",
    "suggestions_enabled",
    "tool_enabled",
]
