"""Production sales25 stages; only immutable references cross parent checkpoints."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import threading
import time
import uuid
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, cast

import structlog

from teamagent.adapters.gemini_client import GeminiClient, GeminiResponse
from teamagent.adapters.media_job import MediaJobClient, MediaJobError
from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.adapters.tiktok_search_fallback import apply_sales25_fallback
from teamagent.media.contracts import MAX_INPUT_BYTES, S3ObjectRef
from teamagent.skills.first_visit import sales25_analysis as analysis
from teamagent.skills.first_visit import sales25_authoring as authoring
from teamagent.skills.first_visit import sales25_contract as contract
from teamagent.skills.omiyage_report.sales25 import (
    Artifact,
    MachineChecks,
    Stages,
    StepResult,
    search_depth,
)

logger = structlog.get_logger(__name__)

_SEARCH_SLOT_NAMES = ("dedup_sales25_search_slot_0", "dedup_sales25_search_slot_1")
# shortcut: one 128 MiB snapshot reserves 16 MiB for metadata; split transfers for larger cases.
_SNAPSHOT_METADATA_RESERVE = 16 * 1024 * 1024


def _limit(name: str, default: int, minimum: int, maximum: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} is outside its allowed range")
    return value


def pack_run(root: Path, owner_id: str) -> bytes:
    """Bounded, deterministic transfer snapshot; the worker seals authored inputs separately."""
    paths = sorted(p for p in root.rglob("*") if p.is_file())
    if sum(p.stat().st_size for p in paths) > MAX_INPUT_BYTES:
        raise ValueError("sales25 run exceeds transfer size limit")
    manifest: dict[str, Any] = {"schema_version": 1, "owner_id": owner_id, "files": {}}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            relative = path.relative_to(root).as_posix()
            if relative == "wire-manifest.json":
                raise ValueError("reserved transfer manifest")
            data = contract.local_path(root, relative).read_bytes()
            manifest["files"][relative] = {
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
            archive.writestr(zipfile.ZipInfo(relative), data)
        archive.writestr(zipfile.ZipInfo("wire-manifest.json"), contract.json_bytes(manifest))
    if output.tell() > MAX_INPUT_BYTES:
        raise ValueError("sales25 run exceeds transfer size limit")
    return output.getvalue()


def _model_identity(value: Any) -> Any:
    if isinstance(value, bytes):
        return hashlib.sha256(value).hexdigest()
    if isinstance(value, (list, tuple)):
        return [_model_identity(item) for item in value]
    if isinstance(value, dict):
        return {key: _model_identity(item) for key, item in value.items()}
    return value


class _Model:
    """Cache model evidence in the same case folder, including across interrupted stages."""

    def __init__(self, wire: Wire, case: dict[str, Any], step: str) -> None:
        self.wire, self.case, self.step = wire, case, step
        self.calls = 0
        self.cost_usd = 0.0
        self.maximum = _limit("OMIYAGE_SALES25_MODEL_CALLS_PER_STAGE", 2000, 1, 20000)
        self.local = threading.local()
        self.lock = threading.Lock()

    def __getattr__(self, method: str) -> Callable[..., GeminiResponse]:
        if method not in {
            "generate_text",
            "analyze_image_bytes",
            "analyze_images_bytes",
            "compare_image_bytes",
            "analyze_audio_bytes",
            "analyze_video_bytes",
        }:
            raise AttributeError(method)

        def invoke(*args: Any, **kwargs: Any) -> GeminiResponse:
            self.wire._remaining(self.case)
            with self.lock:
                if self.calls >= self.maximum:
                    raise TimeoutError("sales25 model call limit")
                self.calls += 1
            identity = contract.json_bytes(
                {
                    "method": method,
                    "args": _model_identity(args),
                    "kwargs": _model_identity(kwargs),
                    "model": os.getenv("GEMINI_MODEL_ID", ""),
                }
            )
            name = "model-" + hashlib.sha256(identity).hexdigest() + ".json"
            cached = self.wire._find_json(self.case, name)
            if cached is None:
                if not hasattr(self.local, "client"):
                    self.local.client = self.wire.model_factory()
                response = cast(GeminiResponse, getattr(self.local.client, method)(*args, **kwargs))
                self.wire._save_json(self.case, name, asdict(response))
            else:
                response = GeminiResponse(**cached)
            with self.lock:
                self.cost_usd += response.cost_usd
            return response

        return invoke


class Wire:
    def __init__(
        self,
        client: MediaJobClient | None = None,
        model_factory: Callable[[], GeminiClient] = GeminiClient.from_env,
        *,
        store: ProposalJobStore | None = None,
    ) -> None:
        self.client = client or MediaJobClient()
        self.model_factory = model_factory
        self.store = store or ProposalJobStore()
        self.post_locks: dict[str, Any] = {}
        self.post_locks_guard = threading.Lock()
        self.post_workers = threading.BoundedSemaphore(
            _limit("OMIYAGE_SALES25_POST_CONCURRENCY", 2, 1, 4)
        )

    @staticmethod
    def _case_id(case: dict[str, Any]) -> str:
        identity = contract.json_bytes([case["owner_id"], case["job_id"]])
        return "mj_" + hashlib.sha256(identity).hexdigest()[:24]

    @staticmethod
    def _remaining(case: dict[str, Any]) -> int:
        duration = _limit("OMIYAGE_SALES25_PARENT_SECONDS", 14400, 300, 86400)
        remaining = int(
            datetime.fromisoformat(case["started_at"]).timestamp() + duration - time.time()
        )
        if remaining <= 0:
            raise TimeoutError("sales25 parent deadline")
        return remaining

    def _deadline(self, case: dict[str, Any]) -> int:
        return int(time.time()) + min(900, self._remaining(case))

    def _download(self, case: dict[str, Any], reference: dict[str, Any]) -> bytes:
        if reference["owner_id"] != case["owner_id"]:
            raise ValueError("sales25 evidence owner mismatch")
        ref = S3ObjectRef.model_validate(reference["object"])
        data = self.client.download(ref, deadline_epoch_s=self._deadline(case))
        if len(data) != ref.size or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise ValueError("sales25 evidence hash/size mismatch")
        return data

    def _save(
        self, case: dict[str, Any], name: str, data: bytes, content_type: str
    ) -> dict[str, Any]:
        ref = self.client.stage_bytes(
            job_id=self._case_id(case),
            name=name,
            body=data,
            content_type=content_type,
            deadline_epoch_s=self._deadline(case),
        )
        return {"owner_id": case["owner_id"], "object": ref.model_dump(mode="json")}

    def _save_json(self, case: dict[str, Any], name: str, data: Any) -> dict[str, Any]:
        return self._save(
            case,
            name,
            contract.json_bytes({"owner_id": case["owner_id"], "data": data}),
            "application/json",
        )

    def _json(self, case: dict[str, Any], reference: dict[str, Any]) -> Any:
        payload = json.loads(self._download(case, reference))
        if payload["owner_id"] != case["owner_id"]:
            raise ValueError("sales25 payload owner mismatch")
        return payload["data"]

    def _find_json(self, case: dict[str, Any], name: str) -> Any:
        ref = self.client.find_staged(
            job_id=self._case_id(case), name=name, deadline_epoch_s=self._deadline(case)
        )
        return (
            self._json(case, {"owner_id": case["owner_id"], "object": ref.model_dump(mode="json")})
            if ref
            else None
        )

    def run(
        self, stage: str, case: dict[str, Any], previous: dict[str, StepResult], step_key: str
    ) -> StepResult:
        name = step_key.split(":")[-1].replace("_", "-") + "-result.json"
        started = time.monotonic()
        cached = self._find_json(case, name)
        if cached is not None:
            result = StepResult.model_validate(cached)
        else:
            result = cast(StepResult, getattr(self, stage)(case, previous, step_key))
            self._save_json(case, name, result.model_dump())
        logger.info(
            "sales25_stage_complete",
            job_id=case["job_id"],
            stage=stage,
            duration_s=round(time.monotonic() - started, 3),
            cached=cached is not None,
            status=result.status,
            actual=result.actual,
        )
        return result

    def _claim_search_slot(self, case: dict[str, Any]) -> tuple[str, str, int]:
        return self._claim_acquisition_slot(
            case, _limit("OMIYAGE_SALES25_SEARCH_SECONDS", 780, 780, 900)
        )

    def _claim_acquisition_slot(self, case: dict[str, Any], seconds: int) -> tuple[str, str, int]:
        started = time.monotonic()
        while True:
            timeout = min(self._remaining(case), seconds)
            now = int(time.time())
            target = f"{now + timeout + 60}:{uuid.uuid4().hex}"
            for name in _SEARCH_SLOT_NAMES[
                : _limit("OMIYAGE_SALES25_ACQUIRE_CONCURRENCY", 2, 1, 2)
            ]:
                current = self.store.get_dedup_lock(name)
                previous = current["target_job_id"] if current else None
                if previous and int(previous.split(":", 1)[0]) > now:
                    continue
                if self.store.put_dedup_lock(name, target, expected_target=previous):
                    logger.info(
                        "sales25_acquisition_slot_claimed",
                        job_id=case["job_id"],
                        wait_s=round(time.monotonic() - started, 3),
                        timeout_s=timeout,
                    )
                    return name, target, timeout
            time.sleep(min(1, self._remaining(case)))

    def search(
        self, case: dict[str, Any], previous: dict[str, StepResult], step_key: str
    ) -> StepResult:
        # The parent shares this object only between the six serial searches of this run.
        session = case.get("_search_session", {})
        if session.get("refused"):
            depth = search_depth()
            diagnostics = {"browserSkipped": True, "reason": "previous_browser_refused"}
            result: dict[str, Any] = {
                "browser_skipped": True,
                "media_job_id": MediaJobClient._job_id(f"{step_key}:sales25-apify"),
                "artifacts": [],
                "raw": {
                    "owner_id": case["owner_id"],
                    "query": case["query"],
                    "videos": [],
                    "ok": False,
                    "status": "failed",
                    "error_code": "TIKTOK_BOT_WALL",
                    "stop_reason": "refused",
                    "diagnostics": diagnostics,
                },
                "acquisition_status": {
                    "query": case["query"],
                    "accepted": False,
                    "status": "failed",
                    "target": depth,
                    "actual": 0,
                    "shortfall": depth,
                    "stop_reason": "refused",
                    "diagnostics": diagnostics,
                    "raw_ref": "sales25-raw.json",
                },
            }
        else:
            if "browser_finished" in session:
                pause = 30 - (time.monotonic() - session["browser_finished"])
                if pause > 0:
                    time.sleep(min(pause, self._remaining(case)))
            slot, lease, timeout = self._claim_search_slot(case)
            try:
                result = self.client.search_tiktok_sales25(
                    case["query"],
                    owner_id=case["owner_id"],
                    request_fingerprint=step_key,
                    max_videos=search_depth(),
                    timeout_s=timeout,
                )
            except MediaJobError as exc:
                # A caller timeout does not stop the child; keep its slot until deadline + 60s.
                if "TIMEOUT" in str(exc) or "DEADLINE_EXCEEDED" in str(exc):
                    return StepResult(status="timeout", reason="timeout")
                raise
            finally:
                session["browser_finished"] = time.monotonic()
            self.store.put_dedup_lock(slot, "", expected_target=lease)
            raw = result["raw"]
            if raw.get("stop_reason") in {
                "refused",
                "captcha",
                "cdn_denied",
                "api_error",
                "no_api_response",
            } or raw.get("error_code") in {
                "TIKTOK_BOT_WALL",
                "TIKTOK_CDN_DENIED",
                "TIKTOK_API_ERROR",
            }:
                session["refused"] = True
        result = apply_sales25_fallback(
            result,
            media_client=self.client,
            deadline_s=self._remaining(case),
            request_id=step_key,
        )
        ref = self._save_json(case, step_key.split(":")[-1].replace("_", "-") + ".json", result)
        raw, acquisition = result["raw"], result["acquisition_status"]
        children = [] if result.get("browser_skipped") else [result["media_job_id"]]
        if not acquisition["accepted"]:
            error = str(raw.get("error_code", ""))
            timeout = "TIMEOUT" in error or "DEADLINE_EXCEEDED" in error
            refused = raw.get("stop_reason") in {
                "refused",
                "captcha",
                "cdn_denied",
                "api_error",
                "no_api_response",
            }
            return StepResult(
                status="timeout" if timeout else "refused" if refused else "incomplete",
                reason="timeout" if timeout else "refused" if refused else None,
                refs=[ref],
                child_job_ids=children,
            )
        actual = acquisition["actual"]
        depth = search_depth()
        return StepResult(
            status="zero" if actual == 0 else "partial" if actual < depth else "complete",
            reason="shortfall" if actual < depth else None,
            actual=actual,
            fetched_at=raw["fetched_at"],
            refs=[ref],
            child_job_ids=children,
        )

    @staticmethod
    def _authored_case(case: dict[str, Any]) -> dict[str, Any]:
        groups = [case["brand"], *case["competitors"], *case["keywords"]]
        return {
            **{k: v for k, v in case.items() if not k.startswith("_")},
            "queries": [
                {
                    "query_id": f"q{i}",
                    "query": query,
                    "group": groups[i],
                    "role": "self" if i == 0 else "competitor" if i < 3 else "market_keyword",
                    "aliases": case["brand_synonyms"].get(groups[i], [groups[i]]),
                }
                for i, query in enumerate(case["queries"])
            ],
        }

    def prefetch(
        self, case: dict[str, Any], previous: dict[str, StepResult], step_key: str
    ) -> StepResult:
        """Only brand TOP10s are stable before all six searches; PR selection waits."""
        index = int(step_key.rsplit("_", 1)[1]) - 1
        if index >= 3:
            return StepResult(status="complete")
        authored = self._authored_case(case)
        query = authored["queries"][index]
        raw = self._json(case, previous[f"search_{index + 1}"].refs[0])["raw"]
        model = _Model(self, case, step_key)
        decisions = authoring.judge_query(
            authored, query, raw, owner_id=case["owner_id"], model=cast(GeminiClient, model)
        )
        source = contract._source(query, raw, decisions, case["owner_id"])
        ids = {pid for ranking in contract._rankings(source["posts"]).values() for pid in ranking}
        for post in source["posts"]:
            if post["id"] in ids:
                self._acquire_post(case, post)
        return StepResult(status="complete")

    def relevance(
        self, case: dict[str, Any], previous: dict[str, StepResult], step_key: str
    ) -> StepResult:
        authored_case = self._authored_case(case)
        raw = {
            f"q{i}": self._json(case, previous[f"search_{i + 1}"].refs[0])["raw"] for i in range(6)
        }
        model = _Model(self, case, step_key)
        inputs = authoring.prepare_inputs(
            authored_case, raw, owner_id=case["owner_id"], model=cast(GeminiClient, model)
        )
        inputs["relevance-usage.json"] = {"calls": model.calls, "cost_usd": model.cost_usd}
        ref = self._save_json(case, "relevance.json", inputs)
        with tempfile.TemporaryDirectory(prefix="sales25-selection-") as work:
            root = Path(work).resolve() / "run"
            contract.write_inputs(root, inputs)
            total = len(analysis.selected_posts(root, case["owner_id"]))
        return StepResult(status="complete", refs=[ref], total=total)

    def _acquire_post(self, case: dict[str, Any], post: dict[str, Any]) -> dict[str, Any]:
        # Same ID across companies must agree before reusing an early acquisition.
        exclusion = analysis.media_exclusion_reason(post)
        if exclusion:
            return {"media_missing": exclusion}
        operation = analysis.media_operation(post)
        with self.post_locks_guard:
            lock = self.post_locks.setdefault(
                self._case_id(case) + ":" + post["id"], threading.Lock()
            )
        with lock, self.post_workers:
            name = "media-" + post["id"]
            record = self._find_json(case, name + ".json")
            if record is None:
                slot, lease, timeout = self._claim_acquisition_slot(case, 900)
                try:
                    artifacts = self.client.acquire_sales25_media(
                        operation,
                        request_fingerprint=case["job_id"] + ":media:" + post["id"],
                        timeout_s=timeout,
                    )
                except MediaJobError as exc:
                    # Timed-out children retain their lease until its deadline.
                    if "TIMEOUT" not in str(exc) and "DEADLINE_EXCEEDED" not in str(exc):
                        self.store.put_dedup_lock(slot, "", expected_target=lease)
                    logger.warning(
                        "sales25_post_media_missing", post_id=post["id"], error_message=str(exc)
                    )
                    record = {
                        "media_missing": "媒体不足（動画を取得できませんでした）",
                        "error_code": str(exc),
                    }
                    self._save_json(case, name + ".json", record)
                    return record
                self.store.put_dedup_lock(slot, "", expected_target=lease)
                analysis.validate_media(operation, artifacts)
                record = {
                    filename: self._save(
                        case,
                        name + "-" + filename,
                        body,
                        "application/zip" if filename.endswith("zip") else "application/json",
                    )
                    for filename, body in artifacts.items()
                }
                self._save_json(case, name + ".json", record)
            return cast(dict[str, Any], record)

    @staticmethod
    def _review_name(post: dict[str, Any], manifest: dict[str, Any], batch_size: int) -> str:
        return (
            "review-"
            + hashlib.sha256(
                contract.json_bytes(
                    [
                        analysis.media_operation(post).model_dump(),
                        manifest,
                        batch_size,
                        os.getenv("GEMINI_MODEL_ID", ""),
                    ]
                )
            ).hexdigest()
            + ".json"
        )

    def media(
        self, case: dict[str, Any], previous: dict[str, StepResult], step_key: str
    ) -> StepResult:
        inputs = self._json(case, previous["relevance"].refs[0])
        collected = {}
        snapshot_bytes = sum(len(contract.json_bytes(value)) for value in inputs.values())
        batch_size = _limit("OMIYAGE_SALES25_IMAGE_BATCH_SIZE", 4, 1, 4)
        model = _Model(self, case, step_key)
        mutex = threading.Lock()
        completed = 0
        oversized = threading.Event()
        with tempfile.TemporaryDirectory(prefix="sales25-selection-") as work:
            root = Path(work).resolve() / "run"
            contract.write_inputs(root, inputs)
            selected = analysis.selected_posts(root, case["owner_id"])

            def review(post: dict[str, Any]) -> tuple[str, dict[str, Any]]:
                nonlocal snapshot_bytes, completed
                if oversized.is_set():
                    raise OverflowError("snapshot_size_limit")
                record = self._acquire_post(case, post)
                if "media_missing" in record:
                    with mutex:
                        completed += 1
                        if case.get("_progress"):
                            case["_progress"](completed, len(selected))
                    return post["id"], record
                artifacts = {name: self._download(case, ref) for name, ref in record.items()}
                manifest, files = analysis.validate_media(analysis.media_operation(post), artifacts)
                with mutex:
                    snapshot_bytes += sum(asset["bytes"] for asset in manifest["assets"])
                    snapshot_bytes += len(contract.json_bytes(manifest))
                    if snapshot_bytes > MAX_INPUT_BYTES - _SNAPSHOT_METADATA_RESERVE:
                        oversized.set()
                        raise OverflowError("snapshot_size_limit")
                name = self._review_name(post, manifest, batch_size)
                cached = self._find_json(case, name)
                if cached is None:
                    cached = analysis._review(
                        root,
                        post,
                        manifest,
                        files,
                        cast(GeminiClient, model),
                        batch_size=batch_size,
                    )
                    self._save_json(case, name, cached)
                with mutex:
                    completed += 1
                    if case.get("_progress"):
                        case["_progress"](completed, len(selected))
                return post["id"], record

            try:
                with ThreadPoolExecutor(
                    max_workers=_limit("OMIYAGE_SALES25_POST_CONCURRENCY", 2, 1, 4)
                ) as pool:
                    collected = dict(pool.map(review, selected))
            except OverflowError:
                ref = self._save_json(
                    case,
                    "media-size-limit.json",
                    {
                        "status": "incomplete",
                        "reason": "snapshot_size_limit",
                        "bytes": snapshot_bytes,
                        "limit_bytes": MAX_INPUT_BYTES,
                        "metadata_reserve_bytes": _SNAPSHOT_METADATA_RESERVE,
                    },
                )
                return StepResult(status="incomplete", reason="media_missing", refs=[ref])
        self._save_json(
            case,
            "media-analysis-usage.json",
            {
                "calls": model.calls,
                "cost_usd": model.cost_usd,
            },
        )
        ref = self._save_json(case, "media.json", collected)
        actual = sum("media_missing" not in record for record in collected.values())
        return StepResult(
            status="partial" if actual < len(collected) else "complete",
            reason="media_missing" if actual < len(collected) else None,
            refs=[ref],
            actual=actual,
            total=len(collected),
        )

    def analysis(
        self, case: dict[str, Any], previous: dict[str, StepResult], step_key: str
    ) -> StepResult:
        inputs = self._json(case, previous["relevance"].refs[0])
        media = self._json(case, previous["media"].refs[0])
        model = _Model(self, case, step_key)
        with tempfile.TemporaryDirectory(prefix="sales25-analysis-") as work:
            root = Path(work).resolve() / "run"
            contract.write_inputs(root, inputs)

            def acquire(operation: Any) -> dict[str, bytes]:
                return {
                    name: self._download(case, ref)
                    for name, ref in media[operation.post_id].items()
                }

            result = analysis.analyze_selection(
                root,
                owner_id=case["owner_id"],
                acquire=acquire,
                model=cast(GeminiClient, model),
                concurrency=_limit("OMIYAGE_SALES25_POST_CONCURRENCY", 2, 1, 4),
                batch_size=_limit("OMIYAGE_SALES25_IMAGE_BATCH_SIZE", 4, 1, 4),
                review_cache=lambda post, manifest: self._find_json(
                    case,
                    self._review_name(
                        post, manifest, _limit("OMIYAGE_SALES25_IMAGE_BATCH_SIZE", 4, 1, 4)
                    ),
                ),
                media_missing={
                    pid: record["media_missing"]
                    for pid, record in media.items()
                    if "media_missing" in record
                },
            )
            (root / "analysis-usage.json").write_bytes(
                contract.json_bytes({"calls": model.calls, "cost_usd": model.cost_usd})
            )
            ref = self._save(
                case, "analysis.zip", pack_run(root, case["owner_id"]), "application/zip"
            )
        if any(p["cover"]["status"] != "matched" for p in result["posts"].values()):
            return StepResult(status="incomplete", reason="media_mismatch", refs=[ref])
        partial_audio = any(p["audio"]["status"] == "unmeasured" for p in result["posts"].values())
        missing = result["media_missing"]
        return StepResult(
            status="partial" if missing or partial_audio else "complete",
            reason="media_missing" if missing else "audio_unverified" if partial_audio else None,
            refs=[ref],
            actual=len(result["posts"]),
            total=len(result["posts"]) + len(missing),
        )

    def fill(
        self, case: dict[str, Any], previous: dict[str, StepResult], step_key: str
    ) -> StepResult:
        result = self.client.render_sales25_pptx(
            self._download(case, previous["analysis"].refs[0]),
            owner_id=case["owner_id"],
            request_fingerprint=step_key,
            timeout_s=min(480, self._remaining(case)),
        )
        qa = json.loads(result["files"]["sales25-qa.json"])
        artifacts = {a["name"]: a["object"] for a in result["artifacts"]}
        pptx = Artifact(**artifacts["deck.pptx"], owner_id=case["owner_id"])
        refs = [
            {"owner_id": case["owner_id"], "object": artifacts[name]}
            for name in ("sales25-qa.json", "sales25-qa.zip")
        ]
        return StepResult(
            status="complete",
            reason="shortfall"
            if qa["status"] == "incomplete" or qa.get("fill_limitations")
            else None,
            artifact=pptx,
            refs=refs,
            child_job_ids=[result["media_job_id"]],
        )

    def machine_check(
        self, case: dict[str, Any], previous: dict[str, StepResult], step_key: str
    ) -> StepResult:
        payload = json.loads(self._download(case, previous["fill"].refs[0]))
        checks = MachineChecks.model_validate(payload["checks"])
        artifact = previous["fill"].artifact
        if (
            artifact is None
            or payload["output_sha256"] != artifact.sha256
            or not checks.accepts(artifact.sha256)
        ):
            return StepResult(status="incomplete", reason="fmt_invalid", checks=checks)
        return StepResult(status="complete", checks=checks)


def production_stages(wire: Wire | None = None, *, store: ProposalJobStore | None = None) -> Stages:
    adapter = wire or Wire(store=store)
    return Stages(
        prefetch=adapter.prefetch,
        **{
            name: partial(adapter.run, name)
            for name in (
                "search",
                "relevance",
                "media",
                "analysis",
                "fill",
                "machine_check",
            )
        },
    )
