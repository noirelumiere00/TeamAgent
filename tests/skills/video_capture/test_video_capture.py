"""video_capture Skill のテスト。

フェイク media worker は **本番の失敗モードをそのまま再現**する:
- YouTube は取得元 bot 判定で MEDIA_ACQUIRE_FAILED（2026-08-17 本番スパイク実測）
- 範囲外 timecode は 1 点でも **ジョブ全体**が MEDIA_PROCESS_FAILED で落ちる
  （実測。ffmpeg が "Nothing was written into output file" で非ゼロ終了するため
  operations._run が MEDIA_FRAME_EMPTY より先に MEDIA_PROCESS_FAILED を上げる）
部分成功で「N枚切り出せました」に化ける経路が無いことを、写像表を壊す変異でも確認する。
"""

from __future__ import annotations

import re
import threading
from typing import Any

import pytest
from pydantic import ValidationError

from teamagent.adapters.media_job import MediaJobError
from teamagent.adapters.slack_channel_ingest_client import HistoryBatch, SlackMessage
from teamagent.skills.base import SkillContext
from teamagent.skills.video_capture import skill as skill_module
from teamagent.skills.video_capture.schema import (
    MEDIA_ERROR_MESSAGES,
    VideoCaptureInput,
    format_timecode,
    parse_timecode,
)
from teamagent.skills.video_capture.skill import VideoCaptureSkill

TIKTOK_URL = "https://www.tiktok.com/@complex/video/7626254334065511711"
YOUTUBE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
INSTAGRAM_URL = "https://www.instagram.com/reel/CzTWjU5K8Hl/"

JPEG = b"\xff\xd8\xff" + b"0" * 64


@pytest.fixture(autouse=True)
def _reset_semaphore() -> Any:
    skill_module._SEMAPHORE = None
    yield
    skill_module._SEMAPHORE = None


# ---------------------------------------------------------------------------
# フェイク（本番の失敗モードを再現する）
# ---------------------------------------------------------------------------
class FakeMedia:
    """media worker の acquire/frame を、実測どおりの失敗モードで再現する。"""

    def __init__(
        self,
        *,
        duration_s: float = 8.0,
        media_bytes: bytes = b"VIDEO-BYTES",
        acquire_error: str | None = None,
        frames_error: str | None = None,
        drop_frames: int = 0,
    ) -> None:
        self.duration_s = duration_s
        self.media_bytes = media_bytes
        self.acquire_error = acquire_error
        self.frames_error = frames_error
        self.drop_frames = drop_frames
        self.acquire_calls: list[dict[str, Any]] = []
        self.frame_calls: list[dict[str, Any]] = []

    def acquire_video(
        self,
        url: str,
        *,
        request_fingerprint: str,
        max_bytes: int,
        timeout_s: int,
    ) -> tuple[bytes, str]:
        self.acquire_calls.append(
            {
                "url": url,
                "max_bytes": max_bytes,
                "timeout_s": timeout_s,
                "fingerprint": request_fingerprint,
            }
        )
        if self.acquire_error:
            raise MediaJobError(self.acquire_error)
        # 本番同様: yt-dlp が YouTube を bot 判定で拒否する。
        if "youtube.com" in url or "youtu.be" in url:
            raise MediaJobError("MEDIA_ACQUIRE_FAILED")
        if len(self.media_bytes) > max_bytes:
            raise MediaJobError("MEDIA_ACQUIRE_SIZE_EXCEEDED")
        return self.media_bytes, "video/mp4"

    def extract_frames(
        self,
        data: bytes,
        mime: str,
        timecodes: list[float],
        *,
        request_fingerprint: str,
        width: int,
        timeout_s: int,
    ) -> list[tuple[float, bytes]]:
        self.frame_calls.append(
            {
                "data": data,
                "mime": mime,
                "timecodes": list(timecodes),
                "width": width,
                "timeout_s": timeout_s,
                "fingerprint": request_fingerprint,
            }
        )
        if self.frames_error:
            raise MediaJobError(self.frames_error)
        # 本番の _frames はループ内で 1 点でも失敗したらジョブごと落ちる（部分返しは無い）。
        if any(sec >= self.duration_s for sec in timecodes):
            raise MediaJobError("MEDIA_PROCESS_FAILED")
        produced = [(sec, JPEG) for sec in sorted(set(timecodes))]
        return produced[: len(produced) - self.drop_frames] if self.drop_frames else produced


