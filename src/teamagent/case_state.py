"""案件の根拠束。資料・営業報告・投稿観測と正式状態を混同しない共有契約。"""

from __future__ import annotations

import os
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from teamagent.mcp_gateway.allowlist import email_allowed

State = Literal["unknown", "proposed", "won", "in_progress", "completed", "lost", "cancelled"]
DateKind = Literal[
    "unknown", "message_posted", "form_submitted", "document_issued", "reviewed", "post_published"
]
Metric = Literal[
    "ad_video_count",
    "plays_total",
    "plays_median",
    "plays_max",
    "saves_total",
    "save_rate_median",
    "cost_total",
]
OBSERVED_METRICS: tuple[Metric, ...] = (
    "ad_video_count",
    "plays_total",
    "plays_median",
    "plays_max",
    "saves_total",
    "save_rate_median",
    "cost_total",
)


def configured() -> bool:
    return any(
        s.strip() for s in os.environ.get("CASE_STATE_EVIDENCE_ALLOWED_EMAILS", "").split(",")
    )


def source_url(uri: str | None) -> str | None:
    """内部 source URI を表示リンクへ変換する。本文から別資料を選ばない。"""
    if uri and uri.startswith(("https://", "http://")):
        return uri
    if uri and re.fullmatch(r"gdrive://[A-Za-z0-9_-]+", uri):
        return f"https://drive.google.com/file/d/{uri[9:]}/view"
    if uri and re.fullmatch(r"slack://[A-Z0-9_]+/[0-9]+\.[0-9]+", uri):
        channel, ts = uri[8:].split("/")
        return f"https://slack.com/archives/{channel}/p{ts.replace('.', '')}"
    return None


def enabled(email: object) -> bool:
    return email_allowed(
        email,
        {
            s.strip().lower()
            for s in os.environ.get("CASE_STATE_EVIDENCE_ALLOWED_EMAILS", "").split(",")
            if s.strip()
        },
    )


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SourceRef(EvidenceModel):
    source_type: str = Field(min_length=1)
    external_id: str = Field(min_length=1)
    locator: str = Field(min_length=1)
    url: str | None = None
    excerpt: str = ""
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class StateRecord(EvidenceModel):
    state: State = "unknown"
    authority: Literal["official", "reported", "unverified"] = "unverified"
    raw_value: str = ""
    source_ref: SourceRef
    evidence_date: str | None = None
    evidence_date_kind: DateKind = "unknown"
    state_as_of: str | None = None
    state_as_of_precision: Literal["unknown", "day", "month"] = "unknown"
    reviewed_by: str | None = None
    reviewed_on: str | None = None
    review_ref: str | None = None

    @model_validator(mode="after")
    def validate_dates_and_authority(self) -> StateRecord:
        for value in (self.evidence_date, self.reviewed_on):
            if value is not None and date.fromisoformat(value).isoformat() != value:
                raise ValueError("invalid evidence/review day")
        if (self.evidence_date is None) != (self.evidence_date_kind == "unknown"):
            raise ValueError("evidence date needs its kind")
        if self.state_as_of is None:
            if self.state_as_of_precision != "unknown":
                raise ValueError("missing state_as_of")
        elif self.state_as_of_precision == "month":
            if len(self.state_as_of) != 7:
                raise ValueError("invalid state month")
            date.fromisoformat(self.state_as_of + "-01")
        elif self.state_as_of_precision == "day":
            if date.fromisoformat(self.state_as_of).isoformat() != self.state_as_of:
                raise ValueError("invalid state day")
        else:
            raise ValueError("state_as_of needs precision")
        if self.authority == "official" and not all(
            (
                self.reviewed_by,
                self.reviewed_on,
                self.review_ref,
                self.evidence_date,
                self.state_as_of,
            )
        ):
            raise ValueError("official state requires a dated human review and state time")
        if self.authority == "official" and any(
            not str(v or "").strip()
            for v in (self.reviewed_by, self.review_ref, self.source_ref.excerpt)
        ):
            raise ValueError("official state requires nonblank review and evidence")
        return self


class DeliveryObservation(EvidenceModel):
    source_ref: SourceRef
    video_count: int = Field(ge=1)
    first_posted_on: str | None = None
    last_posted_on: str | None = None
    metrics: dict[Metric, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_period(self) -> DeliveryObservation:
        for value in self.metrics.values():
            try:
                number = Decimal(value)
            except InvalidOperation as exc:
                raise ValueError("invalid observed metric") from exc
            if not number.is_finite() or number < 0:
                raise ValueError("invalid observed metric")
        for posted in (self.first_posted_on, self.last_posted_on):
            if posted is not None and date.fromisoformat(posted).isoformat() != posted:
                raise ValueError("invalid post day")
        if self.first_posted_on and self.last_posted_on:
            if self.first_posted_on > self.last_posted_on:
                raise ValueError("reversed post period")
        return self


class CaseStateEvidence(EvidenceModel):
    version: Literal[1] = 1
    subject_ref: str = Field(min_length=1)
    source_ref: SourceRef
    source_modified_at: str | None = None
    document_kind: str = "unknown"
    state: State = "unknown"
    authority: Literal["official", "reported", "unverified"] = "unverified"
    resolution: Literal["resolved", "unknown", "conflict"] = "unknown"
    records: tuple[StateRecord, ...] = ()
    delivery_observation: DeliveryObservation | None = None

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: Any) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("unsupported case state evidence version")
        return value

    @model_validator(mode="after")
    def bind_sources(self) -> CaseStateEvidence:
        refs = [r.source_ref for r in self.records]
        if self.delivery_observation:
            refs.append(self.delivery_observation.source_ref)
        if any(
            (s.source_type, s.external_id)
            != (self.source_ref.source_type, self.source_ref.external_id)
            for s in refs
        ):
            raise ValueError("evidence belongs to another source")
        if any(
            r.authority == "official"
            and (
                r.source_ref.content_sha256 != self.source_ref.content_sha256
                or r.source_ref.url != self.source_ref.url
            )
            for r in self.records
        ):
            raise ValueError("review must bind the exact source revision")
        if self.delivery_observation and self.delivery_observation.source_ref != self.source_ref:
            raise ValueError("numbers must bind the same source revision")
        return self
