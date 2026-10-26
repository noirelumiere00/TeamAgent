"""Flagged sales25 intake, resumable stages and owner-bound delivery."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.media.deck_qa import DeckQaReport
from teamagent.skills._shared.deck_review import format_slides
from teamagent.skills._shared.long_jobs import _OWNER_KEY, origin
from teamagent.skills._shared.rollout import rollout_allowed
from teamagent.skills.base import ASYNC_JOB_POLL_METADATA_KEY, SkillContext
from teamagent.skills.omiyage_report import input_check, omakase
from teamagent.skills.omiyage_report.schema import (
    MissingField,
    OmiyageReportResult,
    OmiyageReportStatusOutput,
    OmiyageReportSubmitInput,
    OmiyageReportSubmitOutput,
    Sales25Intake,
)

logger = structlog.get_logger(__name__)

EDITION = "sales25"
STAGES = ("relevance", "media", "analysis", "fill", "machine_check", "page_qa")
LABELS = {
    "intake": "受付",
    "search": "検索",
    "relevance": "関連判定",
    "media": "媒体",
    "analysis": "分析",
    "fill": "差し込み",
    "machine_check": "機械検査",
    "page_qa": "全25頁QA",
    "delivery": "配信",
}
PPTX_TYPE: Final = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def search_concurrency() -> int:
    value = int(os.getenv("OMIYAGE_SALES25_SEARCH_CONCURRENCY", "2"))
    if not 1 <= value <= 2:
        raise ValueError("sales25 search concurrency outside 1..2")
    return value


def time_estimate() -> str:
    return "標準設定の目安は約35〜45分（暫定、混雑・長尺時は約90分以上）です。"


def enabled(metadata: Mapping[str, Any]) -> bool:
    allowed = os.getenv("OMIYAGE_SALES25_ALLOWED_EMAILS", "").strip()
    return (
        os.getenv("USE_OMIYAGE_SALES25", "").strip().lower() in {"1", "true", "yes"}
        and metadata.get("identity_verified") is True
        and bool(metadata.get("verified_slack_user_id"))
        and bool(metadata.get("verified_slack_team_id"))
        and bool(allowed)
        and (
            "*" in {email.strip() for email in allowed.split(",")}
            or rollout_allowed("OMIYAGE_SALES25_ALLOWED_EMAILS", metadata.get("user_email"))
        )
    )


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bucket: str = Field(min_length=1)
    key: str = Field(min_length=1)
    version_id: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(gt=0, le=50 * 1024 * 1024, strict=True)
    content_type: Literal[
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ]
    owner_id: str = Field(min_length=1)

    @field_validator("version_id")
    @classmethod
    def non_null_version(cls, value: str) -> str:
        if value.strip().lower() == "null" or not value.strip():
            raise ValueError("a real S3 version is required")
        return value


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["passed", "failed", "incomplete"]
    checked_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: Literal["audio_unverified", "shortfall"] | None = None


class DeckQaCheck(CheckResult):
    error_count: int = Field(ge=0, strict=True)
    review_pages: list[Annotated[int, Field(ge=1, le=25, strict=True)]] = Field(max_length=25)

    @classmethod
    def from_report(cls, report: DeckQaReport, checked_sha256: str) -> DeckQaCheck:
        # summary() truncates findings to five; the DM must retain every warning page.
        return cls(
            status="failed" if report.error_count else "passed",
            checked_sha256=checked_sha256,
            error_count=report.error_count,
            review_pages=sorted({f.slide for f in report.findings if f.severity == "warn"}),
        )


class MachineChecks(BaseModel):
    model_config = ConfigDict(extra="forbid")
    check_fmt: CheckResult
    check_top5_analysis: CheckResult
    deck_qa_l1: DeckQaCheck
    text_fit: CheckResult

    def accepts(self, sha256: str) -> bool:
        analysis = self.check_top5_analysis
        return (
            all(
                check.checked_sha256 == sha256
                for check in (self.check_fmt, analysis, self.deck_qa_l1, self.text_fit)
            )
            and self.check_fmt.status == "passed"
            and self.deck_qa_l1.status == "passed"
            and self.deck_qa_l1.error_count == 0
            and self.text_fit.status == "passed"
            and (
                analysis.status == "passed"
                or (
                    analysis.status == "incomplete"
                    and analysis.reason in {"audio_unverified", "shortfall"}
                )
            )
        )


class StepResult(BaseModel):
    """Small references only; adapters must reuse step_key for child-job idempotency."""

    model_config = ConfigDict(extra="forbid")
    status: Literal[
        "complete", "partial", "incomplete", "refused", "timeout", "zero", "not_available"
    ]
    refs: list[dict[str, Any]] = Field(default_factory=list, max_length=6)
    child_job_ids: list[str] = Field(default_factory=list, max_length=6)
    actual: int | None = Field(default=None, ge=0, le=100)
    total: int | None = Field(default=None, ge=0, le=100)
    reason: (
        Literal[
            "not_connected",
            "shortfall",
            "refused",
            "timeout",
            "observed_zero",
            "media_mismatch",
            "media_missing",
            "analysis_incomplete",
            "audio_unverified",
            "fmt_invalid",
            "render_invalid",
        ]
        | None
    ) = None
    fetched_at: str = ""
    artifact: Artifact | None = None
    checked_sha256: str = ""
    reviewed_pages: list[int] = Field(default_factory=list, max_length=25)
    checks: MachineChecks | None = None

    @field_validator("fetched_at")
    @classmethod
    def acquisition_time(cls, value: str) -> str:
        if value:
            datetime.fromisoformat(value)
        return value


Step = Callable[[dict[str, Any], dict[str, StepResult], str], StepResult]


def not_connected(
    case: dict[str, Any], previous: dict[str, StepResult], step_key: str
) -> StepResult:
    return StepResult(status="incomplete")


def rendering_not_available(
    case: dict[str, Any], previous: dict[str, StepResult], step_key: str
) -> StepResult:
    return StepResult(status="not_available", reason="not_connected")


@dataclass(frozen=True)
class Stages:
    search: Step = not_connected
    relevance: Step = not_connected
    media: Step = not_connected
    analysis: Step = not_connected
    fill: Step = not_connected
    machine_check: Step = not_connected
    page_qa: Step = rendering_not_available
    prefetch: Step | None = None


def can_continue(key: str, result: StepResult) -> bool:
    if key.startswith("search_"):
        return result.status in {"complete", "partial", "zero"}
    if key == "page_qa":
        return result.status in {"complete", "not_available"}
    return result.status == "complete" or (
        key in {"relevance", "media", "analysis", "machine_check"}
        and result.status in {"partial", "incomplete"}
        and result.reason in {"audio_unverified", "shortfall"}
    )


def delivery_checks_pass(steps: dict[str, StepResult], artifact: Artifact | None) -> bool:
    machine = steps.get("machine_check")
    page_qa = steps.get("page_qa")
    return (
        artifact is not None
        and machine is not None
        and machine.checks is not None
        and machine.checks.accepts(artifact.sha256)
        and page_qa is not None
        and (
            page_qa.status == "not_available"
            or (
                page_qa.status == "complete"
                and page_qa.checked_sha256 == artifact.sha256
                and sorted(page_qa.reviewed_pages) == list(range(1, 26))
            )
        )
        and len(steps) == 12
        and all(can_continue(k, r) for k, r in steps.items())
    )


class ParentResult(OmiyageReportResult):
    edition: Literal["sales25"] = "sales25"
    stage: str
    checkpoints: dict[str, StepResult]
    final_pptx_ref: Artifact | None = None
    delivery_status: Literal["pending", "delivered", "failed", "uncertain", "not_ready"]


class ParentStatus(OmiyageReportStatusOutput):
    edition: Literal["sales25"] = "sales25"
    stage: str = "intake"
    checkpoints: dict[str, StepResult] = Field(default_factory=dict)
    final_pptx_ref: Artifact | None = None
    delivery_status: str = "not_ready"


def owner_matches(summary: dict[str, Any], ctx: SkillContext) -> bool:
    if ctx.metadata.get(ASYNC_JOB_POLL_METADATA_KEY) is True:
        return bool(summary.get("owner")) and ctx.metadata.get(_OWNER_KEY) == summary["owner"]
    return (
        ctx.metadata.get("identity_verified") is True
        and ctx.metadata.get("verified_slack_user_id") == summary.get("user_id")
        and ctx.metadata.get("verified_slack_team_id") == summary.get("team_id")
    )


def preflight(
    store: ProposalJobStore, request: OmiyageReportSubmitInput, ctx: SkillContext
) -> tuple[dict[str, Any], OmiyageReportSubmitOutput | None]:
    metadata = ctx.metadata
    scope = [
        metadata.get(k, "")
        for k in (
            "verified_slack_team_id",
            "verified_slack_user_id",
            "channel_id",
            "thread_ts",
            "_sales25_session",
        )
    ]
    lock_id = "dedup_sales25_intake_" + hashlib.sha256(json.dumps(scope).encode()).hexdigest()
    for _ in range(5):
        row = store.get_dedup_lock(lock_id)
        raw = row["target_job_id"] if row else None
        case = json.loads(raw) if raw else {}
        # A new named case in the same conversation must not inherit a previous brand's answers.
        if request.brand and case.get("brand") and request.brand != case["brand"]:
            case = {}
        for name, value in request.model_dump().items():
            if value:
                case[name] = value
                case.get("omakase_values", {}).pop(name, None)
        details = request.sales25 or Sales25Intake()
        for name, value in details.model_dump(exclude_unset=True).items():
            if value:
                case[name] = value
                case.get("omakase_values", {}).pop(name, None)
        omakase.complete(case, input_check.bedrock_text_caller, ctx.request_id)
        if store.put_dedup_lock(lock_id, json.dumps(case, ensure_ascii=False), expected_target=raw):
            break
    else:
        raise RuntimeError("sales25 intake contention")
    missing: list[MissingField] = []
    labels = []
    required_fields: tuple[tuple[MissingField, str], ...] = (
        ("brand", "自社名"),
        ("competitors", "競合2社"),
        ("category", "カテゴリー"),
        ("keywords", "中立ワード3つ"),
        ("target_product", "対象商品"),
        ("claim_constraints", "守る訴求条件（条件なしも明示）"),
    )
    for name, label in required_fields:
        value = case.get(name)
        valid = omakase.known(value)
        if name in {"competitors", "keywords"}:
            valid = (
                isinstance(value, list)
                and len(value) == (2 if name == "competitors" else 3)
                and all(omakase.known(v) for v in value)
            )
        if not valid:
            missing.append(name)
            labels.append(label)
    names = [case.get("brand", ""), *case.get("competitors", []), *case.get("keywords", [])]
    queries = case.get("queries") or names
    if (
        len(queries) != 6
        or any(not omakase.known(q) for q in queries)
        or len({q.strip().casefold() for q in queries}) != 6
        or len({n.strip().casefold() for n in names}) != 6
    ):
        if not missing:
            missing.append("queries")
            labels.append("重複しない実検索語6本（自社・競合2社・中立3語）")
    if missing:
        return case, OmiyageReportSubmitOutput(
            status="needs_input",
            missing=missing,
            message="25頁のお土産資料を作ります。"
            + "、".join(labels)
            + "の未確定の項目を教えてください。候補をこちらで出すこともできます。",
        )
    synonyms = case.get("brand_synonyms", {})
    case["brand_synonyms"] = {
        name: list(
            dict.fromkeys(
                [name, *synonyms.get(name, [])]
                + (["コープ", "COOP", "生協"] if name == "コープデリ" else [])
            )
        )
        for name in names[:3]
    }
    case.update(edition=EDITION, queries=queries, mode=case.get("mode", "research"))
    return case, None


def fingerprint(case: dict[str, Any], owner: str) -> str:
    return (
        "dedup_omiyage_"
        + hashlib.sha256(
            json.dumps(
                {"edition": EDITION, "case": case, "owner": owner},
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
    )


def read_artifact(ref: Artifact) -> bytes:
    import boto3

    response = boto3.client("s3").get_object(
        Bucket=ref.bucket, Key=ref.key, VersionId=ref.version_id
    )
    with response["Body"] as body:
        return bytes(body.read(ref.size + 1))


class Parent:
    def __init__(
        self,
        store: ProposalJobStore,
        *,
        stages: Stages | None = None,
        reader: Callable[[Artifact], bytes] = read_artifact,
        slack: Any = None,
        heartbeat_seconds: int = 30,
    ) -> None:
        self.store = store
        if stages is None:
            from teamagent.skills.omiyage_report.sales25_wire import production_stages

            stages = production_stages(store=self.store)
        self.stages = stages
        self.reader = reader
        self.slack = slack
        self.heartbeat_seconds = heartbeat_seconds

    def execute(self, job_id: str, row: dict[str, Any]) -> None:
        search_workers = search_concurrency()
        summary = json.loads(row["request_summary"])
        old = row.get("checkpoint_json")
        state = (
            json.loads(old)
            if old
            else {"stage": "intake", "steps": summary.get("reused_steps", {})}
        )
        state["lease"] = uuid.uuid4().hex
        current = json.dumps(state, ensure_ascii=False, sort_keys=True)
        if not self.store.save_checkpoint(
            job_id, current, expected_checkpoint=old, expected_updated_at=row["updated_at"]
        ):
            return
        stop = threading.Event()
        state_lock = threading.RLock()

        def heartbeat() -> None:
            while not stop.wait(self.heartbeat_seconds):
                with state_lock:
                    try:
                        if not self.store.heartbeat(job_id, expected_checkpoint=current):
                            return
                    except Exception:
                        return

        thread = None
        if self.heartbeat_seconds:
            thread = threading.Thread(target=heartbeat, daemon=True)
            thread.start()

        def save() -> bool:
            nonlocal current
            updated = json.dumps(state, ensure_ascii=False, sort_keys=True)
            with state_lock:
                saved = self.store.save_checkpoint(job_id, updated, expected_checkpoint=current)
                if saved:
                    current = updated
                return saved

        searches: dict[str, Future[StepResult]] = {}
        prefetches: list[Future[StepResult]] = []
        search_pool = ThreadPoolExecutor(max_workers=search_workers)
        prefetch_pool = ThreadPoolExecutor(max_workers=2)

        def progress(completed: int, total: int) -> None:
            with state_lock:
                state.update(completed=completed, total=total)
                if not save():
                    raise RuntimeError("sales25 checkpoint lease lost")

        def run_search(
            case: dict[str, Any], previous: dict[str, StepResult], key: str
        ) -> StepResult:
            result = self.stages.search(case, previous, key)
            if self.stages.prefetch is not None and can_continue(key.rsplit(":", 1)[1], result):
                future = prefetch_pool.submit(
                    self.stages.prefetch,
                    case,
                    {
                        **previous,
                        key.rsplit(":", 1)[1]: result,
                    },
                    key,
                )
                with state_lock:
                    prefetches.append(future)
            return result

        try:
            steps = {k: StepResult.model_validate(v) for k, v in state["steps"].items()}
            plan = [(f"search_{i + 1}", "search") for i in range(6)] + [(s, s) for s in STAGES]
            for key, stage in plan:
                if key in steps:
                    continue
                # The six search outcomes are all retained, including refused and true zero.
                if stage != "search" and any(not can_continue(k, r) for k, r in steps.items()):
                    break
                if stage == "relevance":
                    # Early work is an optimization; final validation still decides delivery.
                    started = time.monotonic()
                    search_pool.shutdown(wait=True)
                    prefetch_pool.shutdown(wait=True)
                    logger.info(
                        "sales25_prefetch_wait",
                        job_id=job_id,
                        duration_s=round(time.monotonic() - started, 3),
                    )
                    for future in prefetches:
                        try:
                            future.result()
                        except Exception as exc:
                            logger.warning(
                                "sales25_prefetch_incomplete",
                                job_id=job_id,
                                error_type=type(exc).__name__,
                            )
                state["stage"] = key
                state["completed"] = 0
                if stage in {"media", "analysis"}:
                    source = steps.get("relevance" if stage == "media" else "media")
                    state["total"] = source.total if source is not None else None
                    if stage == "analysis" and source is not None:
                        state["completed"] = source.actual or 0
                else:
                    state.pop("total", None)
                if not save():
                    return
                case = {
                    **summary["case"],
                    "owner_id": summary["owner"],
                    "job_id": job_id,
                    "started_at": row["created_at"],
                }
                if stage == "search":
                    case["query"] = case["queries"][int(key.split("_")[1]) - 1]
                    case["target"] = 100
                try:
                    if stage == "search":
                        if not searches:
                            for index, query in enumerate(case["queries"], 1):
                                search_key = f"search_{index}"
                                if search_key not in steps:
                                    searches[search_key] = search_pool.submit(
                                        run_search,
                                        {**case, "query": query},
                                        dict(steps),
                                        f"{job_id}:{search_key}",
                                    )
                        outcome = searches[key].result()
                    else:
                        case["_progress"] = progress
                        outcome = getattr(self.stages, stage)(case, dict(steps), f"{job_id}:{key}")
                    result = StepResult.model_validate(outcome)
                    if len(result.model_dump_json().encode()) > 4096:
                        raise ValueError("stage must return small references, not raw data")
                    if stage == "search" and (
                        result.status in {"complete", "partial", "zero"}
                        or (result.status == "incomplete" and result.reason == "shortfall")
                    ):
                        if (
                            result.actual is None
                            or not result.fetched_at
                            or (result.status == "zero" and result.actual != 0)
                        ):
                            result = StepResult(
                                status="incomplete",
                                refs=result.refs,
                                child_job_ids=result.child_job_ids,
                            )
                        elif result.status == "incomplete" or (
                            result.actual < 100 and result.status == "complete"
                        ):
                            result = result.model_copy(update={"status": "partial"})
                    if stage == "machine_check":
                        artifact = steps.get("fill", StepResult(status="incomplete")).artifact
                        if (
                            artifact is None
                            or result.checks is None
                            or not result.checks.accepts(artifact.sha256)
                        ):
                            result = result.model_copy(
                                update={"status": "incomplete", "reason": "fmt_invalid"}
                            )
                    if stage == "page_qa" and result.status == "complete":
                        artifact = steps.get("fill", StepResult(status="incomplete")).artifact
                        if (
                            artifact is None
                            or result.checked_sha256 != artifact.sha256
                            or sorted(result.reviewed_pages) != list(range(1, 26))
                        ):
                            result = result.model_copy(
                                update={"status": "incomplete", "reason": "render_invalid"}
                            )
                except TimeoutError:
                    result = StepResult(status="timeout")
                except PermissionError:
                    result = StepResult(status="refused")
                except Exception:
                    result = StepResult(status="incomplete")
                steps[key] = result
                state["steps"][key] = result.model_dump()
                if not save():
                    return
            artifact = steps.get("fill", StepResult(status="incomplete")).artifact
            complete = (
                artifact is not None
                and artifact.owner_id == summary["owner"]
                and delivery_checks_pass(steps, artifact)
            )
            machine = steps.get("machine_check")
            checks = machine.checks if machine else None
            analysis_check = checks.check_top5_analysis if checks else None
            partial = any(
                r.status != "complete" or r.reason in {"audio_unverified", "shortfall"}
                for k, r in steps.items()
                if k != "page_qa"
            ) or (
                analysis_check is not None
                and (analysis_check.status == "incomplete" or analysis_check.reason is not None)
            )
            report_status: Literal["ready", "partial", "incomplete"] = (
                "partial" if complete and partial else "ready" if complete else "incomplete"
            )
            stage = "delivery" if complete else state["stage"]
            reasons = {
                "timeout": "時間切れ",
                "refused": "取得拒否",
                "incomplete": "未接続・未確認",
                "partial": "100本目標に未達",
                "zero": "正常0件",
                "not_connected": "工程が未接続",
                "shortfall": "必要件数が不足",
                "observed_zero": "正常0件",
                "media_mismatch": "媒体の同一性不一致",
                "media_missing": "媒体不足",
                "analysis_incomplete": "分析が未完了",
                "audio_unverified": "原音が未確認",
                "fmt_invalid": "機械検査が不合格",
                "render_invalid": "同じ最終PPTXの全25頁QAを確認できません",
            }
            lines = []
            for i, query in enumerate(summary["case"]["queries"], 1):
                search = steps.get(f"search_{i}")
                if search is not None and search.actual is not None and search.actual < 100:
                    lines.append(f"「{query}」100本目標→{search.actual}本取得")
            shortages = [
                f"{LABELS.get(k, k.replace('search_', '検索 '))}: {reasons[r.reason or r.status]}"
                for k, r in steps.items()
                if k != "page_qa" and (r.status in reasons or r.reason in reasons)
            ]
            if analysis_check is not None and (
                analysis_check.status == "incomplete" or analysis_check.reason is not None
            ):
                shortages.append(f"分析検査: {reasons[analysis_check.reason or 'incomplete']}")
            message = (
                "sales25の資料は機械検査を通過しました。"
                if report_status == "ready"
                else "sales25は部分結果（partial）です。不足・未確認事項があります。"
                if complete
                else "sales25は未完成（incomplete）です。完成資料としてはお届けしません。"
            )
            if complete and checks is not None:
                pages = format_slides(checks.deck_qa_l1.review_pages, limit=25)
                visual = (
                    "見た目（描画）は未確認。"
                    if steps["page_qa"].status == "not_available"
                    else "見た目（描画）は全25頁確認済み。"
                )
                message += (
                    "\n"
                    + visual
                    + (
                        f"PowerPoint の左の一覧で {pages}がL1の要確認です"
                        "（確かめてから使ってください）。"
                        if pages
                        else "L1の要確認頁はありません。"
                    )
                )
            next_step = ("不足: " + "、".join(shortages) + "。" if shortages else "") + (
                "本人DMへの添付を確認します。"
                if complete
                else f"再開地点: {LABELS.get(stage, '検索')}。"
                "未確認の工程を接続・確認する必要があります。"
            )
            parent_result = ParentResult(
                status=report_status,
                message=message,
                summary_lines=[" / ".join(lines[i : i + 2]) for i in range(0, len(lines), 2)]
                or ["6語とも100本目標を取得しました。" if complete else "工程を確認中です。"],
                next_step=next_step,
                stage=stage,
                checkpoints=steps,
                final_pptx_ref=artifact,
                delivery_status="pending" if complete else "not_ready",
                delivery_target="dm" if complete else "none",
                spec_version=EDITION,
                pptx_filename="sales25.pptx" if artifact else "",
            )
            state["stage"] = stage
            if save():
                self.store.mark_done(
                    job_id, parent_result.model_dump_json(), expected_checkpoint=current
                )
        finally:
            search_pool.shutdown(wait=True, cancel_futures=True)
            prefetch_pool.shutdown(wait=True, cancel_futures=True)
            stop.set()
            if thread is not None:
                thread.join(timeout=1)

    def prepare_delivery(
        self, job_id: str, summary: dict[str, Any], result: ParentResult, ctx: SkillContext
    ) -> None:
        target = origin(ctx)
        if (
            result.delivery_status != "pending"
            or result.slack_delivered
            or result.final_pptx_ref is None
            or target is None
            or target.pending
            or target.channel_id != summary["channel_id"]
            or target.thread_ts != summary["thread_ts"]
            or target.user_id != summary["user_id"]
        ):
            return
        ref = result.final_pptx_ref
        if not delivery_checks_pass(result.checkpoints, ref):
            raise ValueError("final PPTX checks are missing or invalid")
        if not target.claim_notice("sales25-attachment"):
            return
        if ref.owner_id != summary["owner"]:
            raise ValueError("artifact owner mismatch")
        data = self.reader(ref)
        if len(data) != ref.size or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise ValueError("artifact hash/size mismatch")
        slack = self.slack
        if slack is None:
            from teamagent.adapters.slack_client import SlackClient

            slack = SlackClient.from_env(timeout_seconds=240)
        with tempfile.TemporaryDirectory(prefix="sales25-delivery-") as work:
            path = Path(work) / "sales25.pptx"
            path.write_bytes(data)
            target.defer(
                slack,
                str(path),
                "sales25（編集可能PPTX）",
                result.message
                + "\n取得日: "
                + "、".join(
                    sorted(
                        {
                            s.fetched_at
                            for k, s in result.checkpoints.items()
                            if k.startswith("search_") and s.fetched_at
                        }
                    )
                )
                + "\n"
                + "\n".join(result.summary_lines)
                + "\n"
                + result.next_step,
                ctx.request_id,
                dm_user_id=summary["user_id"],
                thread_ts=summary["thread_ts"] if summary["channel_id"].startswith("D") else None,
            )

    def status(self, job_id: str, row: dict[str, Any], ctx: SkillContext) -> ParentStatus:
        summary = json.loads(row["request_summary"])
        if not owner_matches(summary, ctx):
            return ParentStatus(
                job_id=job_id,
                status="failed",
                error_code="JOB_NOT_FOUND",
                message="そのjob_idは見つかりません。",
            )
        if row["status"] == "failed":
            return ParentStatus(
                job_id=job_id,
                status="failed",
                error_code=row.get("error_code"),
                message="sales25は未完成です。親ジョブの開始・保存状態を確認できませんでした。",
            )
        if row["status"] == "done":
            try:
                result = ParentResult.model_validate_json(row["result_json"])
            except ValueError:
                self.store.mark_failed(
                    job_id,
                    "RESULT_INVALID",
                    expected_statuses=("done",),
                    expected_updated_at=row["updated_at"],
                )
                return ParentStatus(
                    job_id=job_id,
                    status="failed",
                    error_code="RESULT_INVALID",
                    message="保存されたsales25の結果を検証できませんでした。",
                )
            try:
                self.prepare_delivery(job_id, summary, result, ctx)
            except Exception:
                target = origin(ctx)
                if target is not None:
                    target.release_notice("sales25-attachment")
                return ParentStatus(
                    job_id=job_id,
                    status="done",
                    report_status=result.status,
                    result_message=result.message,
                    message="保存済みPPTXの復元を確認できません。再制作はしていません。",
                    stage="delivery",
                    delivery_status="failed",
                )
            return ParentStatus(
                job_id=job_id,
                status="done",
                report_status=result.status,
                result_message=result.message,
                summary_lines=result.summary_lines,
                next_step=result.next_step,
                slack_delivered=result.slack_delivered,
                delivery_target=result.delivery_target,
                message=result.message,
                stage=result.stage,
                checkpoints=result.checkpoints,
                final_pptx_ref=result.final_pptx_ref,
                delivery_status=result.delivery_status,
            )
        state = json.loads(row.get("checkpoint_json") or "{}")
        stage = state.get("stage", "intake")
        label = (
            "検索 " + str(sum(k.startswith("search_") for k in state.get("steps", {}))) + "/6"
            if stage.startswith("search_")
            else LABELS.get(stage, "受付")
        )
        if stage in {"media", "analysis"} and state.get("total") is not None:
            label = "媒体取得・分析" if stage == "media" else label
            label += f" {state.get('completed', 0)}/{state['total']}（ID和集合）"
        return ParentStatus(
            job_id=job_id,
            status=row["status"],
            stage=stage,
            checkpoints=state.get("steps", {}),
            message=f"sales25: {label}。{time_estimate()}",
            retry_after_seconds=30,
        )