class FakeSlack:
    def __init__(self, *, upload_ok: bool = True, thread_upload_ok: bool = True) -> None:
        self.upload_ok = upload_ok
        self.thread_upload_ok = thread_upload_ok
        self.uploads: list[dict[str, Any]] = []
        self.downloads: list[dict[str, Any]] = []
        self.download_error: Exception | None = None
        self.download_bytes = b"SLACK-VIDEO-BYTES"
        self.dm_channel = "D999"
        self.user_id: str | None = "U777"

    async def upload_file(
        self,
        channel: str,
        file_path: str,
        request_id: str,
        *,
        title: str | None = None,
        initial_comment: str | None = None,
        thread_ts: str | None = None,
    ) -> bool:
        ok = self.upload_ok and (self.thread_upload_ok or channel.startswith("D"))
        self.uploads.append(
            {
                "channel": channel,
                "path": file_path,
                "title": title,
                "initial_comment": initial_comment,
                "thread_ts": thread_ts,
                "ok": ok,
            }
        )
        return ok

    async def lookup_user_id_by_email(self, email: str, request_id: str) -> str | None:
        return self.user_id

    async def open_dm(self, user_id: str, request_id: str) -> str | None:
        return self.dm_channel

    async def download_file_bounded(
        self, url: str, *, max_bytes: int, request_id: str | None = None
    ) -> bytes:
        self.downloads.append({"url": url, "max_bytes": max_bytes})
        if self.download_error is not None:
            raise self.download_error
        return self.download_bytes


class FakeReader:
    def __init__(
        self,
        *,
        thread: list[SlackMessage] | None = None,
        history: list[SlackMessage] | None = None,
        read_error: Exception | None = None,
    ) -> None:
        self.thread = thread or []
        self.history = history or []
        # 本番の失敗モード: bot に im:history / channels:history が無いと
        # conversations.* が missing_scope で例外になる（「動画0件」ではない）。
        self.read_error = read_error
        self.calls: list[str] = []

    def list_thread_replies(
        self, channel_id: str, thread_ts: str, request_id: str, **kwargs: Any
    ) -> HistoryBatch:
        self.calls.append("thread")
        if self.read_error is not None:
            raise self.read_error
        return HistoryBatch(messages=tuple(self.thread))

    def list_channel_history(self, channel_id: str, request_id: str, **kwargs: Any) -> HistoryBatch:
        self.calls.append("history")
        if self.read_error is not None:
            raise self.read_error
        return HistoryBatch(messages=tuple(self.history))


def video_file(
    *,
    file_id: str = "F111",
    mimetype: str = "video/mp4",
    size: int = 1024,
    external: bool = False,
    url: str = "https://files.slack.com/files-pri/T1-F111/movie.mp4",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": file_id,
        "mimetype": mimetype,
        "size": size,
        "url_private_download": url,
        "url_private": url,
    }
    if external:
        payload["is_external"] = True
        payload["external_type"] = "gdrive"
    return payload


def make_ctx(
    *,
    email: str | None = "sales@example.com",
    channel: str | None = "C1",
    thread: str | None = "1700.1",
) -> SkillContext:
    metadata: dict[str, Any] = {}
    if email is not None:
        metadata["user_email"] = email
    if channel is not None:
        metadata["channel_id"] = channel
    if thread is not None:
        metadata["thread_ts"] = thread
    return SkillContext(request_id="req-test", metadata=metadata)


