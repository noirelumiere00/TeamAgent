"""検索で得た根拠だけから施策の状態と日付を決める（DB・LLM は使わない）。"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.search.dates import extract_title_date
from teamagent.skills.search.schema import DocumentReference

JST = timezone(timedelta(hours=9))


def enabled() -> bool:
    return os.environ.get("SEARCH_TIMELINE_STATUS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def day(value: Any) -> str:
    """完全な実在日だけを採用する。時刻付きなら固定 +9 で日付を求める。"""
    raw = str(value or "").strip()
    try:
        if len(raw) == 10:
            return date.fromisoformat(raw).isoformat()
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(JST).date().isoformat()
    except ValueError:
        pass
    return ""


def evidence_date(hit: SearchHit) -> tuple[str, str]:
    meta = hit.metadata or {}
    for key, label in (
        ("last_posted_on", "最終投稿日"),
        ("posted_on", "投稿日"),
        ("occurred_at", "投稿日"),
        ("updated_at", "更新日"),
        ("created_at", "作成日"),
    ):
        found = day(meta.get(key))
        if found:
            if key == "updated_at" and (
                meta.get("source_type") == "slack" or meta.get("is_sales_fb")
            ):
                label = "投稿日"
            return found, label
    # Slack URI の ts は投稿時刻。取り込み時刻で代用しない。
    uri = str(meta.get("source_uri") or "")
    if uri.startswith("slack://"):
        try:
            found = datetime.fromtimestamp(float(uri.rsplit("/", 1)[-1]), JST).date().isoformat()
            return found, "投稿日"
        except (ValueError, OverflowError, OSError):
            pass
    title_date = extract_title_date(str(meta.get("title") or meta.get("file_name") or ""))
    return (title_date, "資料名の日付") if title_date and day(title_date) else ("", "日付不明")


def date_header(hit: SearchHit) -> str:
    found, label = evidence_date(hit)
    meta = hit.metadata or {}
    first = day(meta.get("first_posted_on"))
    period = f", 投稿開始日: {first}" if first else ""
    return f", {label}: {found}{period}" if found else ", 日付不明"


def _norm(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def identity(hit: SearchHit) -> tuple[str, str]:
    meta = hit.metadata or {}
    client = _norm(meta.get("advertiser") or meta.get("client_name") or meta.get("cls_project"))
    campaign = _norm(meta.get("campaign") or meta.get("case_product") or meta.get("proposed_menu"))
    # 取引先だけの一致で別施策を終了扱いにしない。施策不明は資料ごとに分ける。
    if not client or not campaign:
        return client, "unknown:" + str(
            meta.get("document_id") or meta.get("source_uri") or hit.chunk_id
        )
    return client, campaign


@dataclass(frozen=True)
class TimelineStatus:
    label: str
    text: str
    sections: tuple[str, ...]
    source: SearchHit

    @property
    def line(self) -> str:
        return f"{self.label}: {self.text}"


def resolve_statuses(hits: Sequence[SearchHit]) -> tuple[TimelineStatus, ...]:
    grouped: dict[tuple[str, str], list[SearchHit]] = {}
    for hit in hits:
        grouped.setdefault(identity(hit), []).append(hit)
    out: list[TimelineStatus] = []
    for rows in grouped.values():
        latest = max(rows, key=lambda h: evidence_date(h)[0])
        meta = latest.metadata or {}
        label = " / ".join(
            str(v)
            for v in (
                meta.get("advertiser")
                or meta.get("client_name")
                or meta.get("cls_project")
                or "取引先不明",
                meta.get("campaign")
                or meta.get("case_product")
                or meta.get("proposed_menu")
                or "施策不明",
            )
        )
        sections_list: list[str] = []
        if any(
            (h.metadata or {}).get("topic") == "案件決定"
            or "案件決定" in str((h.metadata or {}).get("channel_name") or "")
            for h in rows
        ):
            sections_list.append("deal")
        if any((h.metadata or {}).get("is_sales_fb") in (True, "true") for h in rows):
            sections_list.append("karte")
        sections = tuple(sections_list)
        posted: list[SearchHit] = []
        for hit in rows:
            m = hit.metadata or {}
            if m.get("campaign_aggregate") not in (True, "true"):
                continue
            try:
                count = int(str(m.get("video_count") or "0"))
            except ValueError:
                continue
            if count > 0:
                posted.append(hit)
        if posted:
            # 同じ集計のチャンクを足し上げない。日付不明でも投稿の存在は実施の根拠になる。
            actual = max(posted, key=lambda h: day((h.metadata or {}).get("last_posted_on")))
            m = actual.metadata or {}
            last = day(m.get("last_posted_on")) or "日付不明"
            latest = actual
            text = f"実施済み（投稿 {int(str(m['video_count']))} 本・最終投稿 {last}）"
        else:
            found, _ = evidence_date(latest)
            phase = str(meta.get("deal_phase") or meta.get("cls_phase") or "").strip()
            known = {
                "ヒアリング",
                "ケイパ",
                "初回提案",
                "2回目以降提案",
                "提案",
                "見積",
                "受注",
                "失注",
                "実施",
                "実施済み",
                "報告",
                "終了",
                "進行中",
                "詰めの段階",
            }
            if not phase and meta.get("topic") == "案件決定":
                phase = "受注（案件決定）"
            if found and (phase in known or phase == "受注（案件決定）"):
                text = f"{found} 時点では{phase}（現在の状態は資料からは確認できません）"
            else:
                source = str(
                    meta.get("title") or "営業FB"
                    if meta.get("is_sales_fb")
                    else meta.get("title") or "資料"
                )
                text = (
                    "状態は資料からは確認できません"
                    f"（最新の記録: {found or '日付不明'} の{source}）"
                )
        out.append(TimelineStatus(label, text, sections, latest))
    return tuple(out)


def prompt_block(statuses: Sequence[TimelineStatus]) -> str:
    today = datetime.now(JST).date().isoformat()
    return f"# 時系列の基準\n今日は {today}（JST）\n# コードが決めた状態\n" + "\n".join(
        s.line for s in statuses
    )


def status_answer(
    query: str,
    hits: Sequence[SearchHit],
    url_of: Callable[[SearchHit], str | None],
    displayed_refs: list[DocumentReference] | None = None,
) -> str:
    """現況の問いだけ、RLS下の根拠と同じ日付・リンクをコードで回答に残す。"""
    from teamagent.skills.search import drive_first
    from teamagent.skills.search.case_list import case_link

    own = query.split("\n", 1)[0]
    if not re.search(r"今どう|どうなって|進捗|現況|状況|現状|今の状態|ステータス", own):
        return ""
    named = {
        identity(h)
        for h in hits
        if not identity(h)[1].startswith("unknown:") and identity(h)[1] in _norm(own)
    }
    scoped = [h for h in hits if not named or identity(h) in named]
    lines = []
    for status in resolve_statuses(scoped)[:3]:
        source = status.source
        ref = drive_first.canonical_reference(source.metadata or {}, source.content)
        url = url_of(source)
        link = case_link(url, "資料を見る" if ref.kind == "drive" else "会話の記録")
        text = status.text
        if not re.search(r"\d{4}-\d{2}-\d{2} 時点では", text):
            found, _ = evidence_date(source)
            prefix = f"{found} 時点では" if found else "日付不明: "
            if text.startswith("実施済み"):
                text += "（現在の状態は資料からは確認できません）"
            text = prefix + text
        lines.append(
            f"{status.label}: {text}" + (f"\n{link}" if link else "\n根拠リンクは確認できません。")
        )
        if (
            link
            and ref.kind == "drive"
            and displayed_refs is not None
            and ref not in displayed_refs
        ):
            displayed_refs.append(ref)
    return "\n\n".join(lines)
