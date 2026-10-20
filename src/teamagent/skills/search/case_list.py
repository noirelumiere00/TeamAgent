"""事例要求は取得した資料から最大5行だけ作る。"""

from collections.abc import Callable, Sequence

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.search.answer_mode import strip_advice
from teamagent.skills.search.answer_sections import (
    _clean,
    _link,
    _safe_url,
    case_external_use_mark,
    entry_from_hit,
)
from teamagent.skills.search.campaign_files import video_footer_urls
from teamagent.skills.search.composite import SlackItem
from teamagent.skills.search.not_found import case_target_hits


def render(
    hits: Sequence[SearchHit], target: str | None, url_of: Callable[[SearchHit], str | None]
) -> str:
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for hit in case_target_hits(hits, target):
        entry = entry_from_hit(hit, url=url_of(hit))
        url = _safe_url(entry.url)
        link_label = "資料"
        if not url:
            url = next(
                (
                    safe
                    for value in video_footer_urls([hit]).splitlines()
                    if (safe := _safe_url(value))
                ),
                None,
            )
            link_label = "投稿"
        key = (entry.document_id or url or "", entry.title)
        if not url or key in seen or entry.kind in {"karte", "research"}:
            continue
        seen.add(key)
        meta = hit.metadata or {}
        detail = strip_advice(str(meta.get("case_effect") or hit.content), "事例")
        detail = _clean(detail.splitlines()[0] if detail else "", 80)
        if entry.kind == "deal":
            detail = "案件（実績ではありません） " + detail
        if entry.kind == "case":
            detail += " " + case_external_use_mark(entry.case_external_use)
        line = (
            f"{_clean(entry.title, 100)}（{entry.date or '日付不明'}）— "
            f"{detail.strip()} {_link(link_label, url)}"
        )
        if len("\n".join([*lines, line])) > 2500:
            continue
        lines.append(line)
        if len(lines) == 5:
            break
    return "\n".join(lines)


def slack_cases(items: Sequence[SlackItem]) -> list[SearchHit]:
    return [
        SearchHit(
            chunk_id=-10000 - i,
            content=item.text,
            score=1.0,
            metadata={
                "title": item.text.splitlines()[0][:100] if item.text else "Slackの事例記録",
                "source_uri": item.hit.permalink,
                "source_type": "slack",
            },
        )
        for i, item in enumerate(items)
    ]
