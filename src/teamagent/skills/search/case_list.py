"""事例要求は取得した根拠から最大5件だけ作る。"""

import re
from collections.abc import Callable, Sequence
from datetime import date
from urllib.parse import urlsplit

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.search import drive_first
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
from teamagent.skills.search.schema import DocumentReference

# shortcut: 候補の既定幅は30件、Drive事例の取りこぼしが続く場合はページングへ拡張する。
CANDIDATE_LIMIT = 30
EMPTY_DRIVE_CASES = "Drive に資料のある事例は見つかりませんでした。"


def case_link(url: str | None, label: str) -> str | None:
    """定型カードの2種類のリンクだけを、正確な原本・会話ホストへ許す。"""
    safe = _safe_url(url)
    if not safe:
        return None
    try:
        parsed = urlsplit(safe)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            return None
        allowed = (label == "資料を見る" and drive_first.drive_file_id(safe)) or (
            label == "会話の記録"
            and (parsed.hostname or "").endswith(".slack.com")
            and re.fullmatch(r"/archives/[CDG][A-Z0-9]+/p[0-9]{16}", parsed.path)
        )
    except ValueError:
        return None
    return f"<{safe.replace('&', '&amp;')}|{label}>" if allowed else None


def _month(value: str | None) -> str:
    try:
        parsed = date.fromisoformat(value + "-01" if value and len(value) == 7 else value or "")
    except ValueError:
        return "日付不明"
    return f"{parsed.year}年{parsed.month}月"


def _source_text(text: str) -> str:
    return re.sub(
        r"\[[^\]\n]*\]\(<?https?://[^)\n]*>?\)|<https?://[^>]+>|https?://[^\s<>]+|<[@!][^>\n]*>",
        "",
        text,
        flags=re.IGNORECASE,
    )


def _details(text: str) -> list[str]:
    text = _source_text(strip_advice(text, "事例"))
    text = "\n".join(
        line
        for line in text.splitlines()
        if not re.search(r"©|copyright|all\s+rights\s+reserved", line, re.IGNORECASE)
        and not re.fullmatch(
            r"\s*(?:[-–—]\s*)?(?:(?:page|p\.)\s*)?\d+"
            r"(?:\s*[/／]\s*\d+)?\s*(?:ページ|頁)?\s*[-–—]?\s*",
            line,
            re.IGNORECASE,
        )
    )
    lines = []
    for part in re.split(r"[\r\n]+|(?<=[。！？])", text):
        part = re.sub(r"^[\s#*・•\-]+", "", part).strip()
        if (
            not part
            or re.fullmatch(r"[^。！？]{1,30}[:：]", part)
            or re.match(r"(?:まとめ|要約|推奨アクション|次の一手|おすすめ)", part)
        ):
            continue
        cleaned = _clean(part, 100)
        if cleaned and cleaned not in lines:
            lines.append(cleaned)
    return lines[:2]


def render(
    hits: Sequence[SearchHit],
    target: str | None,
    url_of: Callable[[SearchHit], str | None],
    *,
    displayed_refs: list[DocumentReference] | None = None,
    cards: bool = False,
    drive_only: bool = False,
    max_cases: int = 5,
) -> str:
    lines: list[str] = []
    separator = (
        "\n\n"
        if cards or any((h.metadata or {}).get(drive_first.REFERENCE_KEY) for h in hits)
        else "\n"
    )
    seen: set[tuple[str, str]] = set()
    for hit in case_target_hits(hits, target, soft_industry=drive_only):
        meta = hit.metadata or {}
        entry = entry_from_hit(hit, url=url_of(hit))
        url = _safe_url(entry.url)
        link_label = "資料"
        ref = (hit.metadata or {}).get(drive_first.REFERENCE_KEY)
        if (cards or drive_only) and ref is None:
            ref = drive_first.canonical_reference(hit.metadata or {}, hit.content)
        if drive_only and (
            ref is None
            or ref.kind != "drive"
            or not case_link(ref.url, "資料を見る")
            or entry.kind in {"karte", "research", "deal"}
            or str(meta.get("is_sales_fb") or "").lower() in {"true", "1", "yes"}
            or re.search(r"営業フィードバック|営業FB", str(meta.get("channel_name") or ""))
        ):
            continue
        if ref is not None:
            url = _safe_url(ref.url)
            link_label = drive_first.label(ref)
        if not url and ref is None:
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
        if ref is not None and ref.drive_file_id:
            key = (ref.drive_file_id, entry.title if entry.kind == "case" else "")
        if (not url and ref is None) or key in seen or entry.kind in {"karte", "research"}:
            continue
        seen.add(key)
        if ref is not None:
            details = _details(str(meta.get("case_effect") or hit.content))
            mark = (
                "案件（実績ではありません）"
                if entry.kind == "deal"
                else (
                    case_external_use_mark(entry.case_external_use) if entry.kind == "case" else ""
                )
            )
            if mark:
                details = [mark + (" " + details[0] if details else ""), *details[1:]]
            title = (
                _clean(_source_text(entry.title), 100)
                or _clean(_source_text(ref.title), 100)
                or "案件名不明"
            )
            card = [f"*{title}*（{_month(entry.date)}）"]
            card.extend(f"• {detail}" for detail in details)
            if ref.kind == "drive":
                card.append("• " + (case_link(url, "資料を見る") or "資料URL未確認"))
            elif ref.kind in {"slack_attachment", "slack_conversation"}:
                if ref.kind == "slack_attachment":
                    card.append("• Slack にだけある資料（本文未読）")
                card.append("• " + (case_link(url, "会話の記録") or "会話のURL未確認"))
            else:
                card.append(f"• {drive_first.label(ref)}（資料URL未確認）")
            line = "\n".join(card)
        else:
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
        if len(separator.join([*lines, line])) > 2500:
            continue
        lines.append(line)
        if displayed_refs is not None and ref is not None:
            displayed_refs.append(ref)
        if len(lines) == max_cases:
            break
    return separator.join(lines)


def slack_cases(items: Sequence[SlackItem], *, drive_only: bool = False) -> list[SearchHit]:
    return [
        SearchHit(
            chunk_id=-10000 - i,
            content=item.text,
            score=1.0,
            metadata={
                "title": item.text.splitlines()[0][:100] if item.text else "Slackの事例記録",
                "source_uri": item.hit.permalink,
                "source_type": "slack",
                "file_names": list(item.original_file_names or item.hit.file_names),
                "slack_file_ids": item.hit.file_ids,
                **({drive_first.REFERENCE_KEY: item.hit.reference} if item.hit.reference else {}),
            },
        )
        for i, item in enumerate(items)
        if not drive_only or not re.search(r"営業フィードバック|営業FB", item.hit.channel)
    ]
