"""長い作業の完了・失敗・長時間継続を署名済みの依頼元へ一度ずつ届ける。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable
from typing import Any

import structlog

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.mcp_gateway import detached_jobs
from teamagent.skills._shared.long_jobs import (
    _OWNER_KEY,
    ORIGIN_KEY,
    TERMINAL_STATES,
    Origin,
    enabled,
)
from teamagent.skills._shared.slack_blocks import RichMessage
from teamagent.skills.base import SkillContext

logger = structlog.get_logger(__name__)
_INITIAL_DELAY_SECONDS = 30.0
_POLL_INTERVAL_SECONDS = 30.0
_TIMEOUT_SECONDS = 60 * 60.0
# 停止の断定はしない。見張りは継続し、後から保存された結果も届ける。
_MAX_WATCH_SECONDS = 24 * 60 * 60.0
_monotonic: Callable[[], float] = time.monotonic
_active: dict[str, tuple[str, str, Origin, str]] = {}
_lock = threading.Lock()
_OUTBOX_KEY = "dedup_long_job_outbox"
_CREATED_AT_KEY = "_long_job_created_at"
_recovery_stop = threading.Event()
_recovery_started = False


def publish_notice(
    message: str,
    *,
    origin: Origin,
    request_id: str,
    job_id: str,
    kind: str = "terminal",
    rich: RichMessage | None = None,
    completed: bool = True,
) -> bool:
    """配信の錠は既存の台帳へ保存。timeout は不確実として二重送信しない。"""
    if not enabled() or not origin.claim_notice(kind):
        return False
    digest = hashlib.sha256(
        f"{job_id}\x1f{origin.channel_id}\x1f{origin.thread_ts}\x1f{kind}".encode()
    ).hexdigest()
    store = ProposalJobStore()
    key = f"dedup_notice_{digest}"
    claimed = False
    delivery_started = False

    def record_primary_failure(*, uncertain: bool = False) -> None:
        try:
            store.record_primary_delivery_failure(job_id, uncertain=uncertain)
        except Exception as exc:
            logger.warning(
                "long_job_delivery_record_failed", request_id=request_id, error=type(exc).__name__
            )

    try:
        if not store.put_dedup_lock(key, "claimed", expected_target=None):
            if not store.put_dedup_lock(key, "claimed", expected_target=""):
                return False
        claimed = True
        if kind == "terminal" and not completed:
            # 組み立てに失敗しても、完了済み調査JSONは依頼者へ届ける。
            origin.discard(keep_failure_uploads=True)
        if kind == "terminal" and origin.pending:
            delivered = False
            try:
                delivery_started = True
                delivered = origin.deliver()
                if origin.primary_expected and not origin.primary_delivered:
                    delivered = False
                if delivered:
                    if completed and origin.primary_delivered:
                        try:
                            store.mark_delivered(job_id)
                        except Exception as exc:
                            logger.warning(
                                "long_job_delivery_record_failed",
                                request_id=request_id,
                                error=type(exc).__name__,
                            )
                    if origin.research_delivery_failed:
                        json_note = (
                            "調査JSONの添付に失敗しました。"
                            "調査JSONが必要な場合は再度調査をご依頼ください。"
                        )
                        # 失敗したジョブは元の失敗の知らせを残す（上書きすると失敗が伝わらない）。
                        message = (
                            "資料はDMへお届けしましたが、" + json_note
                            if completed
                            else f"{message} {json_note}"
                        )
                    elif completed:
                        if origin.redirected:
                            detached_jobs.post_to_origin(
                                "資料をDMへお届けしました。"
                                if origin.primary_delivered
                                else "調査JSONをDMへお届けしました。提案書の添付は行っていません。",
                                detached_jobs.Destination(origin.channel_id, origin.thread_ts),
                                request_id=request_id,
                                fallback_user_id=origin.user_id,
                            )
                        return True
                    else:
                        message += " 完了済みの調査JSONはDMへお届けしました。"
            except TimeoutError:
                logger.warning("long_job_notice_uncertain", request_id=request_id, job_id=job_id)
                # 添付の自動再送はしない。DMへ振り替えた場合は依頼元へ結果の不確実性を知らせる。
                if not origin.redirected:
                    record_primary_failure(uncertain=True)
                    return False
                record_primary_failure(uncertain=True)
                message = "提案書の添付結果を確認できませんでした。DMをご確認ください。"
                message += (
                    " 完了済みの調査JSONはDMへお届けしました。"
                    if origin.research_delivered
                    else " 調査JSONの添付に失敗しました。再度調査をご依頼ください。"
                    if origin.research_delivery_failed
                    else ""
                )
                delivered = True  # 下の確定失敗の文には置き換えない。
            except Exception as exc:
                logger.warning(
                    "long_job_upload_failed", request_id=request_id, error=type(exc).__name__
                )
            if not delivered:
                if origin.primary_expected and not origin.primary_delivered:
                    record_primary_failure()
                notes = []
                if origin.primary_expected and not origin.primary_delivered:
                    notes.append("提案書の添付に失敗しました。")
                if origin.research_delivered:
                    notes.append("完了済みの調査JSONはDMへお届けしました。")
                elif origin.research_delivery_failed:
                    notes.append("調査JSONの添付に失敗しました。再度調査をご依頼ください。")
                if not completed:
                    # 失敗したジョブは元の失敗の知らせに添付の結果を書き足す（上書きしない）。
                    message = " ".join([message, *notes]).strip()
                else:
                    message = (
                        " ".join(notes)
                        or "資料は生成・保存できましたが、Slackへの添付に失敗しました。"
                    )
        delivery_started = True
        status = detached_jobs.post_to_origin_status(
            message,
            detached_jobs.Destination(origin.channel_id, origin.thread_ts),
            request_id=request_id,
            fallback_user_id=origin.user_id,
            rich=rich,
        )
        if status == "failed":
            store.put_dedup_lock(key, "", expected_target="claimed")
            origin.release_notice(kind)
        elif status == "uncertain":
            logger.warning("long_job_notice_uncertain", request_id=request_id, job_id=job_id)
        return status == "posted"
    except Exception as exc:
        if not delivery_started:
            if claimed:
                try:
                    store.put_dedup_lock(key, "", expected_target="claimed")
                except Exception:
                    pass
            origin.release_notice(kind)
        logger.warning("long_job_notice_failed", request_id=request_id, error=type(exc).__name__)
        return False


def run_persisted_job(
    job_id: str, store: ProposalJobStore, run: Callable[[], Any], describe: Callable[[Any], str]
) -> Any:
    """切り離した実行の心拍・結果保存を、既存のジョブ台帳で共有する。"""
    if not store.mark_running(job_id):
        raise RuntimeError("VIDEO_ALGORITHM_STATE_WRITE_FAILED")
    heartbeat_stop = threading.Event()

    def heartbeat() -> None:
        while not heartbeat_stop.wait(30):
            try:
                if not store.heartbeat(job_id):
                    return
            except Exception as exc:
                logger.warning("video_job_heartbeat_failed", error=type(exc).__name__)

    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
    try:
        heartbeat_thread.start()
        result = run()
        text = describe(result)
        failed = bool(getattr(result, "error", None)) or getattr(result, "status", "") in (
            TERMINAL_STATES - {"done"}
        )
        saved = (
            store.mark_failed(job_id, "LONG_JOB_FAILED", error_summary=text)
            if failed
            else store.mark_done(job_id, json.dumps({"message": text}))
        )
        if not saved:
            raise RuntimeError("VIDEO_ALGORITHM_STATE_WRITE_FAILED")
        return result
    except Exception as exc:
        store.mark_failed(
            job_id,
            "VIDEO_ALGORITHM_FAILED",
            error_summary=detached_jobs.user_message_for_error(exc),
        )
        raise
    finally:
        heartbeat_stop.set()
        if heartbeat_thread.ident is not None:
            heartbeat_thread.join(timeout=1)


def schedule_completion_notice(
    *,
    tool: str,
    job_id: str,
    origin: Origin,
    request_id: str,
    poll: Callable[[], tuple[str, str]],
    ctx: SkillContext | None = None,
) -> None:
    if not enabled():
        return
    key = f"{tool}:{job_id}:{origin.channel_id}:{origin.thread_ts}"
    with _lock:
        if key in _active:
            return
        _active[key] = (tool, job_id, origin, request_id)

    if ctx is not None:
        from teamagent.adapters.tiktok_s3_source import media_audit_principal_hash

        entry = {
            "key": key,
            "tool": tool,
            "job_id": job_id,
            "channel_id": origin.channel_id,
            "thread_ts": origin.thread_ts,
            "user_id": origin.user_id,
            "request_id": request_id,
            "owner": ctx.metadata.get(_OWNER_KEY, ""),
            "user_email": ctx.metadata.get("user_email", ""),
            "principal": ctx.metadata.get("_long_job_principal_hash")
            or media_audit_principal_hash(
                ctx.metadata.get("user_email") or ctx.user_id or "unknown"
            ),
            # 再開しても最初の受付時刻を引き継ぐ（再開のたびに期限が延びないように）。
            "created_at": float(ctx.metadata.get(_CREATED_AT_KEY) or time.time()),
            "updated_at": time.time(),
        }
        try:
            _change_outbox(lambda entries: {**entries, key: entry})
        except Exception as exc:
            # 台帳に書けなくても依頼そのものは落とさない。見張りはこのプロセスで続け、
            # 再起動後の再開だけを諦める（ジョブの受付を通知の都合で失敗させない）。
            ctx = None
            logger.warning(
                "long_job_outbox_write_failed", request_id=request_id, error=type(exc).__name__
            )

    outbox_key = key if ctx is not None else None

    def run() -> None:
        finished = False
        try:
            finished = _run_completion_notice(
                job_id=job_id,
                origin=origin,
                request_id=request_id,
                poll=poll,
                outbox_key=outbox_key,
                created_at=float(ctx.metadata.get(_CREATED_AT_KEY) or time.time())
                if ctx is not None
                else None,
            )
        finally:
            with _lock:
                _active.pop(key, None)
            # 確定失敗・例外・更新での停止は未配信情報を残して再開に回す。
            if outbox_key and finished and not origin.cancelled:
                _remove_from_outbox(outbox_key, request_id)

    try:
        threading.Thread(target=run, name="long-job-notify", daemon=True).start()
    except Exception as exc:
        with _lock:
            _active.pop(key, None)
        logger.warning("long_job_schedule_failed", request_id=request_id, error=type(exc).__name__)


def _run_completion_notice(
    *,
    job_id: str,
    origin: Origin,
    request_id: str,
    poll: Callable[[], tuple[str, str]],
    outbox_key: str | None = None,
    created_at: float | None = None,
) -> bool:
    started = _monotonic()
    elapsed = max(0.0, time.time() - created_at) if created_at is not None else 0.0
    deadline = started + max(0.0, _MAX_WATCH_SECONDS - elapsed)
    _wait_until_next_poll(deadline, _INITIAL_DELAY_SECONDS)
    last_status = "unknown"
    message = ""
    while not origin.cancelled:
        if outbox_key:
            try:

                def touch(entries: dict[str, Any]) -> dict[str, Any]:
                    if outbox_key in entries:
                        entries[outbox_key]["updated_at"] = time.time()
                    return entries

                _change_outbox(touch)
            except Exception as exc:
                logger.warning(
                    "long_job_outbox_touch_failed", request_id=request_id, error=type(exc).__name__
                )
        try:
            status, message = poll()
            last_status = status
        except Exception as exc:
            last_status = "unknown"
            logger.warning("long_job_poll_failed", request_id=request_id, error=type(exc).__name__)
        else:
            if status == "running" and message.startswith("sales25:"):
                phase = message.split("。", 1)[0]
                # 件数（検索 1/6・分析 3/30）だけの変化では知らせない。大きな工程の切り替わりだけ。
                phase = re.sub(r"\s*\d+\s*/\s*\d+.*$", "", phase)
                publish_notice(
                    message,
                    origin=origin,
                    request_id=request_id,
                    job_id=job_id,
                    kind="progress_" + phase,
                )
            if status in TERMINAL_STATES:
                publish_notice(
                    message,
                    origin=origin,
                    request_id=request_id,
                    job_id=job_id,
                    completed=status == "done",
                )
                if origin.has_notice("terminal"):
                    return True
        if last_status not in TERMINAL_STATES and _monotonic() - started >= _TIMEOUT_SECONDS:
            message = (
                "お土産資料の作業はまだ完了していません。通常の目安は約45〜60分（暫定）、混雑・長尺時は約90分以上です。引き続き確認し、結果をこの会話にお届けします。"
                if message.startswith("sales25:") and last_status in {"queued", "running"}
                else (
                    "作業はまだ完了していません。通常より時間がかかっています。"
                    "完了・失敗はこの会話にお届けします。"
                )
                if last_status in {"queued", "running"}
                else (
                    "作業の状態を確認できません。確認を続け、"
                    "結果が確認できたらこの会話にお届けします。"
                )
            )
            publish_notice(
                message, origin=origin, request_id=request_id, job_id=job_id, kind="stalled"
            )
        if _monotonic() >= deadline:
            break
        _wait_until_next_poll(deadline, _POLL_INTERVAL_SECONDS)
    if origin.cancelled or last_status in TERMINAL_STATES:
        return False
    publish_notice(
        "作業の見守りを継続できなくなりました。完了は確認できていません。",
        origin=origin,
        request_id=request_id,
        job_id=job_id,
        completed=False,
    )
    return origin.has_notice("terminal")


def _wait_until_next_poll(deadline: float, interval_seconds: float) -> None:
    remaining = deadline - _monotonic()
    if remaining > 0:
        threading.Event().wait(min(interval_seconds, remaining))


async def notify_interrupted(*, budget_s: float) -> int:
    """監視を止めて台帳に残す。外部 worker の実行は再起動後に確認する。"""
    _recovery_stop.set()
    if not enabled():
        return 0
    with _lock:
        jobs = [
            job
            for job in _active.values()
            if job[0] not in detached_jobs.DETACHABLE_TOOLS and not job[2].cancelled
        ]
    for _, _, target, _ in jobs:
        target.cancelled = True
    if not jobs:
        return 0

    for _, _, target, _ in jobs:
        target.discard()
    return len(jobs)


def _remove_from_outbox(outbox_key: str, request_id: str) -> None:
    try:
        _change_outbox(lambda entries: {k: v for k, v in entries.items() if k != outbox_key})
    except Exception as exc:
        logger.warning(
            "long_job_outbox_cleanup_failed", request_id=request_id, error=type(exc).__name__
        )


def _read_outbox(store: ProposalJobStore) -> tuple[str | None, dict[str, Any]]:
    row = store.get_dedup_lock(_OUTBOX_KEY)
    raw = row.get("target_job_id") if row else None
    entries = json.loads(raw) if isinstance(raw, str) and raw else {}
    if not isinstance(entries, dict):
        raise ValueError("invalid notification outbox")
    return raw, entries


def _change_outbox(change: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
    store = ProposalJobStore()
    for _ in range(5):
        previous, entries = _read_outbox(store)
        updated = change(entries)
        serialized = json.dumps(updated, ensure_ascii=False, sort_keys=True)
        if len(serialized.encode()) > 250 * 1024:
            raise ValueError("notification outbox capacity reached")
        if store.put_dedup_lock(_OUTBOX_KEY, serialized, expected_target=previous):
            return
    raise RuntimeError("notification outbox contention")


def recover_pending_notices() -> int:
    """固定キー一件だけから未通知ジョブを再開する。Scan・新しい権限は不要。"""
    if not enabled():
        return 0
    from teamagent.mcp_gateway.server import _ASYNC_JOB_TOOLS, _build_async_job_poll
    from teamagent.skills.base import ASYNC_JOB_POLL_METADATA_KEY

    _, entries = _read_outbox(ProposalJobStore())
    resumed = 0
    for key, entry in entries.items():
        with _lock:
            if key in _active:
                continue
        # 別の MCP プロセスが監視を続けている行は奪わない。
        if time.time() - float(entry["updated_at"]) <= 180:
            continue
        tool = entry["tool"]
        if tool not in _ASYNC_JOB_TOOLS | detached_jobs.DETACHABLE_TOOLS:
            continue
        target = Origin(entry["channel_id"], entry["thread_ts"], entry["user_id"])
        context = SkillContext(
            request_id=entry["request_id"],
            metadata={
                _OWNER_KEY: entry["owner"],
                "channel_id": entry["channel_id"],
                "_long_job_principal_hash": entry["principal"],
                "user_email": entry.get("user_email", ""),
                "identity_verified": True,
                "verified_slack_user_id": entry["user_id"],
                _CREATED_AT_KEY: entry.get("created_at", entry["updated_at"]),
                ASYNC_JOB_POLL_METADATA_KEY: True,
                ORIGIN_KEY: target,
            },
        )
        job_ids = entry["job_id"].split("|")
        polls = [_build_async_job_poll(tool, job_id, context) for job_id in job_ids]

        def poll_all(polls: list[Callable[[], tuple[str, str]]] = polls) -> tuple[str, str]:
            results = [poll() for poll in polls]
            if any(state not in TERMINAL_STATES for state, _ in results):
                state = "unknown" if any(state == "unknown" for state, _ in results) else "running"
                return state, ""
            state = "failed" if any(state != "done" for state, _ in results) else "done"
            return state, "\n\n".join(text for _, text in results)

        schedule_completion_notice(
            tool=tool,
            job_id=entry["job_id"],
            origin=target,
            request_id=entry["request_id"],
            poll=poll_all,
            ctx=context,
        )
        resumed += 1
    return resumed


def start_recovery() -> None:
    """起動時に未通知台帳の回復を開始。障害時も次の周期で読み直す。"""
    global _recovery_started
    if not enabled() or _recovery_started:
        return
    _recovery_started = True
    _recovery_stop.clear()

    def run() -> None:
        while not _recovery_stop.is_set() and enabled():
            try:
                if os.environ.get("USE_MEDIA_CASE_SEARCH", "").lower() in {"1", "true", "yes"}:
                    from teamagent.skills.media_case_search.skill import production_skill

                    production_skill().recover()
                recover_pending_notices()
            except Exception as exc:
                logger.warning("long_job_recovery_failed", error=type(exc).__name__)
            _recovery_stop.wait(30)

    threading.Thread(target=run, name="long-job-recovery", daemon=True).start()
