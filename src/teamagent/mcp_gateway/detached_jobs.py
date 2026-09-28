"""video_algorithm を「同じツールのまま切り離す（detach）」仕組み。

フラグは USE_VIDEO_ALGORITHM_DETACH（既定 OFF）。

背景（2026-09-25 本番実測）: 動画分析は 5 本で 9 分前後かかる。OpenClaw は約 6 分（360 秒）で
その実行を打ち切るため、mcp が 555 秒かけて完走しても結果の戻り先が無く、
利用者には何も届かなかった。

仕組み:
- gateway（``server.dispatch_tool``）が video_algorithm だけを専用の daemon thread で始める。
- ``VIDEO_ALGORITHM_DETACH_AFTER_S``（既定 30 秒）以内に終われば、今までどおり同期で返す
  （キャッシュヒット・入力エラー・処理中リースの競合はここで返る）。
- 超えたら受付の payload（``status=running``）を返し、ジョブは走らせ続ける。完了したら
  **署名検証済み claim** の channel_id・thread_ts へ mcp が ``slack_summary`` を直接投稿する。
- OpenClaw 側の打ち切り（CancelledError）もジョブには伝えない（shield 相当）。完了時に同じく届ける。
- 二重依頼は、プロセス内の登録簿（キー＝検証済み slack_user_id＋正規化した query）で止める。
- 再デプロイ時は、処理中ジョブの宛先へ「システム更新で中断」を送る（``notify_interrupted``）。

段階公開のための env（どれも TD の env で変えられる）:
- ``USE_VIDEO_ALGORITHM_DETACH``: 既定 OFF＝今と完全に同じ（同期のまま）。
- ``VIDEO_ALGORITHM_DETACH_ALLOWED_EMAILS``: カンマ区切り。**空なら誰にも適用しない**
  （``skills/_shared/rollout.py`` の「空＝全員許可」とは逆。
  第 1 段階は小俣さん本人だけを入れる想定）。
- ``VIDEO_ALGORITHM_DETACH_DM_ONLY``: 既定 1＝1 対 1 DM（D…）だけ。
- ``VIDEO_ALGORITHM_DETACH_AFTER_S``: 既定 30・5〜240 に丸める。
- ``VIDEO_ALGORITHM_MAX_BACKGROUND``: 既定 2。超えたら同期のまま（今と同じ挙動）に落とす。

利用者向けの文には内部語（job_id・error_code・S3 URL・ツール名）を出さない（SOUL.md の禁止語）。
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog

from teamagent.skills._shared.slack_mrkdwn import markdown_bold_to_mrkdwn

logger = structlog.get_logger(__name__)

# 切り離しの対象ツール（いまは動画分析だけ）。
DETACHABLE_TOOLS = frozenset({"video_algorithm"})

ENABLED_ENV = "USE_VIDEO_ALGORITHM_DETACH"
AFTER_ENV = "VIDEO_ALGORITHM_DETACH_AFTER_S"
ALLOWED_EMAILS_ENV = "VIDEO_ALGORITHM_DETACH_ALLOWED_EMAILS"
DM_ONLY_ENV = "VIDEO_ALGORITHM_DETACH_DM_ONLY"
MAX_BACKGROUND_ENV = "VIDEO_ALGORITHM_MAX_BACKGROUND"

DEFAULT_DETACH_AFTER_S = 30.0
MIN_DETACH_AFTER_S = 5.0
MAX_DETACH_AFTER_S = 240.0
DEFAULT_MAX_BACKGROUND = 2
MAX_MAX_BACKGROUND = 10

# 受付文の目安（5 本で 9 分前後の実測に合わせた概算）。
ETA_MINUTES = 10

# 1 対 1 DM の channel_id（personal_memory/gate.py と同じ判定・strip しない）。
_DM_CHANNEL_RE = re.compile(r"D[A-Z0-9]{8,}")
# Slack の message ts（例: 1784424000.000001）。チャンネル直下の依頼をスレッドにする時だけ使う。
_SLACK_TS_RE = re.compile(r"\d{9,}\.\d{1,9}")

# 完了投稿の Slack 往復。1 回あたり短く切り、1 回だけ再試行する。
_POST_TIMEOUT_S = 10
_POST_ATTEMPTS = 2
_POST_RETRY_WAIT_S = 2.0
# 再デプロイ時の中断通知（ECS の stopTimeout 30 秒の枠内に収める）。
_INTERRUPT_POST_TIMEOUT_S = 5
DEFAULT_INTERRUPT_BUDGET_S = 20.0


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes"}


def _float_env(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        value = default
    if value != value:  # NaN
        value = default
    return min(maximum, max(minimum, value))


def _int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return min(maximum, max(minimum, value))


@dataclass(frozen=True)
class DetachPolicy:
    """切り離しを使うかどうかの決まり（env から読む）。既定は「使わない」。"""

    enabled: bool = False
    detach_after_s: float = DEFAULT_DETACH_AFTER_S
    allowed_emails: frozenset[str] = frozenset()
    dm_only: bool = True
    max_background: int = DEFAULT_MAX_BACKGROUND

    @classmethod
    def from_env(cls) -> DetachPolicy:
        dm_raw = os.environ.get(DM_ONLY_ENV)
        return cls(
            enabled=_truthy(os.environ.get(ENABLED_ENV)),
            detach_after_s=_float_env(
                AFTER_ENV,
                DEFAULT_DETACH_AFTER_S,
                minimum=MIN_DETACH_AFTER_S,
                maximum=MAX_DETACH_AFTER_S,
            ),
            allowed_emails=frozenset(
                e.strip().lower()
                for e in os.environ.get(ALLOWED_EMAILS_ENV, "").split(",")
                if e.strip()
            ),
            # 未設定・空は既定 1（DM だけ）。明示の 0/false/no のときだけ外す。
            dm_only=True
            if dm_raw is None or not dm_raw.strip()
            else dm_raw.strip().lower() not in {"0", "false", "no"},
            max_background=_int_env(
                MAX_BACKGROUND_ENV,
                DEFAULT_MAX_BACKGROUND,
                minimum=0,
                maximum=MAX_MAX_BACKGROUND,
            ),
        )


def load_policy() -> DetachPolicy:
    """呼び出しごとに env を読む（TD 差し替えだけで段階を進められる。テストの差し替え口）。"""
    return DetachPolicy.from_env()


@dataclass(frozen=True)
class Destination:
    """完了投稿の宛先。必ず署名検証済み claim から作る（``_user_context`` の申告値は使わない）。"""

    channel_id: str
    thread_ts: str | None

    @property
    def is_dm(self) -> bool:
        return _DM_CHANNEL_RE.fullmatch(self.channel_id) is not None


def is_dm_channel(channel_id: object) -> bool:
    return isinstance(channel_id, str) and _DM_CHANNEL_RE.fullmatch(channel_id) is not None


def destination_from_claim(verified_caller: Any) -> Destination | None:
    """署名検証済み claim から宛先を作る。

    チャンネル直下へは投げない（スレッドが決まらなければ None）。
    """
    channel = getattr(verified_caller, "channel_id", None)
    if not isinstance(channel, str) or not channel:
        return None
    thread_ts = getattr(verified_caller, "thread_ts", None)
    thread_ts = thread_ts if isinstance(thread_ts, str) and thread_ts else None
    if is_dm_channel(channel) or thread_ts:
        return Destination(channel_id=channel, thread_ts=thread_ts)
    # チャンネル直下の依頼: 依頼メッセージ自体を親にしてスレッドへ返す。
    # message_id が Slack の ts であることは実機未確認のため、ts の形のときだけ使う。
    message_id = getattr(verified_caller, "message_id", None)
    if isinstance(message_id, str) and _SLACK_TS_RE.fullmatch(message_id):
        return Destination(channel_id=channel, thread_ts=message_id)
    return None


def decide(
    policy: DetachPolicy,
    *,
    tool: str,
    verified_caller: Any,
    metadata: dict[str, Any],
) -> tuple[Destination | None, str]:
    """切り離してよいかを決める。

    ``(宛先, 理由)`` を返す。宛先が None なら同期のまま（今と同じ）。
    """
    if tool not in DETACHABLE_TOOLS:
        return None, "tool"
    if not policy.enabled:
        return None, "disabled"
    if verified_caller is None:
        # LEGACY（resolver 無し）や未検証の呼び出しは、宛先を信用できないので切り離さない。
        return None, "legacy"
    if metadata.get("identity_verified") is not True:
        return None, "unverified"
    email = metadata.get("user_email")
    # 照合するのは resolver が解決した email（_user_context の申告値ではない）。
    # 空の allowlist は全員拒否。
    if not isinstance(email, str) or email.strip().lower() not in policy.allowed_emails:
        return None, "not_allowed"
    destination = destination_from_claim(verified_caller)
    if destination is None:
        return None, "no_destination"
    if policy.dm_only and not destination.is_dm:
        return None, "not_dm"
    if policy.max_background <= 0:
        return None, "capacity"
    return destination, "ok"


def inflight_key(slack_user_id: str, query: str) -> str:
    """二重依頼の判定キー（検証済み slack_user_id＋正規化した query）。本数などの引数は含めない。"""
    normalized = " ".join(unicodedata.normalize("NFKC", query or "").casefold().split())
    return f"{slack_user_id}\x1f{normalized}"


# ── 利用者向けの文（内部語を出さない）──────────────────────────────────────


def receipt_text(query: str) -> str:
    return (
        f"🔎 「{query}」の動画分析を続けています。"
        f"終わったらこの会話にお届けします（目安 {ETA_MINUTES} 分）。"
    )


def in_progress_text(query: str, *, same_conversation: bool) -> str:
    where = "この会話" if same_conversation else "最初にご依頼いただいた会話"
    return f"🔎 「{query}」はまだ分析中です。終わったら{where}にお届けします。"


def interrupted_text(query: str) -> str:
    return (
        f"🔎 「{query}」の動画分析は、システム更新で中断されました。"
        "お手数ですが、同じ内容でもう一度依頼してください。"
    )


_TEMPORARY_FAILURE = (
    "一時的な不具合で分析を完了できませんでした。数分おいて、同じ内容でもう一度依頼してください。"
)
_MEDIA_FAILURE = (
    "動画の取得・変換で一時的な不具合が起き、分析を完了できませんでした。"
    "数分おいて、同じ内容でもう一度依頼してください。"
)
_GENERIC_FAILURE = (
    "分析の途中で問題が起きて完了できませんでした。数分おいて、同じ内容でもう一度依頼してください。"
)


def user_message_for_error(error: BaseException) -> str:
    """skill の RuntimeError（``CODE: 説明`` 形式）を利用者向けの文に写す（コード名は出さない）。"""
    message = str(error) if isinstance(error, RuntimeError) else ""
    code, sep, rest = message.partition(":")
    code = code.strip() if sep else ""
    rest = rest.strip()
    if code in {"VIDEO_QUOTA_EXCEEDED", "VIDEO_QUOTA_PARTIAL_AVAILABLE"} and rest:
        # quota_store.quota_block_message が作る利用者向けの文面（残数と選択肢）をそのまま使う。
        return rest
    if code == "VIDEO_QUOTA_IDENTITY_REQUIRED":
        return (
            "ご依頼者の確認ができなかったため、分析を始められませんでした。"
            "管理者に連絡してください。"
        )
    if code == "VIDEO_ALGORITHM_IN_PROGRESS":
        return (
            "同じ内容の分析がまだ続いています。数分おいて、同じ内容でもう一度依頼してください"
            "（終わっていれば、すぐに結果をお返しします）。"
        )
    if code in {
        "VIDEO_ALGORITHM_CACHE_UNAVAILABLE",
        "VIDEO_ALGORITHM_LEASE_LOST",
        "VIDEO_ALGORITHM_CACHE_COMMIT_FAILED",
    }:
        return _TEMPORARY_FAILURE
    if code.startswith("MEDIA_") or message.startswith("MEDIA_"):
        return _MEDIA_FAILURE
    return _GENERIC_FAILURE


def is_in_progress_error(error: BaseException) -> bool:
    return isinstance(error, RuntimeError) and str(error).startswith("VIDEO_ALGORITHM_IN_PROGRESS")


def error_text(query: str, error: BaseException) -> str:
    return f"🔎 「{query}」の動画分析: {user_message_for_error(error)}"


def completion_text(output: Any, query: str) -> str:
    """完了投稿の本文。Slack API へ直接出すので `**語**` を mrkdwn の `*語*` に直す。"""
    summary = getattr(output, "slack_summary", "")
    if not isinstance(summary, str) or not summary.strip():
        summary = f"🔎 「{query}」の動画分析が完了しました。"
    return markdown_bold_to_mrkdwn(summary)


# ── Slack 投稿（既存の直接配信と同じ SlackClient.from_env(timeout_seconds=...)）──────


def _slack_client(timeout_seconds: int) -> Any:
    """共有 Bot Token（SLACK_BOT_TOKEN）の SlackClient。テストはここを差し替える。"""
    from teamagent.adapters.slack_client import SlackClient

    return SlackClient.from_env(timeout_seconds=timeout_seconds)


async def _post_once(
    text: str, destination: Destination, *, request_id: str, timeout_s: int
) -> bool:
    slack = _slack_client(timeout_s)
    result = await asyncio.wait_for(
        slack.post_message(
            channel=destination.channel_id,
            text=text,
            request_id=request_id,
            thread_ts=destination.thread_ts,
        ),
        timeout=timeout_s + 1,
    )
    return bool(getattr(result, "ok", False))


def post_to_origin(text: str, destination: Destination, *, request_id: str) -> bool:
    """依頼元の会話へ投稿する（ジョブの thread から呼ぶ・新しい event loop で 1 回だけ再試行）。"""
    for attempt in range(1, _POST_ATTEMPTS + 1):
        try:
            if asyncio.run(
                _post_once(text, destination, request_id=request_id, timeout_s=_POST_TIMEOUT_S)
            ):
                logger.info(
                    "video_algorithm_detach_posted",
                    request_id=request_id,
                    attempt=attempt,
                    dm=destination.is_dm,
                )
                return True
            logger.warning(
                "video_algorithm_detach_post_not_ok", request_id=request_id, attempt=attempt
            )
        except Exception as exc:
            logger.warning(
                "video_algorithm_detach_post_failed",
                request_id=request_id,
                attempt=attempt,
                error=type(exc).__name__,
            )
        if attempt < _POST_ATTEMPTS:
            threading.Event().wait(_POST_RETRY_WAIT_S)
    return False


# ── ジョブと登録簿 ─────────────────────────────────────────────────────────

# 完了時（切り離し後）の処理: (result, error, interrupted) を受け取る。gateway が渡す。
DetachedDone = Callable[[Any, BaseException | None, bool], None]


class DetachedJob:
    """1 回の video_algorithm 実行。状態の出入りはすべて lock の下で行う。"""

    def __init__(
        self,
        *,
        registry: DetachedJobRegistry,
        key: str,
        tool: str,
        query: str,
        request_id: str,
        destination: Destination,
        target: Callable[[], Any],
        on_detached_done: DetachedDone,
    ) -> None:
        self.key = key
        self.tool = tool
        self.query = query
        self.request_id = request_id
        self.destination = destination
        self._registry = registry
        self._target = target
        self._on_detached_done = on_detached_done
        self._lock = threading.Lock()
        self._done = False
        self._detached = False
        self._interrupted = False
        self._handled = False
        self._result: Any = None
        self._error: BaseException | None = None
        self._wakers: list[Callable[[], None]] = []

    # 状態の参照（テスト・ログ用）
    @property
    def done(self) -> bool:
        with self._lock:
            return self._done

    @property
    def detached(self) -> bool:
        with self._lock:
            return self._detached

    def start(self) -> None:
        thread = threading.Thread(
            target=self._run,
            name=f"{self.tool}-detach-{self.request_id}",
            daemon=True,
        )
        thread.start()

    def _run(self) -> None:
        result: Any = None
        error: BaseException | None = None
        try:
            result = self._target()
        except BaseException as exc:  # thread の外へ漏らさず、結果として扱う
            error = exc
        with self._lock:
            self._done = True
            self._result = result
            self._error = error
            detached = self._detached
            wakers = list(self._wakers)
            self._wakers.clear()
        try:
            if detached:
                self._finish_detached()
            else:
                for wake in wakers:
                    try:
                        wake()
                    except Exception as exc:
                        logger.warning(
                            "video_algorithm_detach_wake_failed",
                            request_id=self.request_id,
                            error=type(exc).__name__,
                        )
        finally:
            self._registry.release(self)

    def add_waker(self, wake: Callable[[], None]) -> None:
        """完了時に呼ぶ関数を登録する（すでに完了していれば即呼ぶ）。"""
        with self._lock:
            if not self._done:
                self._wakers.append(wake)
                return
        wake()

    def detach(self) -> bool:
        """切り離す。

        まだ終わっていなければ True（完了時に thread が届ける）。終わっていれば False。
        """
        with self._lock:
            if self._done:
                return False
            self._detached = True
            return True

    def outcome(self) -> Any:
        """完了済みジョブの結果を返す（失敗なら skill の例外をそのまま送出する）。"""
        with self._lock:
            if not self._done:
                raise RuntimeError("detached job is still running")
            error = self._error
            result = self._result
        if error is not None:
            raise error
        return result

    def deliver_in_background(self) -> None:
        """完了済みなのに受け取り手が居なくなった（打ち切られた）ときに、別 thread で届ける。"""
        with self._lock:
            self._detached = True
        thread = threading.Thread(
            target=self._finish_detached,
            name=f"{self.tool}-detach-deliver-{self.request_id}",
            daemon=True,
        )
        thread.start()

    def mark_interrupted(self) -> bool:
        """再デプロイで中断扱いにする（切り離し済み・未完了のジョブだけ True）。"""
        with self._lock:
            if self._done or not self._detached or self._interrupted:
                return False
            self._interrupted = True
            return True

    def _finish_detached(self) -> None:
        with self._lock:
            if self._handled:
                return
            self._handled = True
            result = self._result
            error = self._error
            interrupted = self._interrupted
        try:
            self._on_detached_done(result, error, interrupted)
        except Exception as exc:
            logger.warning(
                "video_algorithm_detach_finish_failed",
                request_id=self.request_id,
                error=type(exc).__name__,
            )


class DetachedJobRegistry:
    """プロセス内の in-flight 登録簿（キー＝検証済み slack_user_id＋正規化 query）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, DetachedJob] = {}

    def get(self, key: str) -> DetachedJob | None:
        with self._lock:
            return self._jobs.get(key)

    def active_count(self) -> int:
        with self._lock:
            return len(self._jobs)

    def start(
        self,
        *,
        key: str,
        max_background: int,
        tool: str,
        query: str,
        request_id: str,
        destination: Destination,
        target: Callable[[], Any],
        on_detached_done: DetachedDone,
    ) -> tuple[DetachedJob | None, str]:
        """登録して開始する。

        返り値は ``(job, "started")`` / ``(既存 job, "duplicate")`` / ``(None, "capacity")``。
        """
        with self._lock:
            existing = self._jobs.get(key)
            if existing is not None:
                return existing, "duplicate"
            if len(self._jobs) >= max_background:
                return None, "capacity"
            job = DetachedJob(
                registry=self,
                key=key,
                tool=tool,
                query=query,
                request_id=request_id,
                destination=destination,
                target=target,
                on_detached_done=on_detached_done,
            )
            self._jobs[key] = job
        try:
            job.start()
        except BaseException:
            self.release(job)
            raise
        return job, "started"

    def release(self, job: DetachedJob) -> None:
        with self._lock:
            if self._jobs.get(job.key) is job:
                del self._jobs[job.key]

    def interrupt_all(self) -> list[DetachedJob]:
        with self._lock:
            jobs = list(self._jobs.values())
        return [job for job in jobs if job.mark_interrupted()]


