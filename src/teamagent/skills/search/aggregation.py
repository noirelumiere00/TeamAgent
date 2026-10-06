"""集約・一覧クエリの検出とメタデータフィルタ抽出 (DB 非依存・純ロジック)。

Sprint 5。「BANT A の案件一覧」「検討止まりの案件」「代理店経由の案件」のような
**列挙系クエリ**は、単一 chunk への類似検索 (top-k semantic) では原理的に
答えられない (gold set の残り miss の主因)。これらは意味検索ではなく
``WHERE metadata->>'bant_score' = 'A'`` のような構造化フィルタによる列挙で
答えるべき。本モジュールはクエリから列挙意図とフィルタを取り出す。

設計方針:
- 明確なメタデータ信号 (BANT 評価 / チャネル種別) のみを拾う保守的設計。
  特定クライアント名を含む通常クエリを誤って列挙モードに倒さないため、
  検出されたフィルタが無ければ None を返し、呼び出し側は通常の意味検索を使う。
- **「失注」は BANT C へ写さない**（2026-10-01・案件検索 v3 PR 3a）。以前は FB に明示の
  「失注」欄が無いため bant_score=C に近似していたが、BANT C は「検討止まり」の評価であって
  正式な受注・失注ではない。近似の結果を「失注案件」として返すと、根拠の無い失注の断定になる。
  正式な受注状態の正本は当面なし（小俣さん裁定 10-01）なので、「失注」を含むクエリは
  列挙モードに入らず通常の意味検索へ回す。BANT C を使うのは「BANT C」「検討止まり」と
  明示されたときだけ。
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

    # 検討止まり: gold set の定義どおり BANT C の言い換えとして扱う（明示されたときだけ）。
    # 既に BANT 指定があればそちらを優先 (上書きしない)。
    # 「失注」はここで扱わない（モジュール冒頭の説明を参照）。
    if "検討止まり" in query and "bant_score" not in filters:
        filters["bant_score"] = "C"

    return filters or None