def make_skill(media: Any = None, slack: Any = None, reader: Any = None) -> VideoCaptureSkill:
    return VideoCaptureSkill(
        media=media or FakeMedia(),
        slack=slack or FakeSlack(),
        slack_reader=reader or FakeReader(),
    )


# ---------------------------------------------------------------------------
# schema: timecode の決定的パース（LLM に算術させない）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0:05", 5.0),
        ("1:02:03", 3723.0),
        ("12:30", 750.0),
        ("5", 5.0),
        ("5.5", 5.5),
        (7, 7.0),
        (7.25, 7.25),
        ("0:05.250", 5.25),
    ],
)
def test_parse_timecode_is_deterministic(raw: Any, expected: float) -> None:
    assert parse_timecode(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["", "abc", "1:2:3:4", "0:75", "99:99", -1, 6 * 3600 + 1, True])
def test_parse_timecode_rejects_garbage(raw: Any) -> None:
    with pytest.raises(ValueError):
        parse_timecode(raw)


@pytest.mark.parametrize(
    ("seconds", "label"),
    [(5.0, "0:05"), (65.0, "1:05"), (3723.0, "1:02:03"), (5.25, "0:05.25")],
)
def test_format_timecode(seconds: float, label: str) -> None:
    assert format_timecode(seconds) == label


def test_timecodes_are_sorted_and_deduped() -> None:
    parsed = VideoCaptureInput(url=TIKTOK_URL, timecodes=["0:05", 1, "0:05", "0:03"])
    assert parsed.seconds == (1.0, 3.0, 5.0)


def test_source_inputs_are_exclusive() -> None:
    with pytest.raises(ValidationError):
        VideoCaptureInput(url=TIKTOK_URL, slack_file=True, timecodes=[1.0])
    with pytest.raises(ValidationError):
        VideoCaptureInput(timecodes=[1.0])
    with pytest.raises(ValidationError):
        VideoCaptureInput(url=TIKTOK_URL, slack_file_id="F123", timecodes=[1.0])
    assert VideoCaptureInput(slack_file=True, slack_file_id="F123", timecodes=[1.0]).slack_file


def test_too_many_timecodes_rejected() -> None:
    with pytest.raises(ValidationError):
        VideoCaptureInput(url=TIKTOK_URL, timecodes=[float(i) for i in range(13)])


# ---------------------------------------------------------------------------
# 本人限定（fail-closed）
# ---------------------------------------------------------------------------
def test_missing_user_email_is_fail_closed() -> None:
    media = FakeMedia()
    skill = make_skill(media)
    with pytest.raises(PermissionError):
        skill.run(VideoCaptureInput(url=TIKTOK_URL, timecodes=[1.0]), make_ctx(email=None))
    assert media.acquire_calls == []


# ---------------------------------------------------------------------------
# URL 経路
# ---------------------------------------------------------------------------
def test_youtube_is_rejected_before_spending_a_job() -> None:
    media = FakeMedia()
    out = make_skill(media).run(VideoCaptureInput(url=YOUTUBE_URL, timecodes=["0:05"]), make_ctx())
    assert out.error == "youtube_blocked"
    assert "YouTube" in out.message and "添付" in out.message
    assert media.acquire_calls == []  # 90秒待たせない


def test_youtube_can_be_reopened_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEO_CAPTURE_ALLOW_YOUTUBE", "1")
    media = FakeMedia()
    out = make_skill(media).run(VideoCaptureInput(url=YOUTUBE_URL, timecodes=["0:05"]), make_ctx())
    # env を開けると実際に取りに行き、本番同様 bot 判定で落ちる。
    assert media.acquire_calls
    assert out.error == "MEDIA_ACQUIRE_FAILED"


