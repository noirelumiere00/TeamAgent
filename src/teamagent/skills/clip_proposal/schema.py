"""clip_proposal（2秒で切り抜くん）の入出力 Pydantic スキーマ。

設計方針（計画 §2-2「利用者から見た挙動」）:
- submit の入力は **全て任意**。必須入力は「誰向けか（クライアント名）」だけだが、
  ファイル名・スレッド文脈から推定できるため required にしない。required な自由文字列は
  外側ルーターの値捏造ハザードを生む（omiyage_report と同じ方針）。
- status の job_id は ``^clp_[0-9a-f]{32}$`` の pattern で束縛し、omiyage（``omy_``）/
  proposal_builder（``pb_``）の job と **スキーマ境界で** 分離する。
- 出力の ``status`` に ``failed`` 以外の「断らない」状態を持つ:
  ``busy``（順番待ち・自動着手）と ``deferred``（日次上限・明朝の枠を案内）。
  どちらもジョブを作らず、利用者に手作業を突き返さない文言を ``message`` に載せる。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CLIP_JOB_ID_PATTERN = r"^clp_[0-9a-f]{32}$"
CLIP_JOB_ID_PREFIX = "clp_"

# XML 1.0 で許されない制御文字（NUL 等）。PPTX の slide XML を壊すため落とす。
_XML_ILLEGAL_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

#: submit 受付状態。``needs_input`` 以外は利用者に再作業を求めない。
SubmitStatus = Literal["queued", "needs_input", "busy", "deferred", "failed"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _squash(value: str) -> str:
    return " ".join(_XML_ILLEGAL_CONTROL.sub("", str(value)).split())


class ClipProposalSubmitInput(_StrictModel):
    """切り抜き提案ジョブの投入入力。

    ``file_id`` は「依頼スレッド内に実在し、かつ依頼者本人がアップロードした動画」に
    限って採用される（skill 側の門番）。エージェントが申告した ID で任意ファイルを
    取りに行かせないため、ここでは形式だけを縛る。
    """

    client_name: str = Field(
        default="",
        max_length=120,
        description=(
            "誰向けの提案か（クライアント名・例: 初田製作所）。"
            "不明なら空のまま呼ぶ。ファイル名やスレッド文脈から推定し受付文でエコーバックする"
        ),
    )
    file_id: str = Field(
        default="",
        max_length=64,
        pattern=r"^[A-Za-z0-9]*$",
        description=(
            "依頼スレッドに添付された動画の Slack file id（任意）。"
            "省略時はスレッド内の最新の本人アップロード動画を使う"
        ),
    )
    video_url: str = Field(
        default="",
        max_length=2000,
        description=(
            "動画URL（任意）。YouTube 直接取得は本番実測で bot 判定に阻まれるため既定 OFF。"
            "URL 経路が無効なときは案内文を返し、同じスレッドへの添付で継続できる"
        ),
    )

    @field_validator("client_name", "file_id", "video_url")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return _squash(value)


class ClipProposalSubmitOutput(_StrictModel):
    """受付結果。``queued`` 以外ではジョブを作らない。

    - ``busy``: 同時実行の上限。**再依頼を求めない**（順番待ちに入り自動着手する）。
    - ``deferred``: 日次上限。明朝の枠と急ぎの連絡先を案内する。
    - ``needs_input``: 本人アップロードの動画が見つからない等、素材が確定できない。
    """

    status: SubmitStatus
    job_id: str = ""
    retry_after_seconds: int = Field(default=0, ge=0)
    client_name: str = ""
    message: str


class ClipProposalStatusInput(_StrictModel):
    """進行確認。``job_id`` 省略時は本人の直近 1 件を見る（覚えていなくても答える）。"""

    job_id: str = Field(default="", max_length=64)

    @field_validator("job_id")
    @classmethod
    def _validate_job_id(cls, value: str) -> str:
        job_id = _squash(value)
        if job_id and not re.fullmatch(CLIP_JOB_ID_PATTERN, job_id):
            raise ValueError("job_id must match ^clp_[0-9a-f]{32}$")
        return job_id


class ClipProposalCostSummary(_StrictModel):
    """課金の実測（リトライ分を含む累計）。ジョブ行に残す監査値。"""

    gemini_calls: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    cost_cap_usd: float = Field(default=0.0, ge=0)
    capped: bool = False


class ClipProposalResult(_StrictModel):
    """job 完了時に store へ保存する結果。

    **transcript は保存しない**（採用後の秒区間・コピー・界隈名のみ）。
    """

    status: Literal["ready", "partial"]
    message: str
    notices: list[str] = Field(min_length=1, max_length=8)
    client_name: str = ""
    clip_count: int = Field(default=0, ge=0, le=10)
    dropped_clip_count: int = Field(default=0, ge=0, le=10)
    quality_note: str = ""
    pptx_filename: str = ""
    slack_delivered: bool = False
    delivery_target: Literal["thread", "dm", "none"] = "none"
    cost: ClipProposalCostSummary = Field(default_factory=ClipProposalCostSummary)


class ClipProposalStatusOutput(_StrictModel):
    job_id: str = ""
    status: Literal["queued", "running", "done", "failed", "not_found"]
    retry_after_seconds: int = Field(default=0, ge=0)
    result_status: Literal["ready", "partial"] | None = None
    result_message: str = ""
    notices: list[str] = Field(default_factory=list)
    clip_count: int = Field(default=0, ge=0, le=10)
    slack_delivered: bool = False
    delivery_target: Literal["thread", "dm", "none"] = "none"
    cost: ClipProposalCostSummary = Field(default_factory=ClipProposalCostSummary)
    error_code: str | None = None
    message: str = ""


__all__ = [
    "CLIP_JOB_ID_PATTERN",
    "CLIP_JOB_ID_PREFIX",
    "ClipProposalCostSummary",
    "ClipProposalResult",
    "ClipProposalStatusInput",
    "ClipProposalStatusOutput",
    "ClipProposalSubmitInput",
    "ClipProposalSubmitOutput",
    "SubmitStatus",
]
