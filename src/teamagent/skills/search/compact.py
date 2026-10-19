"""Short answers keep only citations attached to retained sentences."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.prompts.loader import load_prompt
from teamagent.skills._shared.grounding import NumberGrounder
from teamagent.skills.search.campaign_files import is_campaign_hit, video_stats_footer
from teamagent.skills.search.comparison import compact_comparison, is_comparison_query
from teamagent.skills.search.composite import SlackItem

CTX_KEY = "_search_compact_answer"
_CITATION = re.compile(r"\[S:([\w-]+)\]")
_EXPLICIT_LIST = re.compile(
    r"一覧|リスト(?:アップ)?|列挙|(?:資料|事例|案件)を全部|全部(?:の資料)?を?(?:出|見せ|並べ)"
)
_METRICS = re.compile(r"(?:動画|投稿)(?:ごと|別|毎)|結果(?:を|と|も|が)|数値|数字|再生数|保存数")
_METRIC_PROSE = re.compile(r"再生|保存|いいね|視聴|平均|中央値|合計|投稿|本数")
_DATES = re.compile(
    r"(?:\d{4}[-/])?\d{1,2}[-/]\d{1,2}|(?:\d{4}年)?\d{1,2}月(?:\d{1,2}日)?|"
    r"\d{4}年|(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)"
)
# title, resolved URL, retrieved content. Keys are assigned by code, never model URLs.
Sources = dict[str, tuple[str, str, str]]


def explicit_list(query: str) -> bool:
    return bool(_EXPLICIT_LIST.search(query)) and not is_comparison_query(query)


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
        sources[str(hit.chunk_id)] = (
            str(meta.get("title") or meta.get("file_name") or "資料"),
            url_of(hit) or "",
            hit.content + "\n" + str(meta.get("updated_at") or ""),
        )
    for i, item in enumerate(slack_items):
        sources[f"slack{i}"] = (item.hit.channel or "Slack", item.hit.permalink, item.text)
    return sources


def prompt(sources: Sources) -> str:
    return (
        load_prompt("search", "compact", "system")
        + "\n# 引用の対応\n"
        + "\n".join(f"[S:{key}] {title}\n{content}" for key, (title, _, content) in sources.items())
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
) -> str:
    sources = {key: source for key, source in sources.items() if _safe_url(source[1])}
    derived = facts.splitlines() if all(key in sources for key in fact_keys) else []
    if derived and (
        len("\n".join(derived)) > 550
        or _sentence_count("\n".join(derived)) > 6
        or len({sources[key][1] for key in fact_keys}) > 3
        or len(facts) + len(_source_line(fact_keys, sources)) + 2 > 2500
    ):
        derived = []
    if any(key not in sources for key in metric_keys):
        metrics, metric_keys = "", ()
    lines: list[str] = []
    used: list[str] = []
    # A valid code-generated metric block also uses its source even without prose.
    for key in [*(fact_keys if derived else ()), *metric_keys]:
        if key in sources and key not in used and len(used) < 3:
            if len(_source_line([*used, key], sources)) + 64 > 2500:
                metrics = ""
                continue
            used.append(key)
    for raw in answer.splitlines():
        keys = list(dict.fromkeys(_CITATION.findall(raw)))
        if not keys or any(key not in sources for key in keys):
            continue
        candidate_keys = list(dict.fromkeys([*used, *keys]))
        if len({sources[key][1] or key for key in candidate_keys}) > 3:
            continue
        text = _CITATION.sub("", raw).strip()
        if re.match(
            r"^(?:[-*#・•]|[📁🏆🗂💬📎🕸]|金庫の資料|事例・施策実績|資料リンク|"
            r"関係図|探した範囲|施策の反応合計|施策の数字はシート)",
            text,
        ):
            continue
        # Quantitative metric prose comes only from the code-generated facts/rows.
        metric_text = _DATES.sub("", unicodedata.normalize("NFKC", text))
        if _METRIC_PROSE.search(metric_text) and re.search(r"\d", metric_text):
            continue
        if (
            not text
            or re.search(r"[a-z][a-z0-9+.-]*://|\[[^]]*\]\(", text, re.IGNORECASE)
            or len(derived) + len(lines) >= 6
            or _sentence_count("\n".join([*derived, *lines, text])) > 6
            or len("\n".join([*derived, *lines, text])) > 550
        ):
            continue
        if not NumberGrounder.from_inputs(
            "\n".join(sources[k][2] for k in keys), always_allowed=frozenset()
        ).ok(text):
            continue
        footer = _source_line(candidate_keys, sources)
        if len("\n".join([*derived, *lines, text, footer])) > 2500:
            continue
        lines.append(text)
        used = candidate_keys
    body = "\n".join([*derived, *lines]) or "取得した資料では、質問への回答を確認できませんでした。"
    footer = _source_line(used, sources)
    # Keep complete metric rows and URLs; explicit lists retain the original dataset.
    budget = 2500 - len(body) - len(footer) - 4
    rows: list[str] = []
    for row in metrics.splitlines():
        if len("\n".join([*rows, row])) > budget:
            break
        rows.append(row)
    return "\n\n".join(part for part in (body, "\n".join(rows), footer) if part)


def build(
    answer: str,
    hits: list[SearchHit],
    slack_items: Sequence[SlackItem],
    url_of: Callable[[SearchHit], str | None],
    *,
    query: str,
    asked_industry: str | None = None,
    karte_hits: Sequence[SearchHit] = (),
) -> str:
    facts, fact_ids = compact_comparison(query, hits)
    show_metrics = wants_metrics(query)
    return render(
        answer,
        source_map([*hits, *karte_hits], slack_items, url_of),
        facts=facts,
        fact_keys=[str(key) for key in fact_ids],
        metrics=video_stats_footer(hits, "", asked_industry=asked_industry) if show_metrics else "",
        metric_keys=[str(hit.chunk_id) for hit in hits if is_campaign_hit(hit)]
        if show_metrics
        else (),
    )
