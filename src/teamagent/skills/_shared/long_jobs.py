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


@dataclass
class Origin:
    channel_id: str
    thread_ts: str | None
    user_id: str
    deferred: bool = True
    _lock: Any = field(default_factory=threading.Lock, repr=False)
    _notices: set[str] = field(default_factory=set, repr=False)
    cancelled: bool = False
    _uploads: list[tuple[Any, str, str, str, str, str, str | None]] = field(
        default_factory=list, repr=False
    )

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
    ) -> None:
        if self.cancelled:
            return
        workdir = tempfile.mkdtemp(prefix="long-job-delivery-")
        copied = str(Path(workdir) / Path(path).name)
        try:
            shutil.copyfile(path, copied)
            with self._lock:
                self._uploads.append(
                    (
                        slack,
                        copied,
                        title,
                        comment,
                        request_id,
                        channel_id or self.channel_id,
                        self.thread_ts if channel_id is None else thread_ts,
                    )
                )
        except BaseException:
            shutil.rmtree(workdir, ignore_errors=True)
            raise

    def discard(self) -> None:
        with self._lock:
            uploads, self._uploads = self._uploads, []
        for _, path, _, _, _, _, _ in uploads:
            shutil.rmtree(Path(path).parent, ignore_errors=True)

    def deliver(self) -> bool:
        """結果を台帳へ保存した後に呼ぶ。完了文は最初のファイルのコメントだけ。"""
        with self._lock:
            uploads, self._uploads = self._uploads, []
        if not uploads:
            return False

        async def send() -> bool:
            for index, (slack, path, title, comment, request_id, channel, thread) in enumerate(
                uploads
            ):
                ok = await asyncio.wait_for(
                    slack.upload_file(
                        channel,
                        path,
                        request_id,
                        title=title,
                        initial_comment=comment if index == 0 else None,
                        thread_ts=thread,
                    ),
                    timeout=250,
                )
                if not ok:
                    return False
            return True

        try:
            return bool(asyncio.run(send()))
        finally:
            for _, path, _, _, _, _, _ in uploads:
                shutil.rmtree(Path(path).parent, ignore_errors=True)


def origin(ctx: SkillContext) -> Origin | None:
    value = ctx.metadata.get(ORIGIN_KEY)
    return value if enabled() and isinstance(value, Origin) else None


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