# ---------------------------------------------------------------------------
# ツール説明: YouTube の制限は切り出しだけ（2026-09-24 本番）
# ---------------------------------------------------------------------------
# 本番で「この動画を分析して <YouTube URL>」に Aico がツールを一度も呼ばず
# 「YouTube は取得元にブロックされるため分析できません」と断った。ここの説明文と SOUL の
# 「YouTube はブロック」を動画分析全般に広げて読んだため（SOUL 側は PR #441 で限定済み）。
# video_analysis は Gemini に file_uri で URL を渡す経路（DL 不要）で YouTube を分析できる。

# 「YouTube … ブロック／未対応」から、読点・句点・閉じ括弧までを 1 節として拾う。
_YOUTUBE_BLOCK_CLAUSE_RE = re.compile(r"YouTube[^。]*?(?:ブロック|未対応)[^。、）]*")


def _unscoped_youtube_block_clauses(text: str) -> list[str]:
    """YouTube のブロック言及のうち、切り出し限定か video_analysis の但し書きを欠くものを返す。"""
    clauses = [m.group(0) for m in _YOUTUBE_BLOCK_CLAUSE_RE.finditer(text)]
    has_carve_out = any(
        "YouTube" in s and "分析" in s and "video_analysis" in s for s in text.split("。")
    )
    return [c for c in clauses if "切り出" not in c or not has_carve_out]


def _exposed_texts() -> dict[str, str]:
    """モデルが毎ターン読む文面（ツール説明＋各引数の説明）。"""
    texts = {"description": VideoCaptureSkill.description}
    for name, field in VideoCaptureInput.model_fields.items():
        if field.description:
            texts[f"field:{name}"] = field.description
    return texts


def test_youtube_block_mentions_are_scoped_to_capture() -> None:
    """YouTube のブロック言及は「切り出しだけ」と書き、分析は video_analysis と同居させる。"""
    offenders = {
        where: bad
        for where, text in _exposed_texts().items()
        if (bad := _unscoped_youtube_block_clauses(text))
    }
    assert not offenders, f"YouTube ブロックの言及が切り出し限定になっていない: {offenders}"


def test_youtube_carve_out_names_the_real_analysis_tool() -> None:
    """但し書きの指し先が実在の動画分析ツール名であること（改名で幽霊を指さない）。"""
    from teamagent.skills.video.skill import VideoAnalysisSkill

    assert VideoAnalysisSkill.name == "video_analysis"
    for where in ("description", "field:url"):
        assert "video_analysis" in _exposed_texts()[where], f"{where} に分析側の但し書きが無い"


def test_youtube_capture_is_called_not_refused_by_the_router() -> None:
    """YouTube の切り出しも、ルーターが自分で断らずに呼ぶ（サーバが決定論の案内を返す）。

    ルーティングシミュ（tests/routing/README.md R5）で、この 2 文が無い説明文では
    「YouTube の 0:30 を画像にして」を 4 本中 4 本がツールを呼ばずに断った
    （description だけ足した R5b は 2 本中 1 本、url 側も足した R5c で 2 本中 2 本が呼んだ）。
    """
    assert "断らずにそのまま呼べば" in VideoCaptureSkill.description
    assert "YouTube の URL もそのまま入れて呼べば" in _exposed_texts()["field:url"]


def test_attached_video_reference_is_called_not_asked_back() -> None:
    """「さっき貼った動画」は、添付が見えなくても聞き返さずに slack_file=true で呼ぶ。

    添付の特定はサーバ（スレッド→会話履歴）が行い、無ければ attachment_not_found の
    決定論の案内を返す。R5 のシミュで、この文が無い説明文では 6 本中 2 本が
    「文脈が足りない」と聞き返した（R5d で追記後はルーター・SOUL 込みとも 6 本で 18/18 が呼んだ）。
    """
    assert "添付が見えなくても聞き返さずに呼ぶ" in VideoCaptureSkill.description
    assert "添付が見えなくても true で呼ぶ" in _exposed_texts()["field:slack_file"]


_CARVE_OUT = "YouTube 動画の分析は video_analysis で可能。"


