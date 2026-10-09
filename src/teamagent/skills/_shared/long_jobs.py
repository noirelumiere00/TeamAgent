"""長い作業の共通部品。宛先は gateway が検証した claim だけから渡す。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.skills.base import SkillContext

ORIGIN_KEY = "_verified_long_job_origin"
_OWNER_KEY = "_verified_long_job_owner"


# 実行中の動画分析がこのプロセスで生きているかを答える口。skills は mcp_gateway / runtime を
# import しない決まり（import-linter）なので、mcp_gateway.detached_jobs が起動時に登録する。
# 未登録（＝確かめられない）のときは None を返し、呼び出し側は「止まった」と断定しない。
_liveness_probe: Callable[[str], bool] | None = None


def set_liveness_probe(probe: Callable[[str], bool] | None) -> None:
    global _liveness_probe
    _liveness_probe = probe


def job_is_alive(request_id: str) -> bool | None:
    probe = _liveness_probe
    if probe is None:
        return None
    try:
        return bool(probe(request_id))
    except Exception:
        return None


def enabled() -> bool:
    return os.environ.get("USE_LONG_JOB_NOTIFY", "1").strip().lower() in {"1", "true", "yes"}


async def open_dm_once_more(slack: Any, user_id: str, request_id: str) -> str | None:
    """添付を始める前のDM解決だけを、長ジョブと同じ上限2回・再試行前に1秒待つで行う。"""
    retry_enabled = os.environ.get("USE_LONG_JOB_RETRY", "1").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    for attempt in range(2 if retry_enabled else 1):
        if attempt:
            await asyncio.sleep(1)
        try:
            channel = await asyncio.wait_for(slack.open_dm(user_id, request_id), timeout=30)
            if channel:
                return str(channel)
        except Exception:
            continue
    return None


@dataclass
class _DeferredUpload:
    slack: Any
    path: str
    title: str
    comment: str
    request_id: str
    channel_id: str
    thread_ts: str | None
    dm_user_id: str | None = None
    deliver_on_failure: bool = False
    on_result: Callable[[bool], None] | None = None


@dataclass
class Origin:
    channel_id: str
    thread_ts: str | None
    user_id: str
    deferred: bool = True
    _lock: Any = field(default_factory=threading.Lock, repr=False)
    _notices: set[str] = field(default_factory=set, repr=False)
    cancelled: bool = False
    _uploads: list[_DeferredUpload] = field(default_factory=list, repr=False)
    redirected: bool = False
    primary_delivered: bool = False
    primary_expected: bool = False
    research_delivered: bool = False
    research_delivery_failed: bool = False

    def __deepcopy__(self, memo: dict[int, Any]) -> Origin:
        # 背景ジョブの ctx と見張りで同じ配信の錠を共有する。
        return self

    def claim_notice(self, kind: str) -> bool:
        with self._lock:
            if kind in self._notices:
                return False
            self._notices.add(kind)
            return True

    @property
    def pending(self) -> bool:
        with self._lock:
            return bool(self._uploads)

    @property
    def primary_pending(self) -> bool:
        with self._lock:
            return any(not upload.deliver_on_failure for upload in self._uploads)

    def defer(
        self,
        slack: Any,
        path: str,
        title: str,
        comment: str,
        request_id: str,
        *,
        channel_id: str | None = None,
        thread_ts: str | None = None,
        dm_user_id: str | None = None,
        deliver_on_failure: bool = False,
        on_result: Callable[[bool], None] | None = None,
    ) -> None:
        if not deliver_on_failure:
            self.primary_expected = True
        if self.cancelled:
            if on_result is not None:
                on_result(False)
            return
        workdir = tempfile.mkdtemp(prefix="long-job-delivery-")
        copied = str(Path(workdir) / Path(path).name)
        try:
            shutil.copyfile(path, copied)
            with self._lock:
                self._uploads.append(
                    _DeferredUpload(
                        slack=slack,
                        path=copied,
                        title=title,
                        comment=comment,
                        request_id=request_id,
                        channel_id=channel_id or self.channel_id,
                        thread_ts=self.thread_ts
                        if channel_id is None and dm_user_id is None
                        else thread_ts,
                        dm_user_id=dm_user_id,
                        deliver_on_failure=deliver_on_failure,
                        on_result=on_result,
                    )
                )
        except BaseException:
            shutil.rmtree(workdir, ignore_errors=True)
            raise

    def discard(self, *, keep_failure_uploads: bool = False) -> None:
        with self._lock:
            uploads, self._uploads = self._uploads, []
            if keep_failure_uploads:
                self._uploads = [upload for upload in uploads if upload.deliver_on_failure]
                uploads = [upload for upload in uploads if not upload.deliver_on_failure]
        for upload in uploads:
            if upload.on_result is not None:
                try:
                    upload.on_result(False)
                except Exception as exc:
                    structlog.get_logger(__name__).warning(
                        "long_job_delivery_callback_failed", error=type(exc).__name__
                    )
            shutil.rmtree(Path(upload.path).parent, ignore_errors=True)

    def deliver(self) -> bool:
        """結果を台帳へ保存した後に呼ぶ。完了文は最初のファイルのコメントだけ。"""
        with self._lock:
            uploads, self._uploads = self._uploads, []
        if not uploads:
            return False

        async def send() -> bool:
            dm_channels: dict[str, str | None] = {}
            delivered = False
            primary_results: list[bool] = []
            primary_timed_out = False
            for upload in uploads:
                ok = False
                try:
                    channel = upload.channel_id
                    if upload.dm_user_id:
                        if upload.dm_user_id not in dm_channels:
                            dm_channels[upload.dm_user_id] = None
                            dm_channels[upload.dm_user_id] = await open_dm_once_more(
                                upload.slack, upload.dm_user_id, upload.request_id
                            )
                        channel = dm_channels[upload.dm_user_id] or ""
                    self.redirected |= bool(
                        channel
                        and (channel != self.channel_id or upload.thread_ts != self.thread_ts)
                    )
                    if channel:
                        ok = bool(
                            await asyncio.wait_for(
                                upload.slack.upload_file(
                                    channel,
                                    upload.path,
                                    upload.request_id,
                                    title=upload.title,
                                    initial_comment=upload.comment if not delivered else None,
                                    thread_ts=upload.thread_ts,
                                ),
                                timeout=250,
                            )
                        )
                except TimeoutError:
                    # 到達が不確実な添付は自動再送しない。後続のJSONは独立して届ける。
                    primary_timed_out |= not upload.deliver_on_failure
                    ok = False
                except Exception:
                    ok = False
                delivered |= ok
                if not upload.deliver_on_failure:
                    primary_results.append(ok)
                self.research_delivered |= ok and upload.deliver_on_failure
                self.research_delivery_failed |= not ok and upload.deliver_on_failure
                if upload.on_result is not None:
                    try:
                        upload.on_result(ok)
                    except Exception as exc:
                        structlog.get_logger(__name__).warning(
                            "long_job_delivery_callback_failed",
                            request_id=upload.request_id,
                            error=type(exc).__name__,
                        )
            self.primary_delivered = bool(primary_results) and all(primary_results)
            if primary_timed_out:
                # 主添付は届いた可能性がある。既存通知側の不確実扱いを維持する。
                raise TimeoutError("primary upload outcome uncertain")
            return all(primary_results) if primary_results else delivered

        try:
            return bool(asyncio.run(send()))
        finally:
            for upload in uploads:
                shutil.rmtree(Path(upload.path).parent, ignore_errors=True)


def origin(ctx: SkillContext) -> Origin | None:
    value = ctx.metadata.get(ORIGIN_KEY)
    return value if enabled() and isinstance(value, Origin) else None


def completed_delivery_failed(
    status: str, slack_delivered: bool | None, target: Origin | None
) -> bool:
    """完了・未配信が確認でき、待機中の添付もない場合だけ配信失敗とする。"""
    return (
        status == "done"
        and slack_delivered is False
        and not (target is not None and target.pending)
    )


def owner_key(ctx: SkillContext, tool: str) -> str | None:
    owner = ctx.metadata.get(_OWNER_KEY)
    if not isinstance(owner, str) or not owner:
        return None
    # チャンネルからの照会を別の会話に広げない。DM は本人の直近を引く。
    channel = ctx.metadata.get("channel_id", "")
    surface = "dm" if isinstance(channel, str) and channel.startswith("D") else str(channel)
    digest = hashlib.sha256(f"{owner}\x1f{surface}\x1f{tool}".encode()).hexdigest()
    return f"dedup_latest_{digest}"


def remember_latest(ctx: SkillContext, tool: str, job_id: str) -> None:
    if not enabled() or not job_id:
        return
    key = owner_key(ctx, tool)
    if key is None:
        return
    store = ProposalJobStore()
    for _ in range(3):
        row = store.get_dedup_lock(key)
        if store.put_dedup_lock(key, job_id, expected_target=row["target_job_id"] if row else None):
            return
    raise RuntimeError("latest job pointer contention")


def latest_job(ctx: SkillContext, tool: str) -> str:
    if not enabled():
        return ""
    key = owner_key(ctx, tool)
    if key is None:
        return ""
    row = ProposalJobStore().get_dedup_lock(key)
    return str(row.get("target_job_id") or "") if row else ""


def failure_reason(code: str | None) -> str:
    """コード・例外本文を利用者へ渡さず、確認できた原因だけを一行で返す。"""
    raw = (code or "").upper()
    if "SCOPE" in raw or "PERMISSION" in raw or "NOT_IN_CHANNEL" in raw:
        return "必要なアクセス権を確認できず、処理が止まりました。"
    if "RATE" in raw or "THROTTL" in raw:
        return "取得先の利用制限により、処理が止まりました。"
    if "TIMEOUT" in raw or "DEADLINE" in raw:
        return "処理の制限時間に達しました。"
    if "RESTART" in raw or "TERMINAT" in raw:
        return "システム更新で処理が中断されました。"
    if "STATE" in raw or "WRITE" in raw:
        return "結果の保存を確認できませんでした。"
    return "取得先の応答または資料の組み立てで問題が起きました。"
