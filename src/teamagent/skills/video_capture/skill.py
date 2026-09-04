"""video_capture Skill 本体 — 動画の指定時刻を JPEG で切り出して Slack に返す。

経路: Slack 自由文（「このTikTokの0:05と0:12を画像にして」「さっき貼った動画の1:20切り出して」）
→ OpenClaw → SOUL 指示で本ツール → media worker（acquire / frame）→ 依頼スレッドへ添付。

入口は 2 つ（排他）:
  A. url          … TikTok / Instagram の公開 URL を media worker が取得（allowlist 二重検証）
  B. slack_file   … この会話に添付された動画ファイル（取得元ブロックの影響を受けない確実な経路）
どちらも同じ ``extract_frames``（kind='frame'）に合流する。

⚠️ 死守ライン:
  G1 本人限定: ``ctx.metadata['user_email']`` を fail-closed 検証（MCP 外殻の注入値のみ信用）。
  配信先は依頼スレッドか依頼者本人 DM のみ（knowledge_deliver と同型）。
  失敗を「0枚でした」に化けさせない: media のエラーコードは schema の写像表で必ず言語化する。
  範囲外 timecode は 1 点でもジョブ全体が落ちる（実測: MEDIA_PROCESS_FAILED）。
  取得バイトは URL 経路 80MB / 添付経路 100MB で上限。添付は逐次サイズ検査つき DL。
  同時実行は semaphore で絞る（mcp Fargate 3GB・16名共用で OOM させない）。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

import structlog
from pydantic import BaseModel

from teamagent.media.url_policy import acquire_host_allowed
from teamagent.skills._shared.user_context import USER_CONTEXT_RULE
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.video_capture.attachment import attachment_download_url, select_video_file
from teamagent.skills.video_capture.schema import (
    SKILL_ERROR_MESSAGES,
    CapturedFrame,
    VideoCaptureInput,
    VideoCaptureOutput,
    format_timecode,
    media_error_message,
)

logger = structlog.get_logger(__name__)

# --- 上限とタイムアウト配分 -------------------------------------------------
# 実測（2026-08-17 本番 media worker）: 1 ジョブの往復は ECS 起動込みで 69〜93秒
# （frame 3点=77.4s / acquire=75.9〜93.2s）。acquire と frame の 2 ジョブ直列なので、
# OpenClaw の全ツール共通天井 600s に対し「acquire 240 + frame 180 = 420s（天井の70%）
# + Slack upload 残余」で配分する。frame 側に 180 を割くのは worker の ffmpeg 実行が
# 1 点あたり最大 60s の上限を持ち、最大12点だと起動オーバヘッド込みで 150s を超え得るため。
# video_algorithm の 300s 打ち切り事故と同型の再演を避けるための明示配分。
_ACQUIRE_TIMEOUT_S = 240
_FRAMES_TIMEOUT_S = 180
# exam fix: acquire の既定 30MB は 360p 数分で足りない。契約上限 128MB の内側で 80MB。
_ACQUIRE_MAX_BYTES = 80 * 1024 * 1024
# 添付経路は取得元ブロックが無く長尺が来やすいので 100MB まで許す（契約 128MB の内側）。
_SLACK_FILE_MAX_MB_DEFAULT = 100
_SLACK_FILE_MAX_MB_CEILING = 128
_ATTACHMENT_HISTORY_LIMIT = 30

_SEMAPHORE_LOCK = threading.Lock()
_SEMAPHORE: threading.BoundedSemaphore | None = None


def _envint(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return value if minimum <= value <= maximum else default


def _capture_semaphore() -> threading.BoundedSemaphore:
    """重い切出しの同時実行数（既定2）。3GB 共有コンテナの総量規制。"""

    global _SEMAPHORE
    with _SEMAPHORE_LOCK:
        if _SEMAPHORE is None:
            _SEMAPHORE = threading.BoundedSemaphore(
                _envint("VIDEO_CAPTURE_MAX_CONCURRENCY", 2, minimum=1, maximum=8)
            )
        return _SEMAPHORE


def slack_file_max_bytes() -> int:
    """添付動画の上限バイト（既定100MB・env で 1〜128MB に調整可）。"""

    megabytes = _envint(
        "VIDEO_CAPTURE_SLACK_MAX_MB",
        _SLACK_FILE_MAX_MB_DEFAULT,
        minimum=1,
        maximum=_SLACK_FILE_MAX_MB_CEILING,
    )
    return megabytes * 1024 * 1024


def _is_youtube(host: str) -> bool:
    return any(
        host == suffix or host.endswith(f".{suffix}") for suffix in ("youtube.com", "youtu.be")
    )


def youtube_enabled() -> bool:
    """YouTube URL を受けるか。

    **既定 OFF**。2026-08-17 の本番スパイクで短尺・長尺とも yt-dlp が
    「Sign in to confirm you're not a bot」で拒否され MEDIA_ACQUIRE_FAILED が確定した。
    90秒待たせてから汎用エラーを返すより、入口で決定的に案内する方が誠実。
    cookie / PO_TOKEN 対応が入ったら env で戻せるようコード側は残す。
    """

    return os.environ.get("VIDEO_CAPTURE_ALLOW_YOUTUBE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def url_policy_error(url: str) -> str:
    """URL 入力を skill 層で先に検証する（media 契約に入る前の一次防壁）。"""

    parsed = urlsplit(url)
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
        return "unsupported_url"
    try:
        if parsed.port not in (None, 443):
            return "unsupported_url"
    except ValueError:
        return "unsupported_url"
    if not acquire_host_allowed(host):
        return "unsupported_url"
    if _is_youtube(host) and not youtube_enabled():
        return "youtube_blocked"
    return ""


@register
class VideoCaptureSkill(BaseSkill[VideoCaptureInput, VideoCaptureOutput]):
    """動画の指定時刻を JPEG で切り出し、依頼スレッド（無ければ本人 DM）へ添付する。"""

    name: ClassVar[str] = "video_capture"
    description: ClassVar[str] = (
        "動画の「◯分◯秒のシーンを画像にして／切り出して／サムネ用に出して」に応えるツール。"
        "指定時刻のフレームを JPEG にして、聞かれたスレッド（無ければ本人 DM）に添付する。"
        "対象は 2 通り: (1) TikTok / Instagram の URL を url に入れる、"
        "(2) この会話に添付された動画なら url を空にして slack_file=true にする"
        "（YouTube は取得元にブロックされるため、ファイル添付でお願いする）。"
        "timecodes はユーザーの言い方のまま渡してよい（「0:05」「1:02:03」「5」いずれも可・"
        "秒への換算はサーバがやるので自分で計算しないこと）。最大12点。"
        "『なぜ勝ってるか分析して』は video_algorithm、"
        "『◯秒のシーンを切り出して・画像にして』が本ツール。"
        "返ってきた message はそのまま返すこと（枚数・時刻を言い換えない）。" + USER_CONTEXT_RULE
    )
    input_schema: ClassVar[type[BaseModel]] = VideoCaptureInput
    output_schema: ClassVar[type[BaseModel]] = VideoCaptureOutput

    def __init__(
        self,
        *,
        media: Any = None,
        slack: Any = None,
        slack_reader: Any = None,
    ) -> None:
        self._media = media
        self._slack = slack
        self._slack_reader = slack_reader

    # ------------------------------------------------------------------
    def run(self, input: VideoCaptureInput, ctx: SkillContext) -> VideoCaptureOutput:
        log = ctx.bind_logger(self.name)

        # ① G1: 本人限定（fail-closed）。MCP 外殻が slack_user_id→email を解決して注入する。
        requester = str(ctx.metadata.get("user_email", "") or "").strip()
        if not requester:
            raise PermissionError("video_capture は本人 user_email が必須です")

        channel_id = ctx.metadata.get("channel_id")
        channel_id = channel_id if isinstance(channel_id, str) and channel_id else None
        thread_ts = ctx.metadata.get("thread_ts")
        thread_ts = thread_ts if isinstance(thread_ts, str) and thread_ts else None

        source_kind = "slack_file" if input.slack_file else "url"

        # ② URL 経路は重い処理に入る前に決定的に弾く（90秒待たせて汎用エラーにしない）。
        if source_kind == "url":
            policy_error = url_policy_error(input.url.strip())
            if policy_error:
                log.info("video_capture_url_rejected", reason=policy_error)
                return self._skill_failure(policy_error, source_kind)

        media = self._media or self._build_media()
        if media is None:
            return self._skill_failure("media_unavailable", source_kind)

        semaphore = _capture_semaphore()
        if not semaphore.acquire(blocking=False):
            log.info("video_capture_busy")
            return self._skill_failure("busy", source_kind)
        try:
            return self._run_bounded(
                input,
                ctx,
                log,
                media=media,
                source_kind=source_kind,
                requester=requester,
                channel_id=channel_id,
                thread_ts=thread_ts,
            )
        finally:
            semaphore.release()

    # ------------------------------------------------------------------
    def _run_bounded(
        self,
        input: VideoCaptureInput,
        ctx: SkillContext,
        log: Any,
        *,
        media: Any,
        source_kind: str,
        requester: str,
        channel_id: str | None,
        thread_ts: str | None,
    ) -> VideoCaptureOutput:
        from teamagent.adapters.media_job import MediaJobError

        fingerprint = f"video_capture:{ctx.request_id}"

        # ③ 素材の取得（URL / 添付の 2 入口 → 同じ bytes に合流）。
        if source_kind == "slack_file":
            data, mime, attach_error = self._load_attachment(
                input,
                log=log,
                channel_id=channel_id,
                thread_ts=thread_ts,
                request_id=ctx.request_id,
            )
            if attach_error:
                return self._skill_failure(attach_error, source_kind)
        else:
            try:
                data, mime = media.acquire_video(
                    input.url.strip(),
                    request_fingerprint=f"{fingerprint}:acquire",
                    max_bytes=_ACQUIRE_MAX_BYTES,
                    timeout_s=_ACQUIRE_TIMEOUT_S,
                )
            except MediaJobError as exc:
                log.warning("video_capture_acquire_failed", code=str(exc))
                return self._media_failure(str(exc), source_kind)
            except Exception:
                log.warning("video_capture_acquire_error")
                return self._media_failure("MEDIA_ACQUIRE_FAILED", source_kind)

        if not data:
            return self._media_failure("MEDIA_ACQUIRE_FAILED", source_kind)

        # ④ 切出し。範囲外 timecode が 1 点でも混ざるとジョブ全体が落ちる（実測）。
        wanted = list(input.seconds)
        try:
            extracted = media.extract_frames(
                data,
                mime,
                wanted,
                request_fingerprint=f"{fingerprint}:frames",
                width=input.width,
                timeout_s=_FRAMES_TIMEOUT_S,
            )
        except MediaJobError as exc:
            log.warning("video_capture_frames_failed", code=str(exc))
            return self._media_failure(str(exc), source_kind)
        except Exception:
            log.warning("video_capture_frames_error")
            return self._media_failure("MEDIA_PROCESS_FAILED", source_kind)

        # 全点そろわない返り（部分成功）を「切り出せました」に化けさせない。
        if len(extracted) != len(wanted):
            log.warning(
                "video_capture_frames_incomplete",
                wanted=len(wanted),
                got=len(extracted),
            )
            return self._media_failure("MEDIA_FRAME_EMPTY", source_kind)

        frames = [CapturedFrame(label=format_timecode(sec), seconds=sec) for sec, _ in extracted]

        # ⑤ 配信（依頼スレッド → 無ければ本人 DM）。knowledge_deliver と同型。
        #    requester は上で fail-closed 済み＝DM フォールバック先は必ずある。
        tmpdir = tempfile.mkdtemp(prefix="aila_video_capture_")
        try:
            prepared: list[tuple[int, str]] = []
            try:
                for index, (sec, body) in enumerate(extracted):
                    label = format_timecode(sec)
                    path = Path(tmpdir) / f"{index:02d}_{label.replace(':', '-')}.jpg"
                    path.write_bytes(body)
                    prepared.append((index, str(path)))
            except OSError:
                log.warning("video_capture_tmpwrite_failed")
                return self._skill_failure("delivery_failed", source_kind, frames=frames)
            try:
                delivered, where = asyncio.run(
                    self._deliver(
                        prepared=prepared,
                        frames=frames,
                        request_id=ctx.request_id,
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        email=requester,
                    )
                )
            except Exception:
                log.warning("video_capture_delivery_error")
                delivered, where = set(), ""
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        if not delivered:
            return self._skill_failure("delivery_failed", source_kind, frames=frames)

        for index in delivered:
            frames[index].delivered = True
        labels = " / ".join(frame.label for frame in frames if frame.delivered)
        destination = "このスレッド" if where == "thread" else "あなたの DM"
        log.info(
            "video_capture_done",
            source_kind=source_kind,
            requested=len(wanted),
            delivered=len(delivered),
            where=where,
        )
        return VideoCaptureOutput(
            source_kind=source_kind,
            frames=frames,
            delivered_count=len(delivered),
            delivered_to=where,
            message=(
                f"🎬 動画から {len(delivered)} 枚を切り出して"
                f"{destination}にお出ししました（{labels}）。"
            ),
        )

    # ------------------------------------------------------------------
    def _load_attachment(
        self,
        input: VideoCaptureInput,
        *,
        log: Any,
        channel_id: str | None,
        thread_ts: str | None,
        request_id: str,
    ) -> tuple[bytes, str, str]:
        """会話の添付から動画 bytes を得る。返り値 ``(data, mime, error)``。"""

        if not channel_id:
            return b"", "", "attachment_not_found"
        max_bytes = slack_file_max_bytes()
        reader = self._slack_reader or self._build_slack_reader()
        if reader is None:
            return b"", "", "attachment_not_found"

        # 「会話を読めなかった」と「読めたが動画が無かった」を混同しない。
        # 混同すると、bot に im:history / channels:history が無いだけの構成ミスが
        # 「動画が見つかりません」に化けて永久に診断されない（注記を事実として報告するな）。
        messages: list[Any] = []
        read_ok = False
        if thread_ts:
            try:
                messages.extend(
                    reader.list_thread_replies(channel_id, thread_ts, request_id).messages
                )
                read_ok = True
            except Exception:
                log.warning("video_capture_thread_read_failed")
        file, reason = select_video_file(messages, file_id=input.slack_file_id, max_bytes=max_bytes)
        if file is None and reason == "not_found":
            # スレッド内に無ければチャンネル/DM の直近も見る（スレッド外に貼られた動画）。
            try:
                messages.extend(
                    reader.list_channel_history(
                        channel_id, request_id, limit=_ATTACHMENT_HISTORY_LIMIT
                    ).messages
                )
                read_ok = True
            except Exception:
                log.warning("video_capture_history_read_failed")
            file, reason = select_video_file(
                messages, file_id=input.slack_file_id, max_bytes=max_bytes
            )
        if file is None:
            if not read_ok:
                log.warning("video_capture_conversation_unreadable")
                return b"", "", "conversation_read_failed"
            log.info("video_capture_attachment_missing", reason=reason)
            error = "attachment_too_large" if reason == "too_large" else "attachment_not_found"
            return b"", "", error

        slack = self._slack or self._build_slack()
        try:
            data = asyncio.run(
                slack.download_file_bounded(
                    attachment_download_url(file),
                    max_bytes=max_bytes,
                    request_id=request_id,
                )
            )
        except Exception as exc:
            if "TOO_LARGE" in str(exc):
                log.info("video_capture_attachment_oversized")
                return b"", "", "attachment_too_large"
            log.warning("video_capture_attachment_download_failed")
            return b"", "", "attachment_failed"
        return data, str(file.get("mimetype") or "video/mp4"), ""

    async def _deliver(
        self,
        *,
        prepared: list[tuple[int, str]],
        frames: list[CapturedFrame],
        request_id: str,
        channel_id: str | None,
        thread_ts: str | None,
        email: str | None,
    ) -> tuple[set[int], str]:
        """依頼スレッド → 本人 DM の順に添付する（knowledge_deliver と同型）。"""

        slack = self._slack or self._build_slack()
        labels = " / ".join(frame.label for frame in frames)
        comment = f"🎬 {len(prepared)} 枚を切り出しました（{labels}）。"

        if channel_id:
            delivered = await self._upload_all(
                slack, channel_id, thread_ts, prepared, frames, comment, request_id
            )
            if delivered:
                return delivered, "thread"

        if email:
            user_id = await slack.lookup_user_id_by_email(email, request_id)
            if user_id:
                dm = await slack.open_dm(user_id, request_id)
                if dm:
                    delivered = await self._upload_all(
                        slack, dm, None, prepared, frames, comment, request_id
                    )
                    if delivered:
                        return delivered, "dm"
        return set(), ""

    @staticmethod
    async def _upload_all(
        slack: Any,
        channel: str,
        thread_ts: str | None,
        prepared: list[tuple[int, str]],
        frames: list[CapturedFrame],
        comment: str,
        request_id: str,
    ) -> set[int]:
        delivered: set[int] = set()
        for order, (index, path) in enumerate(prepared):
            ok = await slack.upload_file(
                channel,
                path,
                request_id,
                title=frames[index].label,
                initial_comment=comment if order == 0 else None,
                thread_ts=thread_ts,
            )
            if ok:
                delivered.add(index)
        return delivered

    # ------------------------------------------------------------------
    @staticmethod
    def _skill_failure(
        error: str,
        source_kind: str,
        *,
        frames: list[CapturedFrame] | None = None,
    ) -> VideoCaptureOutput:
        template = SKILL_ERROR_MESSAGES[error]
        message = template.format(limit_mb=slack_file_max_bytes() // (1024 * 1024))
        return VideoCaptureOutput(
            source_kind=source_kind,
            frames=frames or [],
            error=error,
            message=message,
        )

    @staticmethod
    def _media_failure(code: str, source_kind: str) -> VideoCaptureOutput:
        message = media_error_message(code)
        if message is None:
            # 写像に無いコードでも「0枚でした」に化けさせない（必ず失敗として言う）。
            message = "動画の処理に失敗しました（時間をおいて再度お試しください）。"
        return VideoCaptureOutput(source_kind=source_kind, error=code, message=message)

    # --- 遅延生成（factory が注入しない / 本番起動時のフォールバック） ---

    def _build_media(self) -> Any:
        from teamagent.adapters.media_job import MediaJobClient

        if not MediaJobClient.is_configured():
            return None
        return MediaJobClient()

    def _build_slack(self) -> Any:
        from teamagent.adapters.slack_client import SlackClient

        return SlackClient.from_env()

    def _build_slack_reader(self) -> Any:
        from teamagent.adapters.slack_channel_ingest_client import SlackChannelIngestClient

        try:
            return SlackChannelIngestClient.from_env()
        except Exception:
            return None