@pytest.mark.parametrize(
    "pre_fix",
    [
        "(2) この会話に添付された動画なら url を空にして slack_file=true にする"
        "（YouTube は取得元にブロックされるため、ファイル添付でお願いする）。",
        "切り出す動画の URL（https のみ・TikTok / Instagram に対応。"
        "YouTube は取得元にブロックされるため未対応）。",
    ],
)
def test_youtube_scope_detector_flags_unscoped_clause_even_with_carve_out(pre_fix: str) -> None:
    """修正前の節（切り出し限定なし）は、但し書きが別にあっても捕まえる。"""
    assert _unscoped_youtube_block_clauses(pre_fix + _CARVE_OUT)


def test_youtube_scope_detector_flags_missing_carve_out() -> None:
    """切り出し限定の節だけで video_analysis の但し書きが無ければ捕まえる。"""
    scoped_only = (
        "YouTube の URL は取得元にブロックされるため切り出せず、ファイル添付でお願いする。"
    )
    assert _unscoped_youtube_block_clauses(scoped_only)
    assert not _unscoped_youtube_block_clauses(scoped_only + _CARVE_OUT)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.tiktok.com/@a/video/1",
        "https://vimeo.com/12345",
        "https://user:pw@www.tiktok.com/@a/video/1",
        "https://www.tiktok.com/@a/video/1#frag",
        "https://evil-tiktok.com/@a/video/1",
    ],
)
def test_non_allowlisted_urls_are_rejected(url: str) -> None:
    media = FakeMedia()
    out = make_skill(media).run(VideoCaptureInput(url=url, timecodes=[1.0]), make_ctx())
    assert out.error == "unsupported_url"
    assert media.acquire_calls == []


def test_tiktok_url_delivers_frames_to_thread() -> None:
    media = FakeMedia(duration_s=10.0)
    slack = FakeSlack()
    out = make_skill(media, slack).run(
        VideoCaptureInput(url=TIKTOK_URL, timecodes=["0:01", "0:03", "0:05"]),
        make_ctx(),
    )
    assert out.error == ""
    assert out.source_kind == "url"
    assert out.delivered_count == 3
    assert out.delivered_to == "thread"
    assert [frame.label for frame in out.frames] == ["0:01", "0:03", "0:05"]
    assert "0:01 / 0:03 / 0:05" in out.message
    assert all(upload["channel"] == "C1" for upload in slack.uploads)
    assert all(upload["thread_ts"] == "1700.1" for upload in slack.uploads)
    # exam fix: acquire は 80MB / width は明示 480 / タイムアウト配分は 240+180。
    # 合計 420s = OpenClaw の全ツール共通天井 600s の 70%（実測 1ジョブ 69〜93s に基づく）。
    assert media.acquire_calls[0]["max_bytes"] == 80 * 1024 * 1024
    assert media.acquire_calls[0]["timeout_s"] == 240
    assert media.frame_calls[0]["width"] == 480
    assert media.frame_calls[0]["timeout_s"] == 180
    assert media.acquire_calls[0]["timeout_s"] + media.frame_calls[0]["timeout_s"] <= 420
    assert media.acquire_calls[0]["fingerprint"] != media.frame_calls[0]["fingerprint"]


def test_instagram_url_is_supported() -> None:
    media = FakeMedia()
    out = make_skill(media).run(
        VideoCaptureInput(url=INSTAGRAM_URL, timecodes=["0:02"]), make_ctx()
    )
    assert out.error == ""
    assert out.delivered_count == 1


def test_falls_back_to_dm_when_no_channel() -> None:
    slack = FakeSlack()
    out = make_skill(FakeMedia(), slack).run(
        VideoCaptureInput(url=TIKTOK_URL, timecodes=["0:02"]),
        make_ctx(channel=None, thread=None),
    )
    assert out.delivered_to == "dm"
    assert slack.uploads[0]["channel"] == "D999"