REGISTRY = DetachedJobRegistry()


async def notify_interrupted(
    *,
    budget_s: float = DEFAULT_INTERRUPT_BUDGET_S,
    registry: DetachedJobRegistry | None = None,
) -> int:
    """再デプロイ（プロセス終了）時に、処理中ジョブの宛先へ中断を知らせる。

    何度呼んでも 1 回だけ送る。
    """
    reg = registry or REGISTRY
    jobs = reg.interrupt_all()
    if not jobs:
        return 0
    logger.warning("video_algorithm_detach_interrupted", count=len(jobs))

    async def _one(job: DetachedJob) -> None:
        try:
            ok = await _post_once(
                interrupted_text(job.query),
                job.destination,
                request_id=job.request_id,
                timeout_s=_INTERRUPT_POST_TIMEOUT_S,
            )
            logger.info(
                "video_algorithm_detach_interrupt_notified", request_id=job.request_id, ok=ok
            )
        except Exception as exc:
            logger.warning(
                "video_algorithm_detach_interrupt_notify_failed",
                request_id=job.request_id,
                error=type(exc).__name__,
            )

    try:
        await asyncio.wait_for(asyncio.gather(*(_one(job) for job in jobs)), timeout=budget_s)
    except TimeoutError:
        logger.warning("video_algorithm_detach_interrupt_budget_exceeded", count=len(jobs))
    return len(jobs)


__all__ = [
    "DETACHABLE_TOOLS",
    "REGISTRY",
    "Destination",
    "DetachPolicy",
    "DetachedJob",
    "DetachedJobRegistry",
    "completion_text",
    "decide",
    "destination_from_claim",
    "error_text",
    "in_progress_text",
    "inflight_key",
    "interrupted_text",
    "is_dm_channel",
    "is_in_progress_error",
    "load_policy",
    "notify_interrupted",
    "post_to_origin",
    "receipt_text",
    "user_message_for_error",
]
