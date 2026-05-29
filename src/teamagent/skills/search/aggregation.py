"""集約・一覧クエリの検出とメタデータフィルタ抽出 (DB 非依存・純ロジック)。

Sprint 5。「BANT A の案件一覧」「失注案件」「代理店経由の案件」のような
**列挙系クエリ**は、単一 chunk への類似検索 (top-k semantic) では原理的に
答えられない (gold set の残り miss の主因)。これらは意味検索ではなく
``WHERE metadata->>'bant_score' = 'A'`` のような構造化フィルタによる列挙で
答えるべき。本モジュールはクエリから列挙意図とフィルタを取り出す。

設計方針:
- 明確なメタデータ信号 (BANT 評価 / チャネル種別 / 失注) のみを拾う保守的設計。
  特定クライアント名を含む通常クエリを誤って列挙モードに倒さないため、
  検出されたフィルタが無ければ None を返し、呼び出し側は通常の意味検索を使う。
- 失注 → bant_score=C は近似マッピング (FB に明示的な「失注」フィールドが無く、
  gold set が C 評価=検討止まりと定義しているため)。
"""

from __future__ import annotations

import re

# BANT 評価: 「BANT A」「BANTのA」「BANT:B」等から A/B/C を取る
_BANT_RE = re.compile(r"BANT[\s:のはが]*([ABC])", re.IGNORECASE)


def extract_aggregation_filter(query: str) -> dict[str, str] | None:
    """クエリから列挙系メタデータフィルタを抽出する。

    返り値:
        {"bant_score": "A"} のようなフィルタ dict。該当信号が無ければ None
        (= 呼び出し側は通常の意味検索にフォールバック)。
    """
    filters: dict[str, str] = {}

    m = _BANT_RE.search(query)
    if m:
        filters["bant_score"] = m.group(1).upper()

    # チャネル種別 (代理店経由 / 直販)
    if "代理店" in query:
        filters["channel_type"] = "代理店"
    elif "直販" in query:
        filters["channel_type"] = "直販"

    # 失注 (lost): 明示フィールドが無いため bant_score=C に近似マッピング。
    # 既に BANT 指定があればそちらを優先 (上書きしない)。
    if "失注" in query and "bant_score" not in filters:
        filters["bant_score"] = "C"

    return filters or None