def test_falls_back_to_dm_when_thread_upload_fails() -> None:
    slack = FakeSlack(thread_upload_ok=False)
    out = make_skill(FakeMedia(), slack).run(
        VideoCaptureInput(url=TIKTOK_URL, timecodes=["0:02"]), make_ctx()
    )
    assert out.delivered_to == "dm"
    assert [upload["channel"] for upload in slack.uploads] == ["C1", "D999"]


def test_delivery_failure_is_not_reported_as_success() -> None:
    slack = FakeSlack(upload_ok=False)
    slack.user_id = None
    out = make_skill(FakeMedia(), slack).run(
        VideoCaptureInput(url=TIKTOK_URL, timecodes=["0:02"]), make_ctx()
    )
    assert out.error == "delivery_failed"
    assert out.delivered_count == 0
    assert "失敗" in out.message


# ---------------------------------------------------------------------------
# ★ 範囲外 timecode: 1 点で全滅する事実を握りつぶさない
# ---------------------------------------------------------------------------
def test_single_out_of_range_timecode_fails_the_whole_job() -> None:
    media = FakeMedia(duration_s=8.0)
    slack = FakeSlack()
    out = make_skill(media, slack).run(
        # 3 点は動画（8秒）の内側、最後の 1 点だけが動画長を超える。
        VideoCaptureInput(url=TIKTOK_URL, timecodes=[1.0, 2.0, 3.0, "0:59"]),
        make_ctx(),
    )
    assert out.error == "MEDIA_PROCESS_FAILED"
    assert "指定時刻が動画の長さを超えています" in out.message
    assert out.delivered_count == 0
    assert out.frames == []
    # 「3枚は出せました」に化けていない（1枚もアップロードしていない）。
    assert slack.uploads == []


def test_frame_empty_code_maps_to_the_same_explanation() -> None:
    out = make_skill(FakeMedia(frames_error="MEDIA_FRAME_EMPTY")).run(
        VideoCaptureInput(url=TIKTOK_URL, timecodes=[1.0]), make_ctx()
    )
    assert out.error == "MEDIA_FRAME_EMPTY"
    assert "指定時刻が動画の長さを超えています" in out.message


def test_partial_frame_result_is_treated_as_failure() -> None:
    """worker が要求より少ない枚数を返しても『切り出せました』にしない。"""

    media = FakeMedia(duration_s=100.0, drop_frames=1)
    slack = FakeSlack()
    out = make_skill(media, slack).run(
        VideoCaptureInput(url=TIKTOK_URL, timecodes=[1.0, 2.0, 3.0]), make_ctx()
    )
    assert out.error == "MEDIA_FRAME_EMPTY"
    assert out.delivered_count == 0
    assert slack.uploads == []


@pytest.mark.parametrize(
    ("code", "needle"),
    [
        ("MEDIA_ACQUIRE_SIZE_EXCEEDED", "動画が大きすぎます（目安: 360pで約15分まで）"),
        ("MEDIA_ACQUIRE_FAILED", "取得できませんでした"),
        ("MEDIA_JOB_TIMEOUT", "時間内に終わりませんでした"),
    ],
)
def test_media_error_codes_map_to_japanese(code: str, needle: str) -> None:
    out = make_skill(FakeMedia(acquire_error=code)).run(
        VideoCaptureInput(url=TIKTOK_URL, timecodes=[1.0]), make_ctx()
    )
    assert out.error == code
    assert needle in out.message


def test_unmapped_media_code_still_reports_failure() -> None:
    """写像に無いコードでも『0枚でした』に化けさせない。"""

    out = make_skill(FakeMedia(acquire_error="MEDIA_SOMETHING_NEW")).run(
        VideoCaptureInput(url=TIKTOK_URL, timecodes=[1.0]), make_ctx()
    )
    assert out.error == "MEDIA_SOMETHING_NEW"
    assert out.message and out.delivered_count == 0
    assert "失敗" in out.message


