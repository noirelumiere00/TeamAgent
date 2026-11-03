"""Strict, source-grounded inference and an evaluation-bound application policy."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PROMPT_VERSION: Literal["v1"] = "v1"
DEFAULT_MODEL = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
Name = Annotated[str, Field(min_length=1, max_length=500, pattern=r"\S")]
Score = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Case(StrictModel):
    advertiser: Name
    campaign: Name


class Post(StrictModel):
    caption: str = ""
    hashtags: list[str] = Field(default_factory=list)
    mentions: list[str] = Field(default_factory=list)
    account_name: str = ""
    account_handle: str = ""
    posted_at: str = ""
    advertiser: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Post:
        caption = row.get("caption") or ""
        return cls(
            caption=caption,
            hashtags=row.get("hashtags") or [],
            mentions=re.findall(r"@[\w.]+", caption),
            account_name=row.get("account_name") or "",
            account_handle=row.get("account_handle") or "",
            posted_at=row["posted_at"].isoformat() if row.get("posted_at") else "",
            advertiser=row.get("advertiser") or None,
        )


class Evidence(StrictModel):
    field: Literal["caption", "hashtags", "mentions"]
    quote: Annotated[str, Field(min_length=1, max_length=500, pattern=r"\S")]


class Prediction(StrictModel):
    advertiser: Name | None
    campaign: Name | None
    match_type: Literal["existing", "new", "unknown"]
    confidence: Score
    evidence: Annotated[list[Evidence], Field(max_length=10)]


_FENCE = re.compile(r"\A\s*```(?:json)?\s*\n(.*)\n\s*```\s*\Z", re.S)


def validate_prediction(text: str, post: Post, cases: list[Case]) -> Prediction:
    # Haiku often wraps the JSON in a ```json fence; only a whole-text fence is unwrapped.
    fenced = _FENCE.match(text)
    result = Prediction.model_validate_json(fenced.group(1) if fenced else text)
    pairs = {(c.advertiser, c.campaign) for c in cases}
    if post.advertiser and result.advertiser not in (None, post.advertiser):
        raise ValueError("known_advertiser_conflict")
    if result.campaign and not result.advertiser:
        raise ValueError("campaign_without_advertiser")
    pair = (result.advertiser, result.campaign)
    if result.match_type == "existing" and pair not in pairs:
        raise ValueError("nonexistent_case")
    if result.match_type == "unknown":
        if result.campaign or (
            result.advertiser and result.advertiser not in {c.advertiser for c in cases}
        ):
            raise ValueError("unknown_case")
    if result.match_type == "new" and (not result.advertiser or pair in pairs):
        raise ValueError("invalid_new_case")
    if result.advertiser and not result.evidence:
        raise ValueError("missing_evidence")
    tags = {_ground(t).lstrip("#") for t in post.hashtags}
    text = _ground(post.caption + "".join(f"#{t}" for t in post.hashtags) + "".join(post.mentions))
    for evidence in result.evidence:
        # Quotes may differ from the stored text only in width, case, spacing and the '#' prefix.
        quote = _ground(evidence.quote)
        words = [w.lstrip("#") for w in unicodedata.normalize("NFKC", evidence.quote).split()]
        if quote not in text and not (words and all(_ground(w) in tags for w in words)):
            raise ValueError("ungrounded_quote")
    return result


def _ground(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def application_prediction(result: Prediction, cases: list[Case]) -> Prediction:
    """Keep a multi-campaign match only when the quotes identify exactly that campaign."""
    if result.match_type != "existing":
        return result
    campaigns = {c.campaign for c in cases if c.advertiser == result.advertiser}
    if len(campaigns) <= 1:
        return result
    quoted = {
        name
        for name in campaigns
        if any(_ground(name) in _ground(e.quote) for e in result.evidence)
    }
    if quoted == {result.campaign}:
        return result
    return result.model_copy(update={"campaign": None, "match_type": "unknown"})


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def catalog_hash(cases: list[Case]) -> str:
    return digest(sorted((c.advertiser, c.campaign) for c in cases))


class Gate(StrictModel):
    threshold: Score
    selected: Annotated[int, Field(ge=30)]
    correct: Annotated[int, Field(ge=0)]

    def passed(self) -> bool:
        return self.selected >= self.correct >= math.ceil(self.selected * 0.97)


class Policy(StrictModel):
    application_version: Literal["v2"]
    model: str
    prompt_version: Literal["v1"]
    prompt_hash: str
    catalog_hash: str
    evaluation_hash: str
    existing: Gate | None = None
    advertiser_only: Gate | None = None

    def compatible(self, model: str, prompt: str, cases: list[Case]) -> bool:
        return (
            self.model == model
            and self.prompt_hash == digest(prompt)
            and self.catalog_hash == catalog_hash(cases)
            and bool(self.evaluation_hash)
        )

    def status(self, result: Prediction) -> Literal["applied", "candidate", "unknown"]:
        if result.match_type == "new":
            return "candidate"
        gate = self.existing if result.campaign else self.advertiser_only
        if result.advertiser and gate and gate.passed() and result.confidence >= gate.threshold:
            return "applied"
        return "unknown"


def input_hash(
    post: Post, cases: list[Case], model: str, prompt: str, policy: Policy | None
) -> str:
    return digest(
        [
            post.model_dump(),
            catalog_hash(cases),
            model,
            digest(prompt),
            policy.model_dump() if policy else None,
        ]
    )
