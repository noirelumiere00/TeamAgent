"""長い作業の完了・失敗・長時間継続を署名済みの依頼元へ一度ずつ届ける。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from collections.abc import Callable
from typing import Any

import structlog

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.mcp_gateway import detached_jobs
from teamagent.skills._shared.long_jobs import _OWNER_KEY, Origin, enabled
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

    def record_primary_failure(*, uncertain: bool = False) -> None:
        try:
            store.record_primary_delivery_failure(job_id, uncertain=uncertain)
        except Exception as exc:
            logger.warning(
                "long_job_delivery_record_failed", request_id=request_id, error=type(exc).__name__
            )

    try:
        if not store.put_dedup_lock(key, "claimed", expected_target=None):
            return False
        if kind == "terminal" and not completed:
            # 組み立てに失敗しても、完了済み調査JSONは依頼者へ届ける。
            origin.discard(keep_failure_uploads=True)
        if kind == "terminal" and origin.pending:
            delivered = False
            try:
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
                # 添付の自動再送はしない。DMへ振り替えた場合は依頼元へ結果の不確実性を知らせる。
                if not origin.redirected:
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
        return detached_jobs.post_to_origin(
            message,
            detached_jobs.Destination(origin.channel_id, origin.thread_ts),
            request_id=request_id,
            fallback_user_id=origin.user_id,
            rich=rich,
        )
    except Exception as exc:
        logger.warning("long_job_notice_failed", request_id=request_id, error=type(exc).__name__)
        return False


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
            "principal": ctx.metadata.get("_long_job_principal_hash")
            or media_audit_principal_hash(
                ctx.metadata.get("user_email") or ctx.user_id or "unknown"
            ),
            # 再開しても最初の受付時刻を引き継ぐ（再開のたびに期限が延びないように）。
            "created_at": float(ctx.metadata.get(_CREATED_AT_KEY) or time.time()),
            "updated_at": time.time(),
        }
        try:
            _change_outbox(lambda entries: {**_drop_expired(entries), key: entry})
        except Exception as exc:
            # 台帳に書けなくても依頼そのものは落とさない。見張りはこのプロセスで続け、
            # 再起動後の再開だけを諦める（ジョブの受付を通知の都合で失敗させない）。
            ctx = None
            logger.warning(
                "long_job_outbox_write_failed", request_id=request_id, error=type(exc).__name__
            )

    outbox_key = key if ctx is not None else None

    def run() -> None:
        try:
            _run_completion_notice(
                job_id=job_id,
                origin=origin,
                request_id=request_id,
                poll=poll,
                outbox_key=outbox_key,
            )
        finally:
            with _lock:
                _active.pop(key, None)
            # 更新での停止（cancelled）だけは行を残し、次のプロセスが見張りを再開する。
            # それ以外の終わり方（完了・失敗・24 時間の見守り切れ・例外）では行を消す。
            if outbox_key and not origin.cancelled:
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
) -> None:
    started = _monotonic()
    deadline = started + _MAX_WATCH_SECONDS
    _wait_until_next_poll(deadline, _INITIAL_DELAY_SECONDS)
    last_status = "unknown"
    while _monotonic() < deadline and not origin.cancelled:
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
            if status in {"done", "failed"}:
                publish_notice(
                    message,
                    origin=origin,
                    request_id=request_id,
                    job_id=job_id,
                    completed=status == "done",
                )
                return
        if _monotonic() - started >= _TIMEOUT_SECONDS:
            message = (
                "作業はまだ完了していません。通常より時間がかかっています。完了・失敗はこの会話にお届けします。"
                if last_status in {"queued", "running"}
                else (
                    "作業の状態を確認できません。確認を続け、"
                    "結果が確認できたらこの会話にお届けします。"
                )
            )
            publish_notice(
                message, origin=origin, request_id=request_id, job_id=job_id, kind="stalled"
            )
        _wait_until_next_poll(deadline, _POLL_INTERVAL_SECONDS)
    if origin.cancelled:
        return
    publish_notice(
        "作業の見守りを継続できなくなりました。完了は確認できていません。",
        origin=origin,
        request_id=request_id,
        job_id=job_id,
        kind="interrupted",
    )


def _wait_until_next_poll(deadline: float, interval_seconds: float) -> None:
    remaining = deadline - _monotonic()
    if remaining > 0:
        threading.Event().wait(min(interval_seconds, remaining))


async def notify_interrupted(*, budget_s: float) -> int:
    """終了時に監視の中断を知らせる。実行自体の停止は断定しない。"""
    _recovery_stop.set()
    if not enabled():
        return 0
    with _lock:
        jobs = [
            job for job in _active.values() if job[0] != "video_algorithm" and not job[2].cancelled
        ]
    for _, _, target, _ in jobs:
        target.cancelled = True
    if not jobs:
        return 0

    async def one(job: tuple[str, str, Origin, str]) -> None:
        _, job_id, target, request_id = job
        await asyncio.to_thread(
            publish_notice,
            "システム更新のため結果の確認をいったん止めました。更新後に確認を再開し、結果はこの会話にお届けします。",
            origin=target,
            request_id=request_id,
            job_id=job_id,
            kind="interrupted",
        )
        target.discard()

    try:
        await asyncio.wait_for(asyncio.gather(*(one(job) for job in jobs)), timeout=budget_s)
    except TimeoutError:
        logger.warning("long_job_interrupt_budget_exceeded", count=len(jobs))
    return len(jobs)


def _remove_from_outbox(outbox_key: str, request_id: str) -> None:
    try:
        _change_outbox(lambda entries: {k: v for k, v in entries.items() if k != outbox_key})
    except Exception as exc:
        logger.warning(
            "long_job_outbox_cleanup_failed", request_id=request_id, error=type(exc).__name__
        )


def _drop_expired(entries: dict[str, Any]) -> dict[str, Any]:
    """見守りの上限（24 時間）を過ぎた行を捨てる。行が溜まって台帳が満杯になるのを防ぐ。"""
    limit = time.time() - _MAX_WATCH_SECONDS - 2 * 60 * 60
    return {
        k: v
        for k, v in entries.items()
        if float(v.get("created_at", v.get("updated_at", 0.0))) >= limit
    }


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
    from teamagent.mcp_gateway.server import _build_async_job_poll
    from teamagent.skills.base import ASYNC_JOB_POLL_METADATA_KEY

    _, entries = _read_outbox(ProposalJobStore())
    if len(_drop_expired(entries)) != len(entries):
        try:
            _change_outbox(_drop_expired)
        except Exception as exc:
            logger.warning("long_job_outbox_expire_failed", error=type(exc).__name__)
        entries = _drop_expired(entries)
    resumed = 0
    for key, entry in entries.items():
        with _lock:
            if key in _active:
                continue
        # 別の MCP プロセスが監視を続けている行は奪わない。
        if time.time() - float(entry["updated_at"]) <= 180:
            continue
        tool = entry["tool"]
        if tool not in {
            "tiktok_acquire",
            "omiyage_report_submit",
            "proposal_builder_submit",
            "video_algorithm",
        }:
            continue
        target = Origin(entry["channel_id"], entry["thread_ts"], entry["user_id"])
        context = SkillContext(
            request_id=entry["request_id"],
            metadata={
                _OWNER_KEY: entry["owner"],
                "channel_id": entry["channel_id"],
                "_long_job_principal_hash": entry["principal"],
                _CREATED_AT_KEY: entry.get("created_at", entry["updated_at"]),
                ASYNC_JOB_POLL_METADATA_KEY: True,
            },
        )
        job_ids = entry["job_id"].split("|")
        polls = [_build_async_job_poll(tool, job_id, context) for job_id in job_ids]

        def poll_all(polls: list[Callable[[], tuple[str, str]]] = polls) -> tuple[str, str]:
            results = [poll() for poll in polls]
            if any(state not in {"done", "failed"} for state, _ in results):
                state = "unknown" if any(state == "unknown" for state, _ in results) else "running"
                return state, ""
            state = "failed" if any(state == "failed" for state, _ in results) else "done"
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
                recover_pending_notices()
            except Exception as exc:
                logger.warning("long_job_recovery_failed", error=type(exc).__name__)
            _recovery_stop.wait(30)

    threading.Thread(target=run, name="long-job-recovery", daemon=True).start()