def test_error_map_covers_the_four_required_codes() -> None:
    assert {
        "MEDIA_FRAME_EMPTY",
        "MEDIA_ACQUIRE_SIZE_EXCEEDED",
        "MEDIA_ACQUIRE_FAILED",
        "MEDIA_JOB_TIMEOUT",
        # 実測で範囲外 timecode が返す実コード（設計の想定と違った）。
        "MEDIA_PROCESS_FAILED",
    } <= set(MEDIA_ERROR_MESSAGES)


# ---------------------------------------------------------------------------
# Slack 添付経路
# ---------------------------------------------------------------------------
def test_slack_attachment_is_captured_from_thread() -> None:
    media = FakeMedia()
    slack = FakeSlack()
    reader = FakeReader(
        thread=[SlackMessage(ts="1700.2", user="U1", text="これ", files=(video_file(),))]
    )
    out = make_skill(media, slack, reader).run(
        VideoCaptureInput(slack_file=True, timecodes=["0:02"]), make_ctx()
    )
    assert out.error == ""
    assert out.source_kind == "slack_file"
    assert out.delivered_count == 1
    assert media.acquire_calls == []  # 添付経路では取得元に一切触らない
    assert media.frame_calls[0]["data"] == slack.download_bytes
    assert slack.downloads[0]["max_bytes"] == 100 * 1024 * 1024


def test_slack_attachment_falls_back_to_channel_history() -> None:
    reader = FakeReader(
        thread=[SlackMessage(ts="1700.2", user="U1", text="話だけ")],
        history=[SlackMessage(ts="1699.0", user="U1", text="動画", files=(video_file(),))],
    )
    out = make_skill(FakeMedia(), FakeSlack(), reader).run(
        VideoCaptureInput(slack_file=True, timecodes=["0:02"]), make_ctx()
    )
    assert out.error == ""
    assert reader.calls == ["thread", "history"]


def test_slack_attachment_prefers_the_newest_video() -> None:
    slack = FakeSlack()
    reader = FakeReader(
        thread=[
            SlackMessage(ts="1700.1", user="U1", text="古い", files=(video_file(file_id="F_OLD"),)),
            SlackMessage(
                ts="1700.9",
                user="U1",
                text="新しい",
                files=(video_file(file_id="F_NEW", url="https://files.slack.com/x/new.mp4"),),
            ),
        ]
    )
    out = make_skill(FakeMedia(), slack, reader).run(
        VideoCaptureInput(slack_file=True, timecodes=["0:02"]), make_ctx()
    )
    assert out.error == ""
    assert slack.downloads[0]["url"].endswith("new.mp4")


def test_slack_file_id_only_matches_files_present_in_the_conversation() -> None:
    slack = FakeSlack()
    reader = FakeReader(
        thread=[SlackMessage(ts="1700.2", user="U1", text="x", files=(video_file(file_id="F111"),))]
    )
    out = make_skill(FakeMedia(), slack, reader).run(
        VideoCaptureInput(slack_file=True, slack_file_id="F999", timecodes=["0:02"]),
        make_ctx(),
    )
    assert out.error == "attachment_not_found"
    assert slack.downloads == []  # 申告 ID で任意ファイルを取りに行かせない


def test_external_and_non_video_attachments_are_ignored() -> None:
    reader = FakeReader(
        thread=[
            SlackMessage(
                ts="1700.2",
                user="U1",
                text="x",
                files=(
                    video_file(file_id="F_IMG", mimetype="image/png"),
                    video_file(file_id="F_EXT", external=True),
                ),
            )
        ]
    )
    out = make_skill(FakeMedia(), FakeSlack(), reader).run(
        VideoCaptureInput(slack_file=True, timecodes=["0:02"]), make_ctx()
    )
    assert out.error == "attachment_not_found"


