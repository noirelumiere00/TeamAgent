"""既存の upsert 入力へ根拠束を合成する。外部接続・状態の推測はしない。"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from teamagent.case_state import (
    OBSERVED_METRICS,
    CaseStateEvidence,
    DeliveryObservation,
    SourceRef,
    StateRecord,
    configured,
    enabled,
    source_url,
)

__all__ = [
    "KEY",
    "attach",
    "configured",
    "enabled",
    "input_version",
    "message_reports",
    "source_ref",
]

if TYPE_CHECKING:
    from teamagent.ingest.repository import ChunkUpsert, DocumentUpsert

KEY = "case_state_evidence"
_MANIFEST = Path("data/case_state_reviews.yaml")
JST = timezone(timedelta(hours=9))


def _manifest_bytes() -> bytes:
    return Path(os.environ.get("CASE_STATE_REVIEW_MANIFEST") or _MANIFEST).read_bytes()


def input_version() -> str:
    """差分 skip に判定版と人手確認入力を含める。OFF 時はファイルも読まない。"""
    return "1:" + hashlib.sha256(_manifest_bytes()).hexdigest() if configured() else ""


def _day(value: Any) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo:
            return parsed.astimezone(JST).date().isoformat()
        if len(str(value)) == 10:
            return parsed.date().isoformat()
    except ValueError:
        pass
    return None


def source_ref(doc: DocumentUpsert, chunks: list[ChunkUpsert]) -> SourceRef:
    """同じシート内の集計、同じ deck の別ページも外部 ID と位置で分ける。"""
    meta = doc.metadata
    locator = (
        f"row:{meta['row_idx']}"
        if meta.get("row_idx")
        else f"pages:{meta['case_slide_from']}-{meta['case_slide_to']}"
        if meta.get("case_slide_from")
        else "document"
    )
    payload = {
        "source_type": doc.source_type,
        "external_id": doc.external_id,
        "source_uri": doc.source_uri,
        "title": doc.title,
        "modified_at": doc.modified_at,
        "locator": locator,
        "chunks": [(c.chunk_idx, c.page_num, c.content) for c in chunks],
        "metadata": {
            k: v
            for k, v in meta.items()
            if not k.startswith("cls_")
            and k not in {KEY, "content_sha256"}
            and not (k == "industry" and "cls_industry" in meta)
        },
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()
    return SourceRef(
        source_type=doc.source_type,
        external_id=doc.external_id,
        locator=locator,
        url=source_url(doc.source_uri),
        excerpt="\n".join(c.content for c in chunks)[:500],
        content_sha256=digest,
    )


def message_reports(channel_id: str, external_id: str, messages: list[Any]) -> list[dict[str, Any]]:
    """結合本文を再 parse せず、各返信の値とその投稿時刻・位置を一組で保持する。"""
    from teamagent.ingest.slack_fb_parser import parse_fb_post

    records = []
    for message in messages:
        raw = parse_fb_post(message.text).get("deal_phase")
        if not raw:
            continue
        try:
            posted = datetime.fromtimestamp(float(message.ts), JST).date().isoformat()
        except (ValueError, OverflowError, OSError):
            posted = None
        ref = SourceRef(
            source_type="slack",
            external_id=external_id,
            locator=f"message:{message.ts}",
            url=f"https://slack.com/archives/{channel_id}/p{message.ts.replace('.', '')}",
            excerpt=message.text,
            content_sha256=hashlib.sha256(message.text.encode()).hexdigest(),
        )
        records.append(
            StateRecord(
                authority="reported",
                raw_value=raw,
                source_ref=ref,
                evidence_date=posted,
                evidence_date_kind="message_posted" if posted else "unknown",
            ).model_dump(mode="json")
        )
    return records


def attach(doc: DocumentUpsert, chunks: list[ChunkUpsert]) -> DocumentUpsert:
    if not enabled(doc.owner_email):
        return doc
    ref = source_ref(doc, chunks)
    meta = doc.metadata
    records = [StateRecord.model_validate(r) for r in meta.get("case_state_reports", [])]
    if (
        doc.source_type == "gsheets"
        and not records
        and meta.get("is_sales_fb")
        and meta.get("deal_phase")
    ):
        # Sheets の fallback 処理日時は根拠日として使わない。
        submitted = _day(meta.get("case_state_form_submitted_at"))
        records.append(
            StateRecord(
                authority="reported",
                raw_value=str(meta["deal_phase"]),
                source_ref=ref,
                evidence_date=submitted,
                evidence_date_kind="form_submitted" if submitted else "unknown",
            )
        )
    observation = None
    if meta.get("campaign_aggregate") in (True, "true"):
        try:
            count = int(str(meta.get("video_count") or "0"))
        except ValueError:
            count = 0
        if count > 0:
            observation = DeliveryObservation(
                source_ref=ref,
                video_count=count,
                first_posted_on=_day(meta.get("first_posted_on")),
                last_posted_on=_day(meta.get("last_posted_on")),
                metrics={
                    k: str(meta[k]) for k in OBSERVED_METRICS if meta.get(k) not in (None, "")
                },
            )
    subject = f"source:{ref.source_type}:{ref.external_id}:{ref.locator}"
    manifest = yaml.safe_load(_manifest_bytes())
    if not isinstance(manifest, dict) or set(manifest) != {"version", "reviews"}:
        raise ValueError("invalid case state manifest")
    if (
        type(manifest["version"]) is not int
        or manifest["version"] != 1
        or not isinstance(manifest["reviews"], list)
    ):
        raise ValueError("unsupported case state manifest")
    positions = {ref.locator: "\n".join(c.content for c in chunks)}
    for chunk in chunks:
        positions[f"chunk:{chunk.chunk_idx}"] = chunk.content
        if chunk.page_num is not None:
            key = f"page:{chunk.page_num}"
            positions[key] = positions.get(key, "") + chunk.content
    matched_subjects = set()
    for entry in manifest["reviews"]:
        if not isinstance(entry, dict) or set(entry) != {"subject_ref", "record"}:
            raise ValueError("invalid case state review")
        record = StateRecord.model_validate(entry["record"])
        if (
            record.authority != "official"
            or not isinstance(entry["subject_ref"], str)
            or not entry["subject_ref"].strip()
            or entry["subject_ref"] != entry["subject_ref"].strip()
        ):
            raise ValueError("review must explicitly verify an official state and subject")
        if (record.source_ref.source_type, record.source_ref.external_id) != (
            ref.source_type,
            ref.external_id,
        ):
            continue
        if (
            record.source_ref.content_sha256 != ref.content_sha256
            or record.source_ref.url != ref.url
            or record.source_ref.locator not in positions
            or record.source_ref.excerpt not in positions[record.source_ref.locator]
        ):
            raise ValueError("case state review source revision/position mismatch")
        subject = entry["subject_ref"]
        matched_subjects.add(subject)
        records.append(record)
    if len(matched_subjects) > 1:
        raise ValueError("one source cannot verify multiple subjects")
    bundle = CaseStateEvidence(
        subject_ref=subject,
        source_ref=ref,
        source_modified_at=doc.modified_at,
        document_kind=str(
            meta.get("cls_doc_type")
            or (
                "営業報告"
                if meta.get("is_sales_fb")
                else "事例掲載"
                if meta.get("case_corpus") in (True, "true")
                else "unknown"
            )
        ),
        records=tuple(records),
        delivery_observation=observation,
    )
    return replace(doc, metadata={**meta, KEY: bundle.model_dump(mode="json")})


def main() -> None:
    """取得済み upsert JSON の乾式差分。外部 API・DB・embedding は使わない。"""
    import argparse

    from teamagent.adapters.pgvector_client import SearchHit
    from teamagent.ingest.repository import ChunkUpsert, DocumentUpsert
    from teamagent.skills.search.timeline_status import resolve_statuses

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("input", type=Path, help="document と chunks を持つ配列の JSON")
    args = parser.parse_args()
    entries = json.loads(args.input.read_text())
    if not isinstance(entries, list):
        parser.error("input must be a JSON array")
    changes = []
    for entry in entries:
        doc = DocumentUpsert(**entry["document"])
        if not enabled(doc.owner_email):
            parser.error("document owner is not allowed by CASE_STATE_EVIDENCE_ALLOWED_EMAILS")
        chunks = [ChunkUpsert(embedding=[], **chunk) for chunk in entry["chunks"]]
        prepared = attach(doc, chunks)
        status = resolve_statuses(
            [
                SearchHit(
                    0,
                    "",
                    1.0,
                    {
                        **prepared.metadata,
                        "source_type": doc.source_type,
                        "source_uri": doc.source_uri,
                        "case_state_external_id": doc.external_id,
                    },
                )
            ],
            case_state=True,
        )[0]
        bundle = status.evidence
        assert bundle is not None
        changes.append(
            {
                "source_sha256": bundle.source_ref.content_sha256,
                "changed": doc.metadata.get(KEY) != prepared.metadata[KEY],
                "state": bundle.state,
                "resolution": bundle.resolution,
                "records": [
                    {
                        "state": r.state,
                        "authority": r.authority,
                        "evidence_date": r.evidence_date,
                        "evidence_date_kind": r.evidence_date_kind,
                        "state_as_of": r.state_as_of,
                        "precision": r.state_as_of_precision,
                    }
                    for r in bundle.records
                ],
                "video_count": bundle.delivery_observation.video_count
                if bundle.delivery_observation
                else None,
            }
        )
    print(json.dumps(changes, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
