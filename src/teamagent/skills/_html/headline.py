"""レポート冒頭の「一行見出し」を本文から作る（Bedrock・上限つき）。

なぜ要るか:
    共通テンプレは中身を解釈しないので、放っておくと全レポートの見出しが
    「〈クエリ〉 — 上位N本」で終わる。読む側が最初に知りたいのは件数ではなく
    **「で、何が起きているのか」**（例:「包まないほど伸びている」）。

なぜ許されるか:
    本文（``analysis`` / ``draft`` / ``review``）自体が既に LLM の生成物であり、その 1 行要約は
    新しい主張を持ち込まない。**本文の外の事実は書かせない**ことをプロンプトと後段の検証で縛る。

上限（暴走とコストを構造で抑える）:
    - 入力は本文の先頭 ``_INPUT_MAX`` 文字だけ（長文レポートでもトークンが線形に増えない）
    - ``max_tokens`` は 80、temperature 0（毎回ぶれない）
    - 出力は 1 行・``_OUT_MAX`` 文字まで。改行以降は捨てる
    - URL・HTML・鉤括弧つきの引用に見える体裁が混じったら**採用しない**
    - 例外・空・長すぎは全て ``None``＝見出し無しで描画（無いこと自体は事故ではない）
"""

from __future__ import annotations

import os
import re
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_INPUT_MAX = 1800
_OUT_MAX = 40
_MAX_TOKENS = 80

_SYSTEM = (
    "あなたは社内レポートの見出しを付ける編集者です。"
    "渡された本文だけを根拠に、日本語の見出しを1つだけ返します。"
    "制約: 20〜40字。体言止めまたは言い切り。"
    "本文に書かれていない事実・数値・固有名詞を足さない。"
    "URL・記号装飾・鉤括弧・絵文字・前置き・複数案は禁止。見出しの文字列だけを返す。"
)

_BAD = re.compile(r"https?://|[<>]|^[「『]|見出し[:：]", re.IGNORECASE)


def headline_enabled() -> bool:
    """``USE_HTML_REPORT_HEADLINE`` が真のときだけ生成する（既定 OFF）。"""
    return (os.environ.get("USE_HTML_REPORT_HEADLINE") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _clean(raw: str) -> str | None:
    """モデル出力を 1 行の見出しへ整える。採用できなければ None。"""
    line = (raw or "").strip().splitlines()[0].strip() if (raw or "").strip() else ""
    line = line.strip("　 \t")
    if not line or len(line) > _OUT_MAX or _BAD.search(line):
        return None
    return line


def make_headline(
    body_md: str, *, bedrock: Any | None, request_id: str, tool: str = ""
) -> str | None:
    """本文の 1 行要約を返す。無効・失敗・規約違反は ``None``（見出し無しで描画）。"""
    if not headline_enabled() or not body_md or not body_md.strip():
        return None
    if bedrock is None:
        return None
    try:
        resp = bedrock.converse(
            [{"role": "user", "content": [{"text": body_md[:_INPUT_MAX]}]}],
            request_id,
            system=_SYSTEM,
            temperature=0.0,
            max_tokens=_MAX_TOKENS,
        )
        line = _clean(getattr(resp, "text", "") or "")
        if line is None:
            logger.info("report_headline_rejected", request_id=request_id, tool=tool)
            return None
        logger.info("report_headline_made", request_id=request_id, tool=tool, chars=len(line))
        return line
    except Exception as e:  # 見出しは装飾。本体のレポートは必ず出す。
        logger.warning(
            "report_headline_failed", request_id=request_id, tool=tool, error=type(e).__name__
        )
        return None


__all__ = ["headline_enabled", "make_headline"]