def test_oversized_attachment_is_rejected_before_download() -> None:
    slack = FakeSlack()
    reader = FakeReader(
        thread=[
            SlackMessage(
                ts="1700.2",
                user="U1",
                text="x",
                files=(video_file(size=200 * 1024 * 1024),),
            )
        ]
    )
    out = make_skill(FakeMedia(), slack, reader).run(
        VideoCaptureInput(slack_file=True, timecodes=["0:02"]), make_ctx()
    )
    assert out.error == "attachment_too_large"
    assert "100MB" in out.message
    assert slack.downloads == []


def test_streaming_size_guard_is_surfaced_as_too_large() -> None:
    slack = FakeSlack()
    slack.download_error = RuntimeError("SLACK_FILE_TOO_LARGE: >104857600")
    reader = FakeReader(
        thread=[SlackMessage(ts="1700.2", user="U1", text="x", files=(video_file(size=None),))]
    )
    out = make_skill(FakeMedia(), slack, reader).run(
        VideoCaptureInput(slack_file=True, timecodes=["0:02"]), make_ctx()
    )
    assert out.error == "attachment_too_large"


def test_missing_attachment_gives_actionable_message() -> None:
    out = make_skill(FakeMedia(), FakeSlack(), FakeReader()).run(
        VideoCaptureInput(slack_file=True, timecodes=["0:02"]), make_ctx()
    )
    assert out.error == "attachment_not_found"
    assert "添付" in out.message


def test_unreadable_conversation_is_not_reported_as_no_video() -> None:
    """history scope 不足を「動画が見つかりません」に化けさせない。

    化けさせると、bot の scope 設定ミスが恒久的に『動画0件』として隠れ、
    誰も原因に辿り着けなくなる（実測の注記を事実として報告するな、と同じ罠）。
    """

    reader = FakeReader(read_error=RuntimeError("missing_scope"))
    out = make_skill(FakeMedia(), FakeSlack(), reader).run(
        VideoCaptureInput(slack_file=True, timecodes=["0:02"]), make_ctx()
    )
    assert out.error == "conversation_read_failed"
    assert "履歴を読めませんでした" in out.message
    assert "確認できていません" in out.message
    assert reader.calls == ["thread", "history"]  # 両方試したうえでの判定


def test_readable_but_empty_conversation_still_says_not_found() -> None:
    """読めた場合はこれまでどおり『添付してください』の案内に落とす。"""

    reader = FakeReader(thread=[SlackMessage(ts="1700.2", user="U1", text="雑談だけ")])
    out = make_skill(FakeMedia(), FakeSlack(), reader).run(
        VideoCaptureInput(slack_file=True, timecodes=["0:02"]), make_ctx()
    )
    assert out.error == "attachment_not_found"


# ---------------------------------------------------------------------------
# 総量規制・基盤未設定
# ---------------------------------------------------------------------------
def test_media_not_configured_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MEDIA_TASK_QUEUE", "MEDIA_JOBS_TABLE", "MEDIA_JOB_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    out = VideoCaptureSkill(slack=FakeSlack(), slack_reader=FakeReader()).run(
        VideoCaptureInput(url=TIKTOK_URL, timecodes=[1.0]), make_ctx()
    )
    assert out.error == "media_unavailable"


def test_concurrency_cap_returns_busy_instead_of_ooming() -> None:
    skill_module._SEMAPHORE = threading.BoundedSemaphore(1)
    assert skill_module._SEMAPHORE.acquire(blocking=False)
    try:
        media = FakeMedia()
        out = make_skill(media).run(VideoCaptureInput(url=TIKTOK_URL, timecodes=[1.0]), make_ctx())
        assert out.error == "busy"
        assert media.acquire_calls == []
    finally:
        skill_module._SEMAPHORE.release()


def test_semaphore_is_released_after_failure() -> None:
    skill = make_skill(FakeMedia(acquire_error="MEDIA_JOB_TIMEOUT"))
    for _ in range(3):
        out = skill.run(VideoCaptureInput(url=TIKTOK_URL, timecodes=[1.0]), make_ctx())
        assert out.error == "MEDIA_JOB_TIMEOUT"  # busy に化けない＝解放されている
