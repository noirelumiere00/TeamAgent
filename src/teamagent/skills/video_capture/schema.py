"""video_capture Skill の I/O スキーマ（Pydantic v2）と決定的エラー文の写像表。

死守ライン:
- ``message`` は LLM がそのまま返す決定的日本語文。言い換え・再計算をさせない。
- timecodes の 「0:05」「1:02:03」→ 秒 の変換は **サーバ側で決定的に行う**
  （mm:ss の算術を LLM に任せない＝誤ったフレームを切らない）。
- media worker のエラーコード → 依頼者向け日本語文は本モジュールの
  ``MEDIA_ERROR_MESSAGES`` / ``SKILL_ERROR_MESSAGES`` が唯一の真実源。
  写像に無いコードを握りつぶして「0枚でした」と言わない（必ず失敗として返す）。
"""

from __future__ import annotations

import math
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_TIMECODES = 12
MAX_TIMECODE_SECONDS = 6 * 60 * 60
DEFAULT_WIDTH = 480

# 「0:05」「1:02:03」形式（時は省略可・秒は小数3桁まで）。
_CLOCK_TIMECODE_RE = re.compile(r"^(?:(\d{1,2}):)?(\d{1,2}):(\d{1,2}(?:\.\d{1,3})?)$")

# ---------------------------------------------------------------------------
# media worker のエラーコード → 依頼者に返す決定的日本語文（**仕様**）
# ---------------------------------------------------------------------------
# ⚠️ MEDIA_PROCESS_FAILED は「範囲外 timecode」の**実測コード**（2026-08-17 本番スパイク）。
#    設計時の想定は MEDIA_FRAME_EMPTY だったが、実際の ffmpeg は
#    "Nothing was written into output file" で **非ゼロ終了** するため
#    operations._run が MEDIA_PROCESS_FAILED を先に上げる。
#    MEDIA_FRAME_EMPTY は「ffmpeg が 0 終了して空ファイルを書いた」場合の
#    フォールバック経路として残っており、どちらも同じ原因を指すので同文にする。
#    片方だけ写像すると本番で必ず起きるケースが汎用文に落ちる（＝握りつぶし）。
MEDIA_ERROR_MESSAGES: dict[str, str] = {
    "MEDIA_PROCESS_FAILED": (
        "指定時刻が動画の長さを超えています（動画の長さの内側の時刻を指定してください）。"
    ),
    "MEDIA_FRAME_EMPTY": (
        "指定時刻が動画の長さを超えています（動画の長さの内側の時刻を指定してください）。"
    ),
    "MEDIA_ACQUIRE_SIZE_EXCEEDED": "動画が大きすぎます（目安: 360pで約15分まで）。",
    "MEDIA_ACQUIRE_FAILED": (
        "動画を取得できませんでした（取得元にブロックされた可能性があります）。"
        "動画ファイルをこのスレッドに直接添付していただければ確実に切り出せます。"
    ),
    "MEDIA_JOB_TIMEOUT": (
        "処理が時間内に終わりませんでした（動画が長い可能性があります）。"
        "短い動画で試すか、切り出す時刻の数を減らしてください。"
    ),
}

# skill 層で決まる失敗（media worker に到達する前 / 配信段）の決定的文。
SKILL_ERROR_MESSAGES: dict[str, str] = {
    "unsupported_url": (
        "この URL からは切り出せません（対応: TikTok / Instagram）。"
        "動画ファイルをこのスレッドに直接添付していただければ、どのサイトの動画でも切り出せます。"
    ),
    "youtube_blocked": (
        "YouTube は取得元の bot 判定でブロックされるため、URL からは切り出せません。"
        "動画ファイルをこのスレッドに直接添付してください（それなら確実に切り出せます）。"
    ),
    "attachment_not_found": (
        "この会話に動画ファイルが見つかりませんでした。"
        "切り出したい動画をこのスレッドに添付してから、もう一度お声がけください。"
    ),
    # 「読めなかった」を「動画が無かった」と言わない（bot の history scope 不足を隠さない）。
    "conversation_read_failed": (
        "この会話の履歴を読めませんでした（動画の有無を確認できていません）。"
        "解消しない場合は管理者に連絡してください。"
    ),
    "attachment_too_large": "添付動画が大きすぎます（上限 {limit_mb}MB）。",
    "attachment_failed": "添付動画を取得できませんでした。",
    "media_unavailable": "動画処理の基盤が利用できません（管理者に連絡してください）。",
    "busy": "いま別の動画を処理中です。少し時間をおいてから、もう一度お声がけください。",
    "delivery_failed": (
        "切り出した画像の送信に失敗しました（お届け先が分からなかった可能性があります）。"
    ),
}


def parse_timecode(value: Any) -> float:
    """「0:05」「1:02:03」「5」「5.5」を秒（float）へ決定的に変換する。

    LLM に mm:ss の算術をさせない（誤ったフレームを切る事故の根治）。
    """

    if isinstance(value, bool):
        raise ValueError("timecode must be a number or a m:ss string")
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("timecode must not be empty")
        if ":" in text:
            matched = _CLOCK_TIMECODE_RE.match(text)
            if matched is None:
                raise ValueError(f"timecode format is invalid: {text!r}")
            raw_hours, raw_minutes, raw_seconds = matched.groups()
            hours = int(raw_hours) if raw_hours else 0
            minutes = int(raw_minutes)
            secs = float(raw_seconds)
            if minutes >= 60 or secs >= 60:
                raise ValueError(f"timecode format is invalid: {text!r}")
            seconds = hours * 3600 + minutes * 60 + secs
        else:
            try:
                seconds = float(text)
            except ValueError as exc:
                raise ValueError(f"timecode format is invalid: {text!r}") from exc
    else:
        raise ValueError("timecode must be a number or a m:ss string")
    if not math.isfinite(seconds) or seconds < 0 or seconds > MAX_TIMECODE_SECONDS:
        raise ValueError("timecode is out of range (0 <= t <= 6h)")
    return round(seconds, 3)


