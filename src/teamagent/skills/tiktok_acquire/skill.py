"""tiktok_acquire / tiktok_acquire_status Skill 本体（Skill層）。

設計: A′トポロジ。tiktok_acquire は SQS へ投函するだけ(即return)、実取得は使い捨て Fargate。
tiktok_acquire_status は DynamoDB を読み、done なら S3 成果物を署名URL化して返す。
AWSアクセスは adapters/tiktok_task_store.py に委譲(3層分離)。RunTask/PassRole は本ツールに無い。
"""

from __future__ import annotations

import hashlib
import json
from typing import ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.tiktok_s3_source import media_audit_principal_hash
from teamagent.adapters.tiktok_task_store import TikTokTaskStore, new_job_id
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.tiktok_acquire.schema import (
    TikTokAcquireInput,
    TikTokAcquireOutput,
    TikTokAcquireStatusInput,
    TikTokAcquireStatusOutput,
)

logger = structlog.get_logger(__name__)


def _audit_principal_hash(ctx: SkillContext) -> str:
    requested_by = ctx.metadata.get("user_email") or ctx.user_id or "unknown"
    return media_audit_principal_hash(requested_by)


def _build_client_config(input: TikTokAcquireInput) -> dict[str, object]:
    """config.json に流すクライアント設定を組む(下流FMT用・任意項目)。"""
    cfg: dict[str, object] = {}
    if input.client_name:
        cfg["client"] = input.client_name
        cfg["client_short"] = input.client_name
    if input.competitors:
        cfg["competitors"] = input.competitors
    if input.industry:
        cfg["industry"] = input.industry
    return cfg


@register
class TikTokAcquireSkill(BaseSkill[TikTokAcquireInput, TikTokAcquireOutput]):
    """期限内に完了できるTikTok取得ジョブを投函する。非同期。"""

    name: ClassVar[str] = "tiktok_acquire"
    description: ClassVar[str] = (
        "商材名/KW群のTikTok上位動画を、動画本体(mp4)込みで一括取得し、提案・"
        "マルチモーダル分析の素材としてストックする。安全な実行時間内で指標+サムネを集め、"
        "上位N本(既定2)はmp4本体もS3に保存する。トリガー=「TikTok取得して/動画も保存して/"
        "保存率上位の動画も/素材集めて/ストックして/提案用に集めて」。分析や要約はせず素材を"
        "貯めるだけ（構造分析レポートは video_algorithm、その場で見る軽い検索・横断分析は"
        " tiktok_search）。非同期で即job_idを返し、結果は tiktok_acquire_status で受け取る。"
    )
    input_schema: ClassVar[type[BaseModel]] = TikTokAcquireInput
    output_schema: ClassVar[type[BaseModel]] = TikTokAcquireOutput

    def __init__(self, store: TikTokTaskStore | None = None) -> None:
        self._store = store or TikTokTaskStore()

    def run(self, input: TikTokAcquireInput, ctx: SkillContext) -> TikTokAcquireOutput:
        log = ctx.bind_logger(self.name)
        request_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "request_id": ctx.request_id,
                    "input": input.model_dump(mode="json"),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        job_id = new_job_id(request_fingerprint)
        spec = {
            "job_id": job_id,
            "keywords": input.keywords,
            "search_type": input.search_type,
            "n_per_kw": input.n_per_kw,
            "videos_per_kw": input.videos_per_kw,
            "sort": input.sort,
            "max_video_bytes": 30 * 1024 * 1024,
            "client": _build_client_config(input),
            "audit_principal_hash": _audit_principal_hash(ctx),
            "request_fingerprint": request_fingerprint,
        }
        ok = self._store.submit(spec)
        log.info("tiktok_acquire_submitted", job_id=job_id, kw=len(input.keywords), ok=ok)
        if not ok:
            return TikTokAcquireOutput(
                job_id=job_id,
                status="failed",
                poll_after_s=0,
                message="取得ジョブの投函に失敗しました(設定/権限を確認してください)。",
            )
        return TikTokAcquireOutput(
            job_id=job_id,
            status="queued",
            poll_after_s=75,
            message=f"取得を開始しました(KW{len(input.keywords)}件・数分かかります)。job_id={job_id}",
        )


@register
class TikTokAcquireStatusSkill(BaseSkill[TikTokAcquireStatusInput, TikTokAcquireStatusOutput]):
    """tiktok_acquire のジョブ状態/成果物(署名URL)を取得する。"""

    name: ClassVar[str] = "tiktok_acquire_status"
    description: ClassVar[str] = (
        "tiktok_acquire が返した job_id の取得結果をポーリング照会する（tiktok_acquire専用の"
        "後工程で、単独の入口にはしない）。doneならposts/サムネ/動画(mp4)を署名URLとS3キーで"
        "返す（url=人向け / s3_key=機械処理用）。"
    )
    input_schema: ClassVar[type[BaseModel]] = TikTokAcquireStatusInput
    output_schema: ClassVar[type[BaseModel]] = TikTokAcquireStatusOutput

    def __init__(self, store: TikTokTaskStore | None = None) -> None:
        self._store = store or TikTokTaskStore()

    def run(self, input: TikTokAcquireStatusInput, ctx: SkillContext) -> TikTokAcquireStatusOutput:
        log = ctx.bind_logger(self.name)
        st = self._store.get_status(
            input.job_id,
            audit_principal_hash=_audit_principal_hash(ctx),
        )
        if st is None:
            return TikTokAcquireStatusOutput(
                job_id=input.job_id, status="unknown", message="そのjob_idは見つかりません。"
            )
        status = st.get("status", "unknown")
        log.info("tiktok_acquire_status", job_id=input.job_id, status=status)
        fail_msg = (
            f"失敗しました: {st.get('error_code') or ''} {st.get('stop_reason') or ''}".strip()
        )
        msg = {
            "queued": "順番待ちです。少し待って再度照会してください。",
            "running": "取得中です(数分)。",
            "done": "完了しました。posts/サムネ/動画の署名URLを返します。",
            "failed": fail_msg,
        }.get(status, "状態不明です。")
        return TikTokAcquireStatusOutput(
            job_id=input.job_id,
            status=status,
            progress=st.get("progress"),
            counts=st.get("counts"),
            s3_prefix=st.get("s3_prefix"),
            posts_json_url=st.get("posts_json_url"),
            config_json_url=st.get("config_json_url"),
            manifest_url=st.get("manifest_url"),
            videos=st.get("videos", []),
            error_code=st.get("error_code"),
            warnings=st.get("warnings", []),
            shortfalls=st.get("shortfalls", []),
            message=msg,
        )
