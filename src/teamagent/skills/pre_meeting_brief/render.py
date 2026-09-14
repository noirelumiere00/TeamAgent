"""事例ブリーフの描画（DELTA §3 の実物例に厳密に合わせる）＋無害化。

無害化（``harden``）の順序は固定:
  1. NFKC 正規化
  2. 制御文字・改行の除去
  3. ``<`` ``>`` ``@`` を全角へ
  4. 字数上限

これで ``<!channel>`` ``<@U…>`` ``<url|text>`` を **組み立て不能** にする。URL は
``source_uri`` の実値だけをリンク化し、文字列連結で URL を作らない。

出力例（DELTA §3）:

    🌞 9/11(金) アポ前 事例ブリーフィング
    本日の社外MTG：2件

    ▶️ 14:00–15:00  【社外】青葉広告山田様
      クライアント：北都リゾート（レジャー・テーマパーク）／代理店：青葉広告（山田様）
      └ 南島リゾートパーク（観光・テーマパーク）— …。 社内担当: 田中  ⚠口頭紹介のみ
      ※「北都リゾート」自体の実施事例はDrive上で確認できず（観光・テーマパークで近い実績）。

    — 出典 —
    • 📍ショート動画施策事例集（マスター表・営業担当列より）
"""

from __future__ import annotations

import datetime as _dt
import unicodedata
from typing import Any

from teamagent.skills.morning_digest import calendar_window as _calwin

# 社外 MTG が 0 件の日の 1 行（節ごと消さない＝「予定が無い日」と区別できるようにする）。
NO_EXTERNAL_LINE = "📌 本日は社外MTGなし"
SOURCES_HEADER = "— 出典 —"
MASTER_SHEET_SOURCE = "📍ショート動画施策事例集（マスター表・営業担当列より）"
# 送信時刻が下限 06:00 に張り付いた日に冒頭へ添える 1 行（DELTA §1）。
EARLY_NOTICE_LINE = "⏰ 最初の予定が近いため、通常より短い間隔でお送りしています"

_MAX_TITLE = 60
_MAX_COMPANY = 40
_MAX_EFFECT = 120
_MAX_NOTE = 60


def harden(raw: str | None, limit: int) -> str:
    """Slack 記法を組み立て不能にする無害化（NFKC → 制御文字除去 → 全角化 → 上限）。"""
    if not raw:
        return ""
    text = unicodedata.normalize("NFKC", str(raw))
    text = "".join(ch for ch in text if ch >= " " and ch != "\x7f")
    text = text.replace("<", "＜").replace(">", "＞").replace("@", "＠")
    text = text.replace("&", "＆")  # &amp; 由来の実体参照も潰す
    return text.strip()[:limit]


def _fmt_time_range(start_at: str | None, end_at: str | None) -> str:
    """``14:00–15:00``（en dash）。読めなければ空文字（推測しない）。"""
    start = _calwin.parse_jst_datetime(start_at)
    if start is None:
        return ""
    end = _calwin.parse_jst_datetime(end_at)
    head = start.astimezone(_calwin.JST).strftime("%H:%M")
    if end is None:
        return head
    return f"{head}–{end.astimezone(_calwin.JST).strftime('%H:%M')}"


def _client_line(item: Any) -> str:
    """``クライアント：北都リゾート（レジャー・テーマパーク）／代理店：青葉広告（山田様）``。"""
    names = list(getattr(item, "clients_display", []) or [])
    industries = list(getattr(item, "client_industries", []) or [])
    parts: list[str] = []
    for idx, name in enumerate(names):
        label = harden(name, _MAX_COMPANY)
        if not label:
            continue
        industry = harden(industries[idx] if idx < len(industries) else "", 24)
        parts.append(f"{label}（{industry}）" if industry else label)
    agency = harden(getattr(item, "agency_display", ""), _MAX_COMPANY)
    if not parts and not agency:
        return ""
    line = "クライアント：" + ("・".join(parts) if parts else "（要確認）")
    if agency:
        line += f"／代理店：{agency}"
    return line


