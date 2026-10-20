"""全DM良い率の手動測定。本文はメモリだけ、保存するのは集計と入力digestの判定。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from teamagent.adapters.dm_quality_reader import QualityExchange, QualityExtraction
from teamagent.prompts.loader import load_prompt

HAIKU_MODEL: Final = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
Verdict = Literal["good", "partial", "bad", "no_reply", "not_a_request", "unassessable"]


class Judgement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Verdict
    cost_usd: float = Field(default=0, ge=0, allow_inf_nan=False)


class QualityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    rubric: Literal["rubric_v1"] = "rubric_v1"
    rubric_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_id: Literal["jp.anthropic.claude-haiku-4-5-20251001-v1:0"] = HAIKU_MODEL
    start: datetime
    end: datetime
    observed_until: datetime
    complete: bool
    dm_count: int = Field(ge=0)
    unit_count: int = Field(ge=0)
    user_count: int = Field(ge=0)
    good: int = Field(ge=0)
    partial: int = Field(ge=0)
    bad: int = Field(ge=0)
    no_reply: int = Field(ge=0)
    not_a_request: int = Field(ge=0)
    missing: int = Field(ge=0)
    extraction_failures: int = Field(ge=0)
    request_count: int = Field(ge=0)
    good_rate: float | None = Field(default=None, ge=0, le=1)
    good_rate_ci95: tuple[float, float] | None = None
    cost_usd: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def consistent(self) -> QualityReport:
        if any(d.tzinfo is None for d in (self.start, self.end, self.observed_until)):
            raise ValueError("timezone_required")
        if not self.start < self.end <= self.observed_until:
            raise ValueError("invalid_period")
        requests = self.good + self.partial + self.bad + self.no_reply
        complete = self.missing == self.extraction_failures == 0
        rate = self.good / requests if complete and requests else None
        if (
            self.request_count != requests
            or self.complete != complete
            or self.unit_count != requests + self.not_a_request + self.missing
            or self.good_rate != rate
            or self.good_rate_ci95 != wilson(self.good, requests, complete)
        ):
            raise ValueError("inconsistent_quality_report")
        return self


def wilson(good: int, total: int, complete: bool) -> tuple[float, float] | None:
    if not complete or not total:
        return None
    p, z = good / total, 1.96
    center = p + z * z / (2 * total)
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    scale = 1 + z * z / total
    return (center - margin) / scale, (center + margin) / scale


def summarize(
    extraction: QualityExtraction,
    decisions: dict[str, Judgement],
    *,
    start: datetime,
    end: datetime,
    observed_until: datetime,
) -> QualityReport:
    counts: Counter[str] = Counter(
        decisions[x.input_id].verdict for x in extraction.exchanges if x.input_id in decisions
    )
    missing = len(extraction.exchanges) - sum(counts.values()) + counts["unassessable"]
    complete = missing == len(extraction.failures) == 0
    requests = sum(counts[v] for v in ("good", "partial", "bad", "no_reply"))
    return QualityReport(
        rubric_sha256=hashlib.sha256(
            load_prompt("dm_quality", "v1", "system").encode()
        ).hexdigest(),
        start=start,
        end=end,
        observed_until=observed_until,
        complete=complete,
        dm_count=extraction.dm_count,
        unit_count=len(extraction.exchanges),
        user_count=len({x.person_id for x in extraction.exchanges}),
        good=counts["good"],
        partial=counts["partial"],
        bad=counts["bad"],
        no_reply=counts["no_reply"],
        not_a_request=counts["not_a_request"],
        missing=missing,
        extraction_failures=len(extraction.failures),
        request_count=requests,
        good_rate=counts["good"] / requests if complete and requests else None,
        good_rate_ci95=wilson(counts["good"], requests, complete),
        cost_usd=sum(d.cost_usd for d in decisions.values()),
    )


def _evidence(exchange: QualityExchange) -> str:
    return json.dumps(
        {
            "question": exchange.question,
            "files": exchange.files,
            "answers": exchange.answers,
            "previous_two_utterances": exchange.context,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


class Judge(Protocol):
    model_id: str

    def judge(self, exchange: QualityExchange) -> Judgement: ...


class HaikuJudge:
    def __init__(self, client: Any) -> None:
        if client.model_id != HAIKU_MODEL:
            raise ValueError("haiku_model_required")
        self.client = client
        self.model_id = client.model_id

    def judge(self, exchange: QualityExchange) -> Judgement:
        text = _evidence(exchange)
        # shortcut: 巨大な会話は欠測にする。実データで必要になったら全文分割判定を追加する。
        if len(text.encode()) > 200_000:
            return Judgement(verdict="unassessable")
        response = self.client.converse(
            messages=[{"role": "user", "content": [{"text": text}]}],
            request_id=exchange.input_id,
            system=load_prompt("dm_quality", "v1", "system"),
            temperature=0.1,
            max_tokens=128,
            cache_system=True,
        )
        if response.stop_reason != "end_turn":
            raise ValueError("judgement_incomplete")
        try:
            # モデル自身に費用や自由記述を申告させない。
            raw = json.loads(response.text)
            if not isinstance(raw, dict) or set(raw) != {"verdict"}:
                raise ValueError
            return Judgement(verdict=raw["verdict"], cost_usd=response.usage.cost_usd)
        except (ValueError, TypeError, KeyError):
            raise ValueError("judgement_invalid") from None


def _atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".quality-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_report(path: Path, report: QualityReport) -> None:
    _atomic_json(path, report.model_dump(mode="json"))


def measure(
    extraction: QualityExtraction,
    judge: Judge,
    *,
    start: datetime,
    end: datetime,
    observed_until: datetime,
    checkpoint: Path,
    budget_usd: float,
) -> QualityReport:
    if not math.isfinite(budget_usd) or budget_usd <= 0:
        raise ValueError("invalid_budget")
    rubric = load_prompt("dm_quality", "v1", "system")
    identity = hashlib.sha256(
        (
            "dm-quality-input-digest-v1"
            + rubric
            + judge.model_id
            + start.isoformat()
            + end.isoformat()
            + observed_until.isoformat()
        ).encode()
    ).hexdigest()
    cache = json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
    if cache and cache.get("identity") != identity:
        raise ValueError("checkpoint_version_mismatch")
    entries = cache.get("entries", {})
    decisions: dict[str, Judgement] = {}
    spent = float(cache.get("spent_usd", 0))
    if not math.isfinite(spent) or spent < 0:
        raise ValueError("invalid_checkpoint_cost")
    for exchange in extraction.exchanges:
        evidence = _evidence(exchange)
        # 同じ内容の別発言をまとめず、本文は保存しない。
        digest = hashlib.sha256((exchange.input_id + evidence).encode()).hexdigest()
        previous = entries.get(exchange.input_id)
        if previous and previous["digest"] == digest:
            decisions[exchange.input_id] = Judgement.model_validate(previous["decision"])
            continue
        reserve = (
            len(evidence.encode()) + len(rubric.encode())
        ) * 1.375 / 1_000_000 + 128 * 5.5 / 1_000_000
        if spent + reserve > budget_usd:
            continue
        # 呼出し前に費用を予約。クラッシュ・応答不明時に同じ課金を自動で繰り返さない。
        spent += reserve
        decision = Judgement(verdict="unassessable", cost_usd=reserve)
        entries[exchange.input_id] = {"digest": digest, "decision": decision.model_dump()}
        _atomic_json(checkpoint, {"identity": identity, "entries": entries, "spent_usd": spent})
        try:
            decision = judge.judge(exchange)
        except Exception:
            # 例外やモデル出力の本文は、checkpointにもログにも持ち込まない。
            decisions[exchange.input_id] = Judgement(verdict="unassessable", cost_usd=reserve)
            continue
        spent += decision.cost_usd - reserve
        decisions[exchange.input_id] = decision
        entries[exchange.input_id] = {"digest": digest, "decision": decision.model_dump()}
        _atomic_json(checkpoint, {"identity": identity, "entries": entries, "spent_usd": spent})
    report = summarize(extraction, decisions, start=start, end=end, observed_until=observed_until)
    report.cost_usd = spent
    return report
