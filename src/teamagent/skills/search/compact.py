"""Short answers keep only citations attached to retained sentences."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.prompts.loader import load_prompt
from teamagent.skills._shared.grounding import NumberGrounder
from teamagent.skills.search.comparison import is_comparison_query
from teamagent.skills.search.composite import SlackItem

CTX_KEY = "_search_compact_answer"
_CITATION = re.compile(r"\[S:([\w-]+)\]")
_EXPLICIT_LIST = re.compile(
    r"一覧|リスト(?:アップ)?|列挙|(?:資料|事例|案件)を全部|全部(?:の資料)?を?(?:出|見せ|並べ)"
)
_METRICS = re.compile(r"(?:動画|投稿)(?:ごと|別|毎)|結果(?:を|と|も|が)|数値|数字|再生数|保存数")
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
        if url in seen or not url or re.search(r"[\s<>()[\]`\"']", url):
            continue
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            continue
        seen.add(url)
        title = re.sub(r"[\[\]()<>@\n\r]", " ", title).strip()[:70] or "資料"
        links.append(f"[{title}]({url})")
    return "出典: " + " / ".join(links) if links else ""


def render(
    answer: str, sources: Sources, *, metrics: str = "", metric_keys: Sequence[str] = ()
) -> str:
    lines: list[str] = []
    used: list[str] = []
    # A valid code-generated metric block also uses its source even without prose.
    for key in metric_keys:
        if key in sources and key not in used and len(used) < 3:
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
        if not text or "http" in text or len(lines) >= 6 or len("\n".join([*lines, text])) > 550:
            continue
        if not NumberGrounder.from_inputs(
            "\n".join(sources[k][2] for k in keys), always_allowed=frozenset()
        ).ok(text):
            continue
        footer = _source_line(candidate_keys, sources)
        if len("\n".join([*lines, text, footer])) > 2500:
            continue
        lines.append(text)
        used = candidate_keys
    body = "\n".join(lines) or "取得した資料では、質問への回答を確認できませんでした。"
    footer = _source_line(used, sources)
    # Keep complete metric rows and URLs; explicit lists retain the original dataset.
    budget = 2500 - len(body) - len(footer) - 4
    rows: list[str] = []
    for row in metrics.splitlines():
        if len("\n".join([*rows, row])) > budget:
            break
        rows.append(row)
    return "\n\n".join(part for part in (body, "\n".join(rows), footer) if part)
