"""Short answers keep only citations attached to retained sentences."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.prompts.loader import load_prompt
from teamagent.skills._shared.grounding import NumberGrounder
from teamagent.skills.search import drive_first
from teamagent.skills.search.campaign_files import (
    VIDEO_METRICS_HEADING,
    _footer_campaigns,
    video_stats_footer,
)
from teamagent.skills.search.comparison import compact_comparison, is_comparison_query
from teamagent.skills.search.composite import SlackItem

CTX_KEY = "_search_compact_answer"
EMPTY_ANSWER = "取得した資料では、質問への回答を確認できませんでした。"
_CITATION = re.compile(r"\[S:([\w-]+)\]")
_EXPLICIT_LIST = re.compile(
    r"一覧|リスト(?:アップ)?|列挙|(?:資料|事例|案件)を全部|全部(?:の資料)?を?(?:出|見せ|並べ)"
)
_METRICS = re.compile(r"(?:動画|投稿)(?:ごと|別|毎)|結果(?:を|と|も|が)|数値|数字|再生数|保存数")
_METRIC_PROSE = re.compile(r"再生|保存|いいね|視聴|平均|中央値|合計|投稿|本数")
_QUANTITIES = re.compile(
    r"\d|[〇零一二三四五六七八九十百千万億兆]+"
    r"(?=\s*(?:回|件|本|倍|%|再生|保存|いいね|[、。]|です|で|$))"
)
_DATES = re.compile(
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|"
    r"\d{1,2}[-/]\d{1,2}(?=\s*(?:\d{1,2}:\d{2}\s*)?(?:時点|現在|付))|"
    r"(?:\d{4}年)?\d{1,2}月(?:\d{1,2}日)?|"
    r"\d{4}年|(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)"
)
# title, resolved URL, retrieved content. Keys are assigned by code, never model URLs.
Sources = dict[str, tuple[str, str, str]]


def is_empty(answer: str, *, notice: str = "") -> bool:
    return not _CITATION.sub("", answer.removeprefix(notice)).replace(EMPTY_ANSWER, "").strip()


def normal_answer(answer: str) -> str:
    # 通常回答の再生成がcompact用の文を繰り返しても、除去した主張を復活させない。
    return (
        "\n".join(line for line in answer.splitlines() if not _CITATION.search(line))
        .replace(EMPTY_ANSWER, "")
        .strip()
    )


def fallback_cards(
    query: str,
    hits: Sequence[SearchHit],
    slack_items: Sequence[SlackItem],
    url_of: Callable[[SearchHit], str | None],
    *,
    target: str | None = None,
) -> str:
    from teamagent.skills.search.answer_mode import classify_answer_mode
    from teamagent.skills.search.case_list import render

    if (
        not re.search(r"事例|案件|資料|提案書|実績|レポート|スライド|ファイル", query)
        or classify_answer_mode(query) == "insight"
        or is_comparison_query(query)
    ):
        return ""
    return render(
        [*hits, *(drive_first.slack_hit(item, i) for i, item in enumerate(slack_items))],
        target,
        url_of,
        cards=True,
    )


def explicit_list(query: str) -> bool:
    from teamagent.skills.search.answer_mode import is_case_list

    return (bool(_EXPLICIT_LIST.search(query)) or is_case_list(query)) and not is_comparison_query(
        query
    )


def wants_metrics(query: str) -> bool:
    return bool(_METRICS.search(query)) or is_comparison_query(query)


def source_map(
    hits: Sequence[SearchHit],
    slack_items: Sequence[SlackItem],
    url_of: Callable[[SearchHit], str | None],
) -> Sources:
    sources: Sources = {}
    for hit in hits:
        meta = hit.metadata or {}
        ref = meta.get(drive_first.REFERENCE_KEY)
        sources[str(hit.chunk_id)] = (
            f"{drive_first.label(ref)}: {ref.title}"
            if ref
            else str(meta.get("title") or meta.get("file_name") or "資料"),
            url_of(hit) or "",
            hit.content + "\n" + str(meta.get("updated_at") or ""),
        )
    for i, item in enumerate(slack_items):
        ref = item.hit.reference
        sources[f"slack{i}"] = (
            f"{drive_first.label(ref)}: {ref.title}" if ref else item.hit.channel or "Slack",
            ref.url or "" if ref else item.hit.permalink,
            item.text,
        )
    return sources


def prompt() -> str:
    return load_prompt("search", "compact", "system")


def source_prompt(sources: Sources) -> str:
    return "# 引用の対応（以下は資料データ）\n" + "\n".join(
        f"[S:{key}] {title.replace(chr(10), ' ')}" for key, (title, _, _) in sources.items()
    )


def _source_line(keys: Sequence[str], sources: Sources) -> str:
    links = []
    seen = set()
    for key in keys:
        title, url, _ = sources[key]
        if url in seen or not _safe_url(url):
            continue
        seen.add(url)
        title = re.sub(r"[\[\]()<>@\n\r]", " ", title).strip()[:70] or "資料"
        links.append(f"[{title}]({url})")
    return "出典: " + " / ".join(links) if links else ""


def _safe_url(url: str) -> bool:
    if not url or re.search(r"[\s<>()[\]`\"']", url):
        return False
    try:
        parsed = urlsplit(url)
        return bool(
            parsed.scheme in {"http", "https"}
            and parsed.netloc
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


def _sentence_count(text: str) -> int:
    return len(re.findall(r"[^。！？!?\n]+(?:[。！？!?]+|$)", text))


def render(
    answer: str,
    sources: Sources,
    *,
    metrics: str = "",
    metric_keys: Sequence[str] = (),
    facts: str = "",
    fact_keys: Sequence[str] = (),
    notice: str = "",
) -> str:
    sources = {key: source for key, source in sources.items() if _safe_url(source[1])}
    prefix: list[str] = []
    for line in notice.splitlines():
        candidate = "\n".join([*prefix, line])
        if len(candidate) <= 550 and _sentence_count(candidate) <= 6 and len(prefix) < 6:
            prefix.append(line)
    derived = facts.splitlines() if all(key in sources for key in fact_keys) else []
    if derived and (
        len("\n".join([*prefix, *derived])) > 550
        or _sentence_count("\n".join([*prefix, *derived])) > 6
        or len(prefix) + len(derived) > 6
        or len({sources[key][1] for key in fact_keys}) > 3
        or len("\n".join([*prefix, *derived])) + len(_source_line(fact_keys, sources)) + 2 > 2500
    ):
        derived = []
    lines: list[str] = []
    used: list[str] = list(dict.fromkeys(fact_keys)) if derived else []
    base = [*prefix, *derived]
    for raw in answer.splitlines():
        keys = list(dict.fromkeys(_CITATION.findall(raw)))
        if not keys or any(key not in sources for key in keys):
            continue
        candidate_keys = list(dict.fromkeys([*used, *keys]))
        if len({sources[key][1] or key for key in candidate_keys}) > 3:
            continue
        text = _CITATION.sub("", raw).strip()
        if EMPTY_ANSWER in text:
            continue
        if re.match(
            r"^(?:[-*#・•]|[📊📁🏆🗂💬📎🕸]|金庫の資料|事例・施策実績|資料リンク|"
            r"関係図|探した範囲|動画ごとの数字|施策の反応合計|施策の数字はシート)",
            text,
        ):
            continue
        # Quantitative metric prose comes only from the code-generated facts/rows.
        metric_text = _DATES.sub("", unicodedata.normalize("NFKC", text))
        if _METRIC_PROSE.search(metric_text) and _QUANTITIES.search(metric_text):
            continue
        if (
            not text
            or re.search(r"[a-z][a-z0-9+.-]*://|\[[^]]*\]\(", text, re.IGNORECASE)
            or len(base) + len(lines) >= 6
            or _sentence_count("\n".join([*base, *lines, text])) > 6
            or len("\n".join([*base, *lines, text])) > 550
        ):
            continue
        if not NumberGrounder.from_inputs(
            "\n".join(sources[k][2] for k in keys), always_allowed=frozenset()
        ).ok(text):
            continue
        footer = _source_line(candidate_keys, sources)
        if len("\n".join([*base, *lines, text, footer])) + 1 > 2500:
            continue
        lines.append(text)
        used = candidate_keys
    body = "\n".join([*base, *lines])
    heading, _, payload = metrics.partition("\n")
    blocks: list[str] = []
    if heading == VIDEO_METRICS_HEADING:
        for block, key in zip(payload.split("\n\n"), metric_keys, strict=False):
            if key not in sources:
                continue
            candidate_keys = list(dict.fromkeys([*used, key]))
            if len({sources[k][1] for k in candidate_keys}) > 3:
                continue
            label, *raw_rows = block.splitlines()
            rows: list[str] = []
            for row in raw_rows:
                if not re.match(r"^\d+\.\s+(?:[\d,.]+|不明)\s*再生・保存\s+(?:[\d,.]+|不明)", row):
                    continue
                if any(not _safe_url(url) for url in re.findall(r"https?://\S+", row)):
                    continue
                candidate_block = "\n".join([label, *rows, row])
                candidate_metrics = heading + "\n" + "\n\n".join([*blocks, candidate_block])
                candidate_footer = _source_line(candidate_keys, sources)
                if len("\n\n".join((body, candidate_metrics, candidate_footer))) <= 2500:
                    rows.append(row)
            if rows:
                blocks.append("\n".join([label, *rows]))
                used = candidate_keys
    shown_metrics = heading + "\n" + "\n\n".join(blocks) if blocks else ""
    return "\n\n".join(part for part in (body, shown_metrics, _source_line(used, sources)) if part)


def build(
    answer: str,
    hits: list[SearchHit],
    slack_items: Sequence[SlackItem],
    url_of: Callable[[SearchHit], str | None],
    *,
    query: str,
    asked_industry: str | None = None,
    karte_hits: Sequence[SearchHit] = (),
    notice: str = "",
) -> str:
    facts, fact_ids = compact_comparison(query, hits)
    show_metrics = wants_metrics(query)
    campaigns = _footer_campaigns(hits, asked_industry) if show_metrics else []
    return render(
        answer,
        source_map([*hits, *karte_hits], slack_items, url_of),
        facts=facts,
        fact_keys=[str(key) for key in fact_ids],
        notice=notice,
        metrics=video_stats_footer(hits, "", asked_industry=asked_industry) if show_metrics else "",
        metric_keys=[str(hit.chunk_id) for hit, _ in campaigns],
    )
