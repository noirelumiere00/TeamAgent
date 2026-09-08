"""検索ヒットに添える「日付」の純関数（DB / env / LLM 非依存）。

便A-3（資料検索の更新日露出）。利用者の「最新の更新日からちゃんと出して」に対し、
Aico が **根拠のある日付だけ** を返すための土台。

- ``extract_title_date``: ファイル名 / タイトルに埋め込まれた日付
  （``20260227`` / ``2026-02-27`` / ``2026年2月27日`` / ``260227`` 等）を取り出す。
  資料の「提案日」に最も近い値で、Drive の modifiedTime（誰かが開いて保存した日）より
  利用者の期待に合う。
- ``resolve_date_basis``: ヒットの日付の根拠を 1 語で表す。
  ``title_date`` 優先 → 無ければ ``modified_at`` → どちらも無ければ ``none``。
  ``none`` のときは呼び側が日付フィールドを空にする（「根拠不明の日付」を出さない）。
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Literal

DateBasis = Literal["modified_at", "title_date", "none"]

# 妥当と見なす年の範囲（ファイル名中の 8 桁数字が金額・ID と紛れないための床/天井）。
_YEAR_MIN = 2000
_YEAR_MAX = 2099
# 2 桁年（YYMMDD）はさらに狭く取る（例 ``260708_提案書FMT.pptx``）。
_YY_MIN = 15
_YY_MAX = 39

# 区切り付き YYYY-MM-DD / YYYY/MM/DD / YYYY.MM.DD（区切りは前後で同じでなくてよい）。
_RE_YMD_SEP = re.compile(r"(?<!\d)(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
# 和暦風 YYYY年M月D日 / YYYY年M月。
_RE_YMD_JA = re.compile(r"(?<!\d)(20\d{2})年(\d{1,2})月(?:(\d{1,2})日)?")
# 連続 8 桁 YYYYMMDD（前後に数字が続かない）。
_RE_YMD_8 = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")
# 連続 6 桁 YYMMDD（文字列先頭・``_``・空白・開き括弧の直後だけ。Slack スレッド題の
# ``1720000000.123456`` の小数部（``.`` 直後）を日付に誤読しないよう ``.`` は含めない）。
_RE_YMD_6 = re.compile(r"(?:^|(?<=[_\s(\[（【「]))(\d{2})(\d{2})(\d{2})(?!\d)")


def _valid(y: int, m: int, d: int | None) -> str | None:
    """年月日が暦として妥当なら ISO 文字列（日なしは YYYY-MM）を返す。"""
    if not (_YEAR_MIN <= y <= _YEAR_MAX) or not (1 <= m <= 12):
        return None
    if d is None:
        return f"{y:04d}-{m:02d}"
    try:
        return _dt.date(y, m, d).isoformat()
    except ValueError:
        return None


def extract_title_date(text: str | None) -> str | None:
    """タイトル / ファイル名から日付を 1 つ抽出して ISO（YYYY-MM-DD / YYYY-MM）で返す。

    優先順位（同一文字列に複数あれば先に一致した形式を採用）:
    1. ``2026-02-27`` / ``2026/2/27`` / ``2026.02.27``
    2. ``2026年2月27日`` / ``2026年2月``（日なしは ``2026-02``）
    3. ``20260227``
    4. ``260227``（トークン先頭のみ・2 桁年 15〜39）

    暦として無効な値（13 月 / 2 月 30 日 等）や範囲外の年は無視する。
    見つからなければ None。純関数・例外を出さない。
    """
    if not text:
        return None
    s = str(text)

    m = _RE_YMD_SEP.search(s)
    if m:
        iso = _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if iso:
            return iso

    m = _RE_YMD_JA.search(s)
    if m:
        day = int(m.group(3)) if m.group(3) else None
        iso = _valid(int(m.group(1)), int(m.group(2)), day)
        if iso:
            return iso

    for m in _RE_YMD_8.finditer(s):
        iso = _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if iso:
            return iso

    for m in _RE_YMD_6.finditer(s):
        yy = int(m.group(1))
        if not (_YY_MIN <= yy <= _YY_MAX):
            continue
        iso = _valid(2000 + yy, int(m.group(2)), int(m.group(3)))
        if iso:
            return iso

    return None


def resolve_date_basis(updated_at: str | None, title_date: str | None) -> DateBasis:
    """ヒットに添える日付の根拠を決める。

    - title_date があれば ``title_date``（資料の日付＝提案日に最も近い）
    - 無ければ updated_at があれば ``modified_at``（取込元の最終更新）
    - どちらも無ければ ``none``（呼び側は日付を空にする）
    """
    if title_date:
        return "title_date"
    if updated_at:
        return "modified_at"
    return "none"


__all__ = ["DateBasis", "extract_title_date", "resolve_date_basis"]
