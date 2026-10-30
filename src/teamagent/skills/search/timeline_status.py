"""検索で得た根拠だけから施策の状態と日付を決める（DB・LLM は使わない）。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from calendar import monthrange
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
from typing import Any

from pydantic import ValidationError

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.case_state import CaseStateEvidence, SourceRef, source_url
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
    evidence: CaseStateEvidence | None = None
    evidence_sources: tuple[CaseStateEvidence, ...] = ()

    @property
    def line(self) -> str:
        return f"{self.label}: {self.text}"


def case_state_of(hit: SearchHit) -> CaseStateEvidence:
    """旧行・不正な束は unknown。正式性は分類値や呼び出し側の表示から作らない。"""
    meta = hit.metadata or {}
    try:
        bundle = CaseStateEvidence.model_validate_json(json.dumps(meta.get("case_state_evidence")))
        ref = bundle.source_ref
        if (
            ref.external_id != meta.get("case_state_external_id")
            or ref.source_type != meta.get("source_type")
            or ref.url != source_url(meta.get("source_uri"))
        ):
            raise ValueError("case state source mismatch")
    except (ValidationError, TypeError, ValueError):
        ref = SourceRef(
            source_type=str(meta.get("source_type") or "unknown"),
            external_id=str(
                meta.get("case_state_external_id")
                or meta.get("document_id")
                or meta.get("source_uri")
                or hit.chunk_id
            ),
            locator=f"chunk:{hit.chunk_id}",
            url=source_url(meta.get("source_uri")),
            content_sha256=hashlib.sha256(hit.content.encode()).hexdigest(),
        )
        bundle = CaseStateEvidence(
            subject_ref=f"source:{ref.source_type}:{ref.external_id}:{ref.locator}",
            source_ref=ref,
            document_kind=str(meta.get("cls_doc_type") or "unknown"),
        )
    return bundle.model_copy(
        update={"state": "unknown", "authority": "unverified", "resolution": "unknown"}
    )


def _resolve_case_states(hits: Sequence[SearchHit]) -> tuple[TimelineStatus, ...]:
    grouped: dict[str, dict[str, tuple[SearchHit, CaseStateEvidence]]] = {}
    for hit in hits:
        bundle = case_state_of(hit)
        # 同じ文書の複数チャンクを集計し直さない。順位も認定には使わない。
        key = bundle.source_ref.model_dump_json()
        group = grouped.setdefault(bundle.subject_ref, {})
        if key not in group or (hit.chunk_id, hit.content) < (
            group[key][0].chunk_id,
            group[key][0].content,
        ):
            group[key] = (hit, bundle)
    out = []
    today = datetime.now(JST).date().isoformat()
    labels = {
        "proposed": "提案",
        "won": "受注",
        "in_progress": "進行中",
        "completed": "完了",
        "lost": "失注",
        "cancelled": "中止",
    }
    for subject in sorted(grouped):
        rows = [grouped[subject][key] for key in sorted(grouped[subject])]
        official = [
            (record, hit, bundle)
            for hit, bundle in rows
            for record in bundle.records
            if record.authority == "official"
            and record.state != "unknown"
            and record.state_as_of
            and record.state_as_of <= today
        ]
        hit, evidence = rows[0]
        text = "正式状態は不明（正本未確認）"
        if official:
            # 月と日は区間で比較し、最新区間と重なる食い違いも競合として残す。
            def end(value: str) -> str:
                if len(value) == 7:
                    year, month = map(int, value.split("-"))
                    return f"{value}-{monthrange(year, month)[1]}"
                return value

            latest = max(
                official,
                key=lambda item: (
                    end(item[0].state_as_of or ""),
                    item[2].source_ref.model_dump_json(),
                ),
            )
            record, hit, evidence = latest
            start = (record.state_as_of or "") + (
                "-01" if record.state_as_of_precision == "month" else ""
            )
            states = {r.state for r, _, _ in official if end(r.state_as_of or "") >= start}
            if len(states) > 1:
                evidence = evidence.model_copy(update={"resolution": "conflict"})
                text = "正式状態は不明（根拠が競合）"
            else:
                evidence = evidence.model_copy(
                    update={
                        "state": record.state,
                        "authority": "official",
                        "resolution": "resolved",
                    }
                )
                text = f"{record.state_as_of} 時点の正式状態: {labels[record.state]}"
        meta = hit.metadata or {}
        label = str(meta.get("title") or subject)
        out.append(TimelineStatus(label, text, (), hit, evidence, tuple(b for _, b in rows)))
    return tuple(out)


def resolve_statuses(
    hits: Sequence[SearchHit], *, case_state: bool = False
) -> tuple[TimelineStatus, ...]:
    if case_state:
        return _resolve_case_states(hits)
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


def guard_state_words(text: str, statuses: Sequence[TimelineStatus] = ()) -> str:
    """正式に解決した状態以外の強い断定を最終文から落とす。実績は状態から導かない。"""
    allowed = {
        {"won": "受注", "completed": "完了", "lost": "失注"}.get(s.evidence.state)
        for s in statuses
        if s.evidence is not None and s.evidence.resolution == "resolved"
    }
    return re.sub(r"受注|実績|完了|失注", lambda m: m[0] if m[0] in allowed else "不明", text)


def resolved_evidence(hits: Sequence[SearchHit]) -> dict[int, CaseStateEvidence]:
    """公開ヒットも全可視根拠で解決し、各ヒット自身の出典は維持する。"""
    resolved = {
        s.evidence.subject_ref: s.evidence
        for s in resolve_statuses(hits, case_state=True)
        if s.evidence is not None
    }
    out = {}
    for hit in hits:
        bundle = case_state_of(hit)
        state = resolved[bundle.subject_ref]
        out[hit.chunk_id] = bundle.model_copy(
            update={
                "state": state.state,
                "authority": state.authority,
                "resolution": state.resolution,
            }
        )
    return out


def render_case_states(hits: Sequence[SearchHit]) -> str:
    """自由文を採用せず、同じ対象の状態・根拠日・原出典を一緒に描画する。"""
    statuses = resolve_statuses(hits, case_state=True)
    if not statuses:
        return "正式状態は不明（参照できる根拠なし）。"
    date_labels = {
        "unknown": "種別不明",
        "message_posted": "投稿日",
        "form_submitted": "提出日",
        "document_issued": "資料発行日",
        "reviewed": "確認日",
        "post_published": "公開日",
    }

    def literal(value: str) -> str:
        # 題名・位置は引用であって状態の認定ではない。メンションや改行も持ち込まない。
        return escape(guard_state_words(" ".join(value.split())), quote=False)

    def link(value: str | None) -> str:
        return (
            escape(value, quote=False)
            if value and re.fullmatch(r"https?://[^\s<>|]+", value)
            else "リンク不明"
        )

    blocks = []
    for index, status in enumerate(statuses, 1):
        lines = [f"対象 {index}（資料名: {literal(status.label)}）", status.text]
        for bundle in status.evidence_sources:
            lines.append(f"資料種別: {literal(bundle.document_kind)}")
            if not bundle.records:
                lines.append("根拠日: 不明（状態の記録なし）")
            for record in sorted(bundle.records, key=lambda r: r.model_dump_json()):
                authority = (
                    "確認済み記録" if record.authority == "official" else "報告（正式性未確認）"
                )
                lines.append(
                    f"{authority} / 根拠日（{date_labels[record.evidence_date_kind]}）: "
                    f"{record.evidence_date or '不明'}"
                )
                if record.authority != "official":
                    lines.append(
                        "報告の記載（正式状態ではありません）: "
                        + literal(record.raw_value or "不明")
                    )
                ref = record.source_ref
                lines.append(f"出典: {link(ref.url)} / 位置: {literal(ref.locator)}")
            if not bundle.records:
                ref = bundle.source_ref
                lines.append(f"出典: {link(ref.url)} / 位置: {literal(ref.locator)}")
            if observation := bundle.delivery_observation:
                lines.append(
                    f"投稿観測: {observation.video_count} 本 / "
                    f"公開期間: {observation.first_posted_on or '不明'}"
                    f" 〜 {observation.last_posted_on or '不明'}"
                )
                lines.append(f"投稿の出典: {link(observation.source_ref.url)}")
                lines.extend(
                    f"観測値 {key}: {value}" for key, value in sorted(observation.metrics.items())
                )
        # 案件ごとに検査する。他案件の受注を提案中の案件へ流用しない。
        blocks.append(guard_state_words("\n".join(lines), [status]))
    return "\n\n".join(blocks)


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