def _case_line(case: Any, *, multi_client: bool) -> str:
    """``└ [○○系] 企業名「商材」（業種）— 効果。 社内担当: 氏名  ⚠注記``。"""
    company = harden(getattr(case, "company_display", ""), _MAX_COMPANY) or "（企業名不明）"
    head = ""
    group = harden(getattr(case, "client_group", ""), 24)
    if multi_client and group:
        head = f"[{group}系] "
    product = harden(getattr(case, "product_display", ""), _MAX_COMPANY)
    product_part = f"「{product}」" if product else ""
    if getattr(case, "same_client", False):
        paren = "（同一クライアント）"
    else:
        industry = harden(getattr(case, "industry_display", ""), 24)
        paren = f"（{industry}）" if industry else ""
    effect = harden(getattr(case, "effect_display", ""), _MAX_EFFECT)
    effect_part = f"— {effect}" if effect else "— （効果は資料内・リンク参照）"
    owner = harden(getattr(case, "owner_display", ""), 24) or "未登録"
    line = f"└ {head}{company}{product_part}{paren} {effect_part} 社内担当: {owner}"
    note = harden(getattr(case, "external_use_note", ""), _MAX_NOTE)
    if note:
        line += f"  {note}"
    return line


def render_brief_lines(output: Any, day: _dt.date, *, early_notice: bool = False) -> list[str]:
    """事例ブリーフ節を行リストで返す。

    - ``corpus_available=False`` → **空リスト**（節そのものを出さない）
    - 社外 0 件 → ``📌 本日は社外MTGなし`` の 1 行のみ
    """
    if not getattr(output, "corpus_available", False):
        return []
    if not getattr(output, "scanned", False):
        return []

    lines: list[str] = []
    if early_notice:
        lines.append(EARLY_NOTICE_LINE)
    items = list(getattr(output, "items", []) or [])
    lines.append(f"🌞 {_calwin.fmt_jst_date(day)} アポ前 事例ブリーフィング")
    if not items:
        lines.append(NO_EXTERNAL_LINE)
        return lines
    # ⚠️ 見出しは **切り詰める前** の真の件数。len(items) を出すと、社外 8 件の日に
    #   「5件」と表示され、残り 3 件は行も注記も出ないまま消える（当日のアポを
    #   取りこぼす）。真の件数は external_count + uncertain_count が持っている。
    total = int(getattr(output, "external_count", 0)) + int(getattr(output, "uncertain_count", 0))
    total = max(total, len(items))
    lines.append(f"本日の社外MTG：{total}件")
    for item in items:
        lines.append("")
        when = _fmt_time_range(getattr(item, "start_at", None), getattr(item, "end_at", None))
        title = harden(getattr(item, "title_display", ""), _MAX_TITLE) or "（無題）"
        suffix = "（社外か要確認）" if getattr(item, "verdict", "") == "uncertain" else ""
        lines.append(f"▶️ {when}  {title}{suffix}" if when else f"▶️ {title}{suffix}")
        client_line = _client_line(item)
        if client_line:
            lines.append(f"  {client_line}")
        cases = list(getattr(item, "cases", []) or [])
        multi = len(list(getattr(item, "clients_display", []) or [])) > 1
        for case in cases:
            lines.append(f"  {_case_line(case, multi_client=multi)}")
        if not cases:
            lines.append("  └ 該当事例なし（要確認）")
        note = harden(getattr(item, "no_exact_note", ""), 160)
        if note:
            lines.append(f"  ※{note}")

    hidden = total - len(items)
    if hidden > 0:
        # 黙って縮小しない。落ちた MTG があることは必ず 1 行で告げる。
        lines.append("")
        lines.append(f"…ほか {hidden} 件（表示上限）")

    # ⚠️ harden は **データ由来の行だけ** に掛ける。コード定数（MASTER_SHEET_SOURCE）へ
    #   掛けると NFKC がリテラルの全角括弧まで半角化し、DELTA §3 の実物例と字面がズレる。
    sources = [
        s if s == MASTER_SHEET_SOURCE else harden(s, 120)
        for s in (getattr(output, "source_lines", []) or [])
    ]
    sources = [s for s in sources if s]
    if sources:
        lines.append("")
        lines.append(SOURCES_HEADER)
        lines.extend(f"• {s}" for s in sources)
    return lines


__all__ = [
    "EARLY_NOTICE_LINE",
    "MASTER_SHEET_SOURCE",
    "NO_EXTERNAL_LINE",
    "SOURCES_HEADER",
    "harden",
    "render_brief_lines",
]
