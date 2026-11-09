"""事例要求は取得した根拠からコードで件数・出典を確定する。"""

import json
import re
from collections.abc import Callable, Sequence
from datetime import date
from urllib.parse import urlsplit

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills._shared.source_url import ingested_slack_file_id, slack_file_permalink
from teamagent.skills.search import drive_first
from teamagent.skills.search.answer_mode import strip_advice
from teamagent.skills.search.answer_sections import (
    _clean,
    _link,
    _safe_url,
    case_external_use_mark,
    entry_from_hit,
)
from teamagent.skills.search.campaign_files import scope_by_industry, video_footer_urls
from teamagent.skills.search.client_match import normalize_client
from teamagent.skills.search.composite import SlackItem
from teamagent.skills.search.not_found import case_target_hits
from teamagent.skills.search.schema import DocumentReference
from teamagent.skills.search.source_attribution import without_source_attribution

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
        message = (
            (parsed.hostname or "") == "slack.com" or (parsed.hostname or "").endswith(".slack.com")
        ) and re.fullmatch(r"/archives/[CDG][A-Z0-9]+/p[0-9]{16}", parsed.path)
        allowed = (
            label == "資料を見る"
            and (drive_first.drive_file_id(safe) or slack_file_permalink(safe) or message)
        ) or (label == "会話の記録" and message)
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
        without_source_attribution(text),
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
            or re.search(r"[？?]|ますか|ください|お知らせ|見つかったのは", part)
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
    requested_count: int | None = None,
    asked_industry: str | None = None,
    client: str | None = None,
    summarize: Callable[[list[SearchHit]], str] | None = None,
) -> str:
    max_cases = requested_count or max_cases
    lines: list[str] = []
    separator = (
        "\n\n"
        if cards or any((h.metadata or {}).get(drive_first.REFERENCE_KEY) for h in hits)
        else "\n"
    )
    seen: set[tuple[str, str]] = set()
    seen_cases: set[str] = set()
    selected: list[SearchHit] = []
    rendered: list[list[str]] = []
    if summarize is not None:
        hits = scope_by_industry(
            sorted(hits, key=lambda h: h.score, reverse=True), asked_industry, soft=True
        )
        hits = case_target_hits(hits, client)
        cards = True
        separator = "\n\n"
    for hit in case_target_hits(hits, target, soft_industry=drive_only or summarize is not None):
        meta = hit.metadata or {}
        entry = entry_from_hit(hit, url=url_of(hit))
        url = _safe_url(entry.url)
        link_label = "資料"
        ref = (hit.metadata or {}).get(drive_first.REFERENCE_KEY)
        if (
            cards
            or drive_only
            or meta.get("source_type") == "slack"
            or ingested_slack_file_id(meta)
        ) and ref is None:
            ref = drive_first.canonical_reference(hit.metadata or {}, hit.content)
        if drive_only and (
            ref is None
            or (ref.kind not in {"drive", "slack_attachment"})
            or not case_link(ref.url, "資料を見る")
            or entry.kind in {"karte", "research", "deal"}
            or str(meta.get("is_sales_fb") or "").lower() in {"true", "1", "yes"}
            or re.search(r"営業フィードバック|営業FB", str(meta.get("channel_name") or ""))
        ):
            continue
        if ref is not None:
            url = _safe_url(ref.url)
            link_label = drive_first.label(ref)
            if requested_count is not None:
                if ref.kind in {"drive", "slack_attachment", "slack_conversation"}:
                    if not case_link(url, "資料を見る"):
                        continue
                else:
                    ref = None
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
        if summarize is not None and ref is None:
            continue
        key = (entry.document_id or url or "", entry.title)
        if ref is not None and ref.drive_file_id:
            key = (ref.drive_file_id, entry.title if entry.kind == "case" else "")
        if requested_count is not None and url:
            key = (url, entry.title if entry.kind == "case" else "")
        case_key = ""
        if summarize is not None:
            assert ref is not None  # summarize ありで ref なしは上で continue 済み
            # 同じ資料はIDで、別資料になった同じ案件は取引先＋施策名でまとめる。
            key = (ref.drive_file_id or ref.slack_file_id or url or entry.document_id or "", "")
            project = meta.get("campaign") or meta.get("case_product") or meta.get("case_brand")
            owner = meta.get("advertiser") or meta.get("case_client") or meta.get("client_name")
            case_key = normalize_client(f"{owner} {project}") if owner and project else ""
            if case_key and case_key in seen_cases:
                continue
        if (not url and ref is None) or key in seen or entry.kind in {"karte", "research"}:
            continue
        seen.add(key)
        if case_key:
            seen_cases.add(case_key)
        if ref is not None:
            details = (
                []
                if summarize is not None
                else _details(str(meta.get("case_effect") or hit.content))
            )
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
                card.append("• " + (case_link(url, "資料を見る") or "資料URL未確認"))
            else:
                card.append(f"• {drive_first.label(ref)}（資料URL未確認）")
            line = "\n".join(card)
            if summarize is not None:
                selected.append(hit)
                rendered.append(card)
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
        if requested_count is None and len(separator.join([*lines, line])) > 2500:
            continue
        lines.append(line)
        if displayed_refs is not None and ref is not None:
            displayed_refs.append(ref)
        if len(lines) == max_cases:
            break
    answer = separator.join(lines)
    if selected and summarize is not None:
        try:
            points = json.loads(summarize(selected))
        except Exception:
            points = {}
        if not isinstance(points, dict):
            points = {}
        for index, card in enumerate(rendered, 1):
            text = points.get(str(index), "")
            if isinstance(text, str):
                card[1:1] = [f"• {line}" for line in _details(text)[: 4 - len(card)]]
        answer = separator.join("\n".join(card) for card in rendered)
    if lines and requested_count is not None and len(lines) < requested_count:
        answer += f"\n\n見つかったのは {len(lines)} 件です。"
    return answer


def slack_cases(items: Sequence[SlackItem], *, drive_only: bool = False) -> list[SearchHit]:
    return [
        drive_first.slack_hit(item, i)
        for i, item in enumerate(items)
        if not drive_only or not re.search(r"営業フィードバック|営業FB", item.hit.channel)
    ]
