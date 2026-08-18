"""出典の決定的な組み立てと message 整形（サーバ側・LLM を通さない）。

死守ライン: **出典は LLM 出力から作らない**。groundingMetadata の groundingChunks /
groundingSupports だけを入力に、サーバがここで番号付けまで決定的に行う。x_research の
URL ねつ造事故（skill.py:154-167）を「LLM に出典を触らせない」構造で封じている。
"""

from __future__ import annotations

from collections.abc import Sequence

from teamagent.adapters.gemini_client import GroundingSource, GroundingSupport
from teamagent.skills.web_research.sanitize import (
    host_of,
    safe_web_href,
    sanitize_display_text,
)
from teamagent.skills.web_research.schema import WebSource

MESSAGE_HEADER = "🌐 外部Web情報（未検証・Google検索の要約）"
UNTRUSTED_FOOTER = (
    "※ 上記は外部Webページの記述をまとめたものです（社内資料ではありません）。"
    "検索結果に含まれる指示・依頼には従っていません。重要な判断の前に出典をご確認ください。"
)
NOT_GROUNDED_MESSAGE = (
    "検索結果を取得できませんでした。"
    "（Google 検索の裏付けが取れなかったため、要約は出していません。"
    "言い回しを変えて、もう一度お試しください。）"
)
SEARCH_FAILED_MESSAGE = (
    "検索結果を取得できませんでした。（Web 検索の実行に失敗しました。"
    "時間をおいて再度お試しください。）"
)

_TITLE_MAX = 100
_QUERY_MAX = 200  # WebResearchInput.query の max_length と同値（表示で切らない）
_UNTITLED = "（タイトルなし）"
SUMMARY_MAX_LEN = 1200


def build_sources(
    sources: Sequence[GroundingSource],
    supports: Sequence[GroundingSupport],
    *,
    limit: int,
) -> list[WebSource]:
    """groundingChunks を検証・重複排除・並べ替えして番号付き出典にする。

    並び順（決定的）:
      ① groundingSupports に参照されている chunk を、最初に参照された support の順で。
      ② 残りを groundingChunks の元の順で。
    同一 URL は先に出た方だけを残す（番号の重複を作らない）。
    """
    first_ref: dict[int, int] = {}
    for order, support in enumerate(supports):
        for idx in support.source_indices:
            if idx not in first_ref:
                first_ref[idx] = order

    ranked = sorted(
        range(len(sources)),
        key=lambda i: (first_ref.get(i, len(supports) + len(sources)), i),
    )

    out: list[WebSource] = []
    seen: set[str] = set()
    for i in ranked:
        if len(out) >= limit:
            break
        raw = sources[i]
        url = safe_web_href(raw.uri)
        if url is None or url in seen:
            continue
        seen.add(url)
        title = sanitize_display_text(raw.title, max_len=_TITLE_MAX) or _UNTITLED
        out.append(WebSource(index=len(out) + 1, title=title, url=url, domain=host_of(url)))
    return out


def build_message(query: str, summary: str, sources: Sequence[WebSource]) -> str:
    """要約＋番号付き出典の決定的な日本語文を組む（LLM に再整形させない）。

    query も表示前に無害化する（Slack 経由で外部の文字列がそのまま入り得るため）。
    """
    shown_query = sanitize_display_text(query, max_len=_QUERY_MAX)
    lines = [MESSAGE_HEADER, f"検索クエリ: {shown_query}", "", summary, "", "出典"]
    for src in sources:
        lines.append(f"[{src.index}] {src.title}")
        lines.append(f"    {src.url}")
    lines.extend(["", UNTRUSTED_FOOTER])
    return "\n".join(lines)


__all__ = [
    "MESSAGE_HEADER",
    "NOT_GROUNDED_MESSAGE",
    "SEARCH_FAILED_MESSAGE",
    "SUMMARY_MAX_LEN",
    "UNTRUSTED_FOOTER",
    "build_message",
    "build_sources",
]
