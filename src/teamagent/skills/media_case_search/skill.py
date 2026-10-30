"""Search preloaded campaigns; refresh at most three initialized profiles at a time."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel, ValidationError

from teamagent.adapters.apify_client import ApifyClient, ApifyRunResult
from teamagent.adapters.apify_media_cases import ApifyCaseAssets
from teamagent.adapters.cost_guard import CostGuard, CostLimitExceededError
from teamagent.adapters.embeddings_client import build_embedder_from_env
from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.identity import shared_company_domains_from_env
from teamagent.ingest.media_cases import MediaCaseStore
from teamagent.observability import redact_secrets_and_pii
from teamagent.skills._shared.long_jobs import _OWNER_KEY, enabled, origin
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.media_case_search.campaigns import render_hits
from teamagent.skills.media_case_search.distill import CaseDistiller, clean_text, normalize_post
from teamagent.skills.media_case_search.schema import MediaCaseInput, MediaCaseOutput

logger = structlog.get_logger(__name__)
_LOCK_ID = "dedup_media_case_ingest"
_TERMINAL = {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}


def _post_error_message(exc: Exception) -> str:
    message = str(exc)
    if isinstance(exc, ValidationError):
        # Pydantic's default string also echoes untrusted input, including personal names.
        message = "; ".join(
            f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
            for error in exc.errors(include_input=False, include_context=False, include_url=False)
        )
    return clean_text(redact_secrets_and_pii(message))[:200]


@dataclass
class IngestProgress:
    total: int = 0
    processed: int = 0
    failed: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, *, total: int = 0, failed: bool | None = None) -> None:
        with self._lock:
            self.total += total
            if failed is not None:
                self.processed += 1
                self.failed += int(failed)

    def snapshot(self) -> tuple[int, int, int]:
        with self._lock:
            return self.processed, self.total, self.failed


def accounts() -> dict[str, dict[str, str]]:
    with files("teamagent").joinpath("assets/media_accounts.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != 10:
        raise ValueError("expected ten approved media accounts")
    result = {}
    for row in rows:
        handle = row["handle"]
        if (
            not re.fullmatch(r"[A-Za-z0-9_.]{1,24}", handle)
            or handle in result
            or row["active"] not in {"True", "False"}
        ):
            raise ValueError("invalid media account registry")
        if row["active"] == "True":
            result[handle] = row
    return result


def query_filters(input: MediaCaseInput) -> tuple[str, dict[str, Any]]:
    query = re.sub(
        r"(?i)(?<![a-z])cp(?![a-z])", "キャンペーン", unicodedata.normalize("NFKC", input.query)
    )
    filters: dict[str, Any] = {
        "accounts": input.accounts or list(accounts()),
        "pr_only": input.pr_only or bool(re.search(r"(?i)PR\s*(のみ|だけ)", query)),
        "posted_from": input.posted_from.isoformat() if input.posted_from else None,
        "posted_to": input.posted_to.isoformat() if input.posted_to else None,
    }
    year = re.search(r"\b(20\d{2})年", query)
    if year and not input.posted_from and not input.posted_to:
        filters.update(posted_from=f"{year[1]}-01-01", posted_to=f"{year[1]}-12-31")
    if "旅行" in query:
        filters["categories"] = ["旅行", "宿泊", "交通"]
    else:
        for term, category in (
            ("ホテル", "宿泊"),
            ("美容", "美容"),
            ("食品", "食品"),
            ("飲料", "飲料"),
            ("家電", "家電"),
            ("飲食", "飲食"),
        ):
            if term in query:
                filters["categories"] = [category]
                break
    for term, kind in (
        ("キャンペーン", "campaign"),
        ("新商品", "new_product"),
        ("イベント", "event"),
    ):
        if term in query:
            filters["campaign_type"] = kind
            break
    if "国内" in query:
        filters["geography"] = "domestic"
    elif "海外" in query:
        filters["geography"] = "overseas"
    return query, filters


def _owner(ctx: SkillContext) -> str:
    return str(
        ctx.metadata.get(_OWNER_KEY)
        or hashlib.sha256(str(ctx.metadata.get("user_email") or "").encode()).hexdigest()
    )


def _with_notice(message: str, notice: str) -> str:
    body, footer = message.rsplit("\n※ ", 1)
    return f"{body}\n{notice}\n※ {footer}"


@register
class MediaCaseSearchSkill(BaseSkill[MediaCaseInput, MediaCaseOutput]):
    name: ClassVar[str] = "media_case_search"
    description: ClassVar[str] = (
        "依頼文にトレンドメディア／自社メディアと明示された時だけ使う事例検索"
        "（下記の登録handleの指定も対象、@付き・無しとも可）。枕詞なしの一般事例はsearchのみ。"
        "本ツール使用時はsearchを併用しない。CP=キャンペーン。"
        "queryは枕詞と直後の「での／で／の」を除いた残り。"
        "例: トレンドメディア 国内 旅行 CP → query=国内 旅行 CP。handle指定はaccountsに渡す。"
        "保存済み投稿を案件単位で最大3件に集計。初回は管理者取り込み。古い収録分だけ差分更新。"
        "「動画出して」「まとめて」は共に案件単位の回答＋いちばん伸びた1本の投稿リンク。"
        "回答末尾は収録範囲と次の一手の2行。投稿リンクの説明は繰り返さない。"
        "選択式で聞き返さない。登録handle: " + ", ".join(accounts())
    )
    input_schema: ClassVar[type[BaseModel]] = MediaCaseInput
    output_schema: ClassVar[type[BaseModel]] = MediaCaseOutput
    mcp_relay_fields: ClassVar[tuple[str, ...]] = ("message",)

    def __init__(
        self,
        *,
        store: Any | None = None,
        jobs: ProposalJobStore | None = None,
        apify: ApifyClient | None = None,
        distiller: CaseDistiller | None = None,
        launch: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._store = store
        self.jobs = jobs or ProposalJobStore()
        self._durable_required = jobs is None
        self._apify = apify
        self._distiller = distiller
        self._launch = launch or (
            lambda fn: threading.Thread(target=fn, daemon=True, name="media-case").start()
        )
        self._active: set[str] = set()
        self._active_lock = threading.Lock()

    @property
    def store(self) -> Any:
        if self._store is None:
            self._store = MediaCaseStore(PgVectorClient.from_env(), build_embedder_from_env())
        return self._store

    def _start(self, job_id: str) -> None:
        with self._active_lock:
            if job_id in self._active:
                return
            self._active.add(job_id)
        try:
            self._launch(lambda: self._worker(job_id))
        except Exception:
            with self._active_lock:
                self._active.discard(job_id)
            raise

    def recover(self) -> None:
        if self._durable_required and not self.jobs.uses_dynamodb:
            return
        row = self.jobs.get_dedup_lock(_LOCK_ID)
        if row and row.get("target_job_id"):
            job_id = row["target_job_id"]
            job = self.jobs.get_job(job_id)
            if job and job.get("status") in {"queued", "running"}:
                self._start(job_id)

    def _search(self, input: MediaCaseInput, ctx: SkillContext) -> str:
        query, filters = query_filters(input)
        hits = self.store.search(query, filters, ctx.metadata, input.limit)
        states = [self.store.state(handle) for handle in filters["accounts"]]
        ready = sum(bool(state.get("initialized")) for state in states)
        partial = sum(
            bool(state.get("failed_ids") or state.get("gap") or state.get("error"))
            for state in states
        )
        message = render_hits(hits, filters, input.limit)
        count = self.store.coverage(filters["accounts"], ctx.metadata)
        operators = "・".join(sorted({accounts()[h]["operator"] for h in filters["accounts"]}))
        dates = sorted(
            {str(p["metrics_observed_at"])[:10] for p in hits if p.get("metrics_observed_at")}
        )
        observed = (
            f" 数値は {dates[0]}"
            + (f"〜{dates[-1]}" if dates[0] != dates[-1] else "")
            + " の取得時点。"
            if dates
            else ""
        )
        return (
            message
            + f"\n※ {operators} の {len(states)} アカウント・{count:,} 投稿から"
            + f"（収録 {ready}/{len(states)}）。"
            + ("未収録分は管理者の初回取り込み後に検索できます。" if ready < len(states) else "")
            + (f"うち{partial}件は取得・判定が一部未完了です。" if partial else "")
            + observed
            + "\n施設名やブランド名でも事例を探せます。"
        )

    def run(self, input: MediaCaseInput, ctx: SkillContext) -> MediaCaseOutput:
        if not ctx.metadata.get("identity_verified") or not ctx.metadata.get("user_email"):
            raise PermissionError("verified caller required")
        allowed = accounts()
        if any(handle not in allowed for handle in input.accounts):
            return MediaCaseOutput(
                message="指定のアカウントは今回の収録対象外です。現在は優先10アカウントを対象にしています。"
            )
        message = self._search(input, ctx)
        if self._durable_required and not self.jobs.uses_dynamodb:
            return MediaCaseOutput(
                message=_with_notice(
                    message, "追加取得の保存設定を確認できないため、保存済みの範囲で返しています。"
                )
            )
        now = datetime.now(UTC)
        due = []
        for handle in input.accounts or allowed:
            state = self.store.state(handle)
            if not state.get("initialized"):
                continue
            last = (
                datetime.fromisoformat(state["last_success"]) if state.get("last_success") else None
            )
            attempted = (
                datetime.fromisoformat(state["last_attempt"]) if state.get("last_attempt") else None
            )
            if attempted and state.get("error") and now - attempted < timedelta(days=1):
                continue
            if last and now - last >= timedelta(days=7):
                due.append(handle)
        if not due:
            return MediaCaseOutput(message=message)
        lock = self.jobs.get_dedup_lock(_LOCK_ID)
        previous_id = str(lock.get("target_job_id") or "") if lock else None
        previous = self.jobs.get_job(previous_id) if previous_id else None
        if previous and previous.get("status") in {"queued", "running"}:
            self._start(str(previous_id))
            return MediaCaseOutput(
                message=_with_notice(
                    message, "保存済みの事例を返しながら、新しい投稿を最大3アカウントで更新中です。"
                ),
                status="running",
                deduplicated=True,
            )
        job_id = "media_case_" + uuid.uuid4().hex
        self.jobs.create_job(
            job_id,
            {
                "kind": "media_case",
                "owner": _owner(ctx),
                "input": input.model_dump(mode="json"),
                "accounts": due,
                "request_id": ctx.request_id,
            },
        )
        if not self.jobs.put_dedup_lock(_LOCK_ID, job_id, expected_target=previous_id):
            self.jobs.mark_failed(job_id, "MEDIA_CASE_DEDUPLICATED")
            return MediaCaseOutput(
                message=_with_notice(
                    message, "保存済みの事例を返しながら、新しい投稿を最大3アカウントで更新中です。"
                ),
                status="running",
                deduplicated=True,
            )
        self._start(job_id)
        notice = (
            "完了後にこの会話へ結果を届けます。"
            if origin(ctx) and enabled()
            else "次回の検索から取り込めた分を返します。"
        )
        return MediaCaseOutput(
            message=_with_notice(
                message,
                f"{len(due)}アカウントの追加確認を始めました。"
                + "保存済みの事例はそのまま使えます。"
                + notice,
            ),
            status="queued",
            job_id=job_id,
        )

    def poll(self, job_id: str, ctx: SkillContext) -> tuple[str, str]:
        if not re.fullmatch(r"media_case_[a-f0-9]{32}", job_id):
            return "failed", "取り込みの参照を確認できません。"
        job = self.jobs.get_job(job_id)
        summary = json.loads(job.get("request_summary") or "{}") if job else {}
        if not job or summary.get("kind") != "media_case" or summary.get("owner") != _owner(ctx):
            return "failed", "この取り込みの状態は参照できません。"
        status = str(job["status"])
        if status in {"queued", "running"}:
            self._start(job_id)
            return status, "保存済みの事例を返しながら、新しい投稿を最大3アカウントで更新中です。"
        reason = str(job.get("error_summary") or "")
        if not ctx.metadata.get("identity_verified"):
            return status, (
                reason
                or "投稿の取り込みが完了しました。同じ言葉で検索すると保存済みの事例を返します。"
            )
        return status, _with_notice(
            self._search(MediaCaseInput.model_validate(summary["input"]), ctx),
            reason if status == "failed" else "投稿の追加確認が完了しました。",
        )

    def _worker(self, job_id: str) -> None:
        try:
            with self.store.worker_lock() as alive:
                if alive is None:
                    return
                job = self.jobs.get_job(job_id)
                if not job or job.get("status") not in {"queued", "running"}:
                    return
                summary = json.loads(job["request_summary"])
                if summary.get("kind") != "media_case":
                    return
                if not shared_company_domains_from_env():
                    raise ValueError("company sharing scope unavailable")
                self.jobs.mark_running(job_id)
                apify = self._apify or ApifyClient.from_env(ledger=CostGuard.from_env())
                distiller = self._distiller or CaseDistiller.from_env(CostGuard.from_env())
                pending = [
                    handle
                    for handle in accounts()
                    if (state := self.store.state(handle)).get("intent") or state.get("run_id")
                ]
                self._ingest_many(pending, job_id, apify, distiller, alive)
                self._ingest_many(
                    [
                        h
                        for h in summary["accounts"]
                        if h not in pending and self.store.state(h).get("initialized")
                    ],
                    job_id,
                    apify,
                    distiller,
                    alive,
                )
                self.jobs.mark_done(job_id, json.dumps({"accounts": summary["accounts"]}))
        except Exception as exc:
            reason = (
                "今月の取得・判定枠、または費用予約を確認できず追加を停止しました。"
                if isinstance(exc, CostLimitExceededError)
                else "取り込みが一部で止まりました。確認できた投稿は保存済みです。"
            )
            logger.warning("media_case_ingest_stopped", error=type(exc).__name__, job_id=job_id)
            self.jobs.mark_failed(job_id, "MEDIA_CASE_STOPPED", error_summary=reason)
        finally:
            with self._active_lock:
                self._active.discard(job_id)

    def _ingest_many(
        self,
        handles: list[str],
        job_id: str,
        apify: ApifyClient,
        distiller: CaseDistiller,
        alive: Callable[[], None],
        progress: IngestProgress | None = None,
    ) -> None:
        self._run_accounts(
            handles, lambda h: self._ingest(h, job_id, apify, distiller, alive, progress)
        )

    def _run_accounts(self, handles: list[str], consume: Callable[[str], None]) -> None:
        remaining = iter(dict.fromkeys(handles))
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="media-profile") as executor:

            def submit_next() -> Any:
                handle = next(remaining, None)
                if handle is None:
                    return None
                if handle not in accounts():
                    raise ValueError("unapproved media account")
                return executor.submit(consume, handle)

            active = {future for _ in range(3) if (future := submit_next()) is not None}
            while active:
                finished, active = wait(active, return_when=FIRST_COMPLETED)
                # Stop scheduling on failure; wait for existing runs before releasing the lock.
                for future in finished:
                    future.result()
                for _ in finished:
                    future = submit_next()
                    if future is not None:
                        active.add(future)

    def ingest_initial(
        self,
        handles: list[str],
        *,
        dataset_id: str | None = None,
        kvs: str = "",
        progress: IngestProgress | None = None,
    ) -> dict[str, int]:
        """Administrator CLI only. Dataset import cannot start or resume an Actor."""
        if not handles or any(h not in accounts() for h in handles):
            raise ValueError("unapproved media account")
        if not shared_company_domains_from_env():
            raise ValueError("company sharing scope unavailable")
        with self.store.worker_lock() as alive:
            if alive is None:
                raise RuntimeError("another media ingestion is active")
            apify = self._apify or ApifyClient.from_env(ledger=CostGuard.from_env())
            distiller = self._distiller or CaseDistiller.from_env(CostGuard.from_env())
            pending = [
                h
                for h in accounts()
                if (state := self.store.state(h)).get("intent") or state.get("run_id")
            ]
            if dataset_id:
                if pending:
                    raise RuntimeError("reconcile unfinished profile runs before dataset import")
                snapshot = apify.read_media_dataset(dataset_id, kvs)
                grouped: dict[str, list[dict[str, Any]]] = {h: [] for h in handles}
                seen: set[str] = set()
                for row in snapshot.items:
                    match = re.fullmatch(
                        r"https://(?:www\.)?tiktok\.com/@([A-Za-z0-9_.]+)/(?:photo|video)/[0-9]+(?:[?#].*)?",
                        str(row.get("webVideoUrl") or ""),
                    )
                    if not match or match[1] not in accounts():
                        raise ValueError("dataset contains an unapproved or invalid post")
                    post = normalize_post(row, match[1])
                    if post["id"] in seen:
                        continue
                    seen.add(post["id"])
                    if match[1] in grouped:
                        grouped[match[1]].append(row)
                if any(not rows or len(rows) > 400 for rows in grouped.values()):
                    raise ValueError("dataset must contain 1..400 posts for each selected account")
                assets = ApifyCaseAssets(apify, snapshot)
                if progress:
                    progress.add(total=sum(len(rows) for rows in grouped.values()))

                def consume(handle: str) -> None:
                    alive()
                    state = self.store.state(handle)
                    state.update(
                        last_attempt=datetime.now(UTC).isoformat(),
                        failed_ids=[],
                        fetch_started_at=snapshot.observed_at,
                        error="",
                        dataset_id=dataset_id,
                        kvs_id=snapshot.kvs_id,
                    )
                    self._consume(
                        handle,
                        "",
                        state,
                        replace(snapshot, items=grouped[handle]),
                        apify,
                        distiller,
                        alive,
                        assets=assets,
                        progress=progress,
                    )

                self._run_accounts(list(grouped), consume)
            else:
                self._ingest_many(pending, "", apify, distiller, alive, progress)
                self._ingest_many(
                    [
                        h
                        for h in handles
                        if h not in pending and not self.store.state(h).get("initialized")
                    ],
                    "",
                    apify,
                    distiller,
                    alive,
                    progress,
                )
            states = [self.store.state(h) for h in handles]
            return {
                "accounts": len(states),
                "posts": sum(s.get("result_count", 0) for s in states),
                "failed": sum(len(s.get("failed_ids") or []) for s in states),
                "gaps": sum(bool(s.get("gap")) for s in states),
            }

    def _ingest(
        self,
        handle: str,
        job_id: str,
        apify: ApifyClient,
        distiller: CaseDistiller,
        alive: Callable[[], None],
        progress: IngestProgress | None = None,
    ) -> None:
        state = self.store.state(handle)
        state["last_attempt"] = datetime.now(UTC).isoformat()
        try:
            if state.get("run_id"):
                run = apify.resume_tiktok_profile_run(state["run_id"])
                deadline = time.monotonic() + 180
                while run.status not in _TERMINAL and time.monotonic() < deadline:
                    alive()
                    if job_id:
                        self.jobs.heartbeat(job_id)
                    time.sleep(3)
                    run = apify.resume_tiktok_profile_run(state["run_id"])
                if run.status != "SUCCEEDED":
                    if run.status in _TERMINAL:
                        state.update(intent=False, run_id=None)
                    raise RuntimeError("run requires reconciliation")
            elif state.get("intent"):
                raise RuntimeError("unknown Actor start; manual reconciliation required")
            else:
                state.update(
                    intent=True,
                    failed_ids=[],
                    error="",
                    fetch_started_at=state["last_attempt"],
                    requested_limit=400,
                )
                self.store.save_state(handle, state)
                alive()

                def started(run_id: str) -> None:
                    alive()
                    state["run_id"] = run_id
                    self.store.save_state(handle, state)

                # Date window overlaps two days; pinned posts remain enabled.
                oldest = None
                if state.get("initialized") and state.get("watermark"):
                    oldest = (
                        (datetime.fromisoformat(state["watermark"]) - timedelta(days=2))
                        .date()
                        .isoformat()
                    )
                try:
                    run = apify.tiktok_profiles(
                        handle, oldest_date=oldest, request_id=job_id, on_run_started=started
                    )
                except CostLimitExceededError:
                    state["intent"] = False
                    raise
            if progress:
                progress.add(
                    total=len(
                        {str(raw.get("id") or "") for raw in run.items}
                        - set(state.get("failed_ids") or [])
                    )
                )
            self._consume(handle, job_id, state, run, apify, distiller, alive, progress=progress)
        except Exception as exc:
            state["error"] = type(exc).__name__
            self.store.save_state(handle, state)
            raise

    def _consume(
        self,
        handle: str,
        job_id: str,
        state: dict[str, Any],
        run: ApifyRunResult,
        apify: ApifyClient,
        distiller: CaseDistiller,
        alive: Callable[[], None],
        *,
        assets: ApifyCaseAssets | None = None,
        progress: IngestProgress | None = None,
    ) -> None:
        assets = assets or ApifyCaseAssets(apify, run)
        failed = set(state.get("failed_ids") or [])
        rows = {
            str(raw.get("id") or ""): raw
            for raw in run.items
            if str(raw.get("id") or "") not in failed
        }
        dates = []
        budget_error: CostLimitExceededError | None = None

        def analyze(raw: dict[str, Any]) -> Any:
            alive()
            post = normalize_post(raw, handle)
            previous = self.store.post(post["id"])
            metadata, text, _ = distiller.analyze(post, raw, assets, job_id, previous)
            return post, previous, metadata, text

        remaining = iter(rows.items())
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="media-post") as executor:
            active: dict[Any, str] = {}

            def fill() -> None:
                while len(active) < 2 and budget_error is None:
                    entry = next(remaining, None)
                    if entry is None:
                        break
                    post_id, raw = entry
                    active[executor.submit(analyze, raw)] = post_id

            fill()
            while active:
                finished, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in finished:
                    post_id = active.pop(future)
                    post_failed = False
                    try:
                        post, previous, metadata, text = future.result()
                        alive()
                        if job_id:
                            self.jobs.heartbeat(job_id)
                        observed_at = run.observed_at or datetime.now(UTC).isoformat()
                        newer_metrics = bool(
                            previous.get("metrics_observed_at")
                            and datetime.fromisoformat(previous["metrics_observed_at"])
                            > datetime.fromisoformat(observed_at)
                        )
                        # Only this account's coordinator writes: one post, one transaction.
                        if metadata is not None:
                            metadata["metrics_observed_at"] = (
                                previous["metrics_observed_at"] if newer_metrics else observed_at
                            )
                            if newer_metrics:
                                metadata["metrics"] = previous["metrics"]
                            metadata["operator"] = accounts()[handle]["operator"]
                            self.store.save_post(metadata, text, job_id)
                            if "slide_analysis_failed" in metadata.get("warnings", []):
                                raise ValueError("incomplete slide analysis")
                        elif not newer_metrics:
                            self.store.update_metrics(post_id, post["stats"], observed_at)
                        if post["posted_at"]:
                            dates.append(post["posted_at"])
                    except Exception as exc:
                        post_failed = True
                        failed.add(post_id[:80])
                        state["failed_ids"] = sorted(failed)
                        self.store.save_state(handle, state)
                        if isinstance(exc, CostLimitExceededError):
                            budget_error = exc
                        logger.warning(
                            "media_case_post_failed",
                            error=type(exc).__name__,
                            job_id=job_id,
                            error_message=_post_error_message(exc),
                        )
                    finally:
                        if progress:
                            progress.add(failed=post_failed)
                fill()
        if budget_error is not None:
            raise budget_error
        # Keep an explicit coverage gap, but do not repeatedly buy the same capped window.
        gap = bool(state.get("gap") or len(run.items) >= 400)
        if not failed:
            # A saved acquisition is dated at acquisition, not at today's import time.
            candidate = state.get("fetch_started_at") or state["last_attempt"]
            stamps = [stamp for stamp in (state.get("watermark"), candidate) if stamp]
            state["watermark"] = max(stamps, key=datetime.fromisoformat)
        prior_dates = [state[key] for key in ("oldest_post", "newest_post") if state.get(key)]
        all_dates = [*dates, *prior_dates]
        state.update(
            intent=False,
            run_id=None,
            failed_ids=sorted(failed),
            last_success=max(
                [
                    stamp
                    for stamp in (
                        state.get("last_success"),
                        run.observed_at or datetime.now(UTC).isoformat(),
                    )
                    if stamp
                ],
                key=datetime.fromisoformat,
            ),
            initialized=True,
            error="",
            gap=gap,
            result_count=len(run.items),
            apify_cost_usd=run.estimated_cost_usd or state.get("apify_cost_usd", 0),
            oldest_post=min(all_dates) if all_dates else None,
            newest_post=max(all_dates) if all_dates else None,
        )
        self.store.save_state(handle, state)


_production_skill: MediaCaseSearchSkill | None = None


def production_skill(store: Any | None = None) -> MediaCaseSearchSkill:
    global _production_skill
    if _production_skill is None:
        _production_skill = MediaCaseSearchSkill(store=store)
    return _production_skill