def format_timecode(seconds: float) -> str:
    """秒 → 「0:05」「1:02:03」形式のラベル（表示は必ずサーバが作る）。"""

    total = int(seconds)
    fraction = seconds - total
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    label = f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
    if fraction >= 0.001:
        label += f"{fraction:.3f}"[1:].rstrip("0")
    return label


class VideoCaptureInput(BaseModel):
    """動画のシーン切出しの入力（url か slack_file のどちらか一方が必須）。"""

    url: str = Field(
        default="",
        max_length=2048,
        description=(
            "切り出す動画の URL（https のみ・TikTok / Instagram に対応。"
            "YouTube は取得元にブロックされるため未対応）。"
            "会話に添付された動画を使うときは空のままにして slack_file=true にする。"
        ),
    )
    slack_file: bool = Field(
        default=False,
        description=(
            "この会話（スレッド / DM）に添付された動画ファイルを対象にするなら true。"
            "url とは排他（どちらか一方だけ）。添付動画の特定はサーバ側が行うので"
            "ファイル名や ID を推測して書かないこと。"
        ),
    )
    slack_file_id: str = Field(
        default="",
        max_length=32,
        description=(
            "任意。会話に複数の動画がある場合の絞り込み用 Slack file ID（F から始まる）。"
            "会話内に実在する添付だけが対象になる（slack_file=true のときのみ有効）。"
        ),
    )
    timecodes: list[float | str] = Field(
        min_length=1,
        max_length=MAX_TIMECODES,
        description=(
            "切り出したい時刻のリスト（最大12点）。ユーザーが言った表記のまま渡してよい"
            "（「0:05」「1:02:03」「5」「5.5」すべて可）。秒への換算はサーバが行うので"
            "自分で計算しないこと。"
        ),
    )
    width: int = Field(
        default=DEFAULT_WIDTH,
        ge=64,
        le=1920,
        description="切り出す画像の横幅ピクセル（既定480）",
    )

    @field_validator("slack_file_id")
    @classmethod
    def _slack_file_id_shape(cls, value: str) -> str:
        text = value.strip()
        if text and not re.fullmatch(r"F[A-Z0-9]{2,31}", text):
            raise ValueError("slack_file_id must look like a Slack file ID (F...)")
        return text

    @field_validator("timecodes")
    @classmethod
    def _normalize_timecodes(cls, value: list[float | str]) -> list[float | str]:
        parsed = sorted({parse_timecode(item) for item in value})
        if not parsed:
            raise ValueError("timecodes must not be empty")
        if len(parsed) > MAX_TIMECODES:  # pragma: no cover - 重複除去は増えない
            raise ValueError("too many timecodes")
        return list(parsed)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> VideoCaptureInput:
        has_url = bool(self.url.strip())
        if has_url == self.slack_file:
            raise ValueError("url と slack_file はどちらか一方だけを指定してください")
        if self.slack_file_id and not self.slack_file:
            raise ValueError("slack_file_id は slack_file=true のときだけ指定できます")
        return self

    @property
    def seconds(self) -> tuple[float, ...]:
        """正規化済み（昇順・重複なし）の秒列。"""

        return tuple(float(item) for item in self.timecodes)


class CapturedFrame(BaseModel):
    """切り出した 1 枚。"""

    label: str = Field(description="「0:05」形式の表示ラベル（サーバ生成）")
    seconds: float = Field(ge=0, description="切り出した時刻（秒）")
    delivered: bool = Field(default=False, description="Slack への添付に成功したか")


class VideoCaptureOutput(BaseModel):
    """切出し結果（LLM は message をそのまま返す）。"""

    source_kind: str = Field(default="", description="取得元種別 url / slack_file")
    frames: list[CapturedFrame] = Field(default_factory=list, description="切り出した枚のラベル")
    delivered_count: int = Field(default=0, ge=0, description="Slack に添付できた枚数")
    delivered_to: str = Field(default="", description="配信先 thread / dm / 空（失敗）")
    error: str = Field(
        default="",
        description="失敗種別（media コード or skill 種別・成功時は空）",
    )
    message: str = Field(
        default="",
        description="LLM がそのまま返す決定的日本語文（言い換え・要約・再計算をしないこと）",
    )


def media_error_message(code: str) -> str | None:
    """media worker のエラーコードに対応する決定的日本語文（無ければ None）。"""

    return MEDIA_ERROR_MESSAGES.get(code)


__all__ = [
    "DEFAULT_WIDTH",
    "MAX_TIMECODES",
    "MAX_TIMECODE_SECONDS",
    "MEDIA_ERROR_MESSAGES",
    "SKILL_ERROR_MESSAGES",
    "CapturedFrame",
    "VideoCaptureInput",
    "VideoCaptureOutput",
    "format_timecode",
    "media_error_message",
    "parse_timecode",
]
