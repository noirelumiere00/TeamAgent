"""tiktok_acquire / tiktok_acquire_status Skill 本体（Skill層）。

設計: A′トポロジ。tiktok_acquire は SQS へ投函するだけ(即return)、実取得は使い捨て Fargate。
tiktok_acquire_status は DynamoDB を読み、done なら S3 成果物を署名URL化して返す。
AWSアクセスは adapters/tiktok_task_store.py に委譲(3層分離)。RunTask/PassRole は本ツールに無い。
1ジョブの実行上限に収まらない要求は断らず、plan.py で指標だけの取得・KW分割へ組み直す。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.tiktok_s3_source import media_audit_principal_hash
from teamagent.adapters.tiktok_task_store import TikTokTaskStore, new_job_id
from teamagent.skills.base import (
    ASYNC_JOB_POLL_METADATA_KEY,
    BaseSkill,
    SkillContext,
    register,
)
from teamagent.skills.tiktok_acquire.apify_fallback import (
    ApifyVideoFallback,
    apify_fallback_enabled,
)
from teamagent.skills.tiktok_acquire.plan import (
    AcquireJobPlan,
    AcquirePlan,
    plan_acquire_jobs,
)
from teamagent.skills.tiktok_acquire.schema import (
    TikTokAcquireInput,
    TikTokAcquireJob,
    TikTokAcquireOutput,
    TikTokAcquireStatusInput,
    TikTokAcquireStatusOutput,
)

logger = structlog.get_logger(__name__)


def _audit_principal_hash(ctx: SkillContext) -> str:
    requested_by = ctx.metadata.get("user_email") or ctx.user_id or "unknown"
    return media_audit_principal_hash(requested_by)


def _is_async_poll(ctx: SkillContext) -> bool:
    """完了見張り（30秒ポーリング）からの照会か。見張り経路では Apify 補完を発火させない。"""

    return bool(ctx.metadata.get(ASYNC_JOB_POLL_METADATA_KEY))


def _literal_job(input: TikTokAcquireInput) -> AcquireJobPlan:
    return AcquireJobPlan(tuple(input.keywords), input.n_per_kw, input.videos_per_kw, "full")


def _request_fingerprint(
    input: TikTokAcquireInput,
    ctx: SkillContext,
    plan: AcquirePlan,
    index: int,
) -> str:
    """ジョブの冪等キー。要求どおり1ジョブなら従来と同じ値（同じ job_id）を保つ。"""

    payload: dict[str, Any] = {
        "request_id": ctx.request_id,
        "input": input.model_dump(mode="json"),
    }
    if plan.jobs != (_literal_job(input),):
        job = plan.jobs[index]
        payload["plan_job"] = {
            "index": index,
            "keywords": list(job.keywords),
            "n_per_kw": job.n_per_kw,
            "videos_per_kw": job.videos_per_kw,
            "artifact_mode": job.artifact_mode,
        }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


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
        "マルチモーダル分析の素材としてストックする。指標+サムネを集め、上位N本(既定2)は"
        "mp4本体もS3に保存する。videos_per_kw=0 なら表示順と指標だけを取る（検索面チェック用）。"
        "1回の実行時間に収まらない量は断らずに組み直す（動画ありはKWごとのジョブに分けて"
        "job_ids/jobs で返す・変えた点は adjustments）。トリガー=「TikTok取得して/動画も保存して/"
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
        plan = plan_acquire_jobs(
            input.keywords,
            n_per_kw=input.n_per_kw,
            videos_per_kw=input.videos_per_kw,
        )
        if plan.jobs != (_literal_job(input),):
            log.info(
                "tiktok_acquire_replanned",
                kw=len(input.keywords),
                n_per_kw=input.n_per_kw,
                videos_per_kw=input.videos_per_kw,
                jobs=len(plan.jobs),
                planned=[
                    [len(job.keywords), job.n_per_kw, job.videos_per_kw, job.artifact_mode]
                    for job in plan.jobs
                ],
            )
        submitted: list[TikTokAcquireJob] = []
        failed_keywords: list[str] = []
        first_job_id = ""
        for index, job in enumerate(plan.jobs):
            request_fingerprint = _request_fingerprint(input, ctx, plan, index)
            job_id = new_job_id(request_fingerprint)
            first_job_id = first_job_id or job_id
            spec = {
                "job_id": job_id,
                "keywords": list(job.keywords),
                "search_type": input.search_type,
                "n_per_kw": job.n_per_kw,
                "videos_per_kw": job.videos_per_kw,
                "sort": input.sort,
                "artifact_mode": job.artifact_mode,
                "max_video_bytes": 30 * 1024 * 1024,
                "client": _build_client_config(input),
                "audit_principal_hash": _audit_principal_hash(ctx),
                "request_fingerprint": request_fingerprint,
            }
            ok = self._store.submit(spec)
            log.info(
                "tiktok_acquire_submitted",
                job_id=job_id,
                kw=len(job.keywords),
                ok=ok,
                artifact_mode=job.artifact_mode,
                job_index=index,
                jobs=len(plan.jobs),
            )
            if not ok:
                failed_keywords.extend(job.keywords)
                continue
            submitted.append(
                TikTokAcquireJob(
                    job_id=job_id,
                    keywords=list(job.keywords),
                    n_per_kw=job.n_per_kw,
                    videos_per_kw=job.videos_per_kw,
                    artifact_mode=job.artifact_mode,
                )
            )
        adjustments = list(plan.adjustments)
        if not submitted:
            return TikTokAcquireOutput(
                job_id=first_job_id,
                status="failed",
                poll_after_s=0,
                message="取得ジョブの投函に失敗しました(設定/権限を確認してください)。",
                adjustments=adjustments,
            )
        if failed_keywords:
            adjustments.append(
                "KW「" + "」「".join(failed_keywords) + "」の取得は投函に失敗しました"
                "（設定/権限を確認してください）。"
            )
        return TikTokAcquireOutput(
            job_id=submitted[0].job_id,
            status="queued",
            poll_after_s=75,
            message=_queued_message(len(input.keywords), submitted, adjustments),
            job_ids=[job.job_id for job in submitted],
            jobs=submitted,
            adjustments=adjustments,
        )


def _queued_message(
    keyword_count: int,
    submitted: list[TikTokAcquireJob],
    adjustments: list[str],
) -> str:
    if len(submitted) == 1:
        message = (
            f"取得を開始しました(KW{keyword_count}件・数分かかります)。job_id={submitted[0].job_id}"
        )
    else:
        per_job = "、".join(f"{job.job_id}（{'・'.join(job.keywords)}）" for job in submitted)
        message = (
            f"取得を開始しました(KW{keyword_count}件を{len(submitted)}件の取得に分けて並行・"
            f"数分かかります)。job_id={per_job}。結果は job_id ごとに照会してください。"
        )
    if adjustments:
        message += " " + " ".join(adjustments)
    return message


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

    def __init__(
        self,
        store: TikTokTaskStore | None = None,
        apify_fallback: ApifyVideoFallback | None = None,
    ) -> None:
        self._store = store or TikTokTaskStore()
        # 二段構え（設計A）。None なら env opt-in 時に遅延生成する（既定 OFF＝一切触らない）。
        self._apify_fallback = apify_fallback

    def _apply_apify_fallback(
        self,
        st: dict[str, Any],
        job_id: str,
        ctx: SkillContext,
        log: Any,
    ) -> dict[str, Any]:
        """worker が落とせなかった動画を mcp 側 Apify で補完する。失敗しても従来結果を返す。"""

        fallback = self._apify_fallback
        if fallback is None:
            fallback = ApifyVideoFallback(
                media_client_factory=getattr(self._store, "media_client", None),
            )
            self._apify_fallback = fallback
        try:
            return fallback.apply(
                st,
                job_id=job_id,
                audit_principal_hash=_audit_principal_hash(ctx),
                request_id=ctx.request_id,
                user_email=str(ctx.metadata.get("user_email") or ""),
                log=log,
            )
        except Exception as exc:
            logger.warning(
                "tiktok_apify_fallback_unexpected", job_id=job_id, error=type(exc).__name__
            )
            out = dict(st)
            warnings = list(st.get("warnings") or [])
            warnings.append(f"APIFY_FALLBACK_FAILED:{type(exc).__name__}")
            out["warnings"] = warnings
            return out

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
        done_msg = (
            "完了しました。posts/サムネ/動画の署名URLを返します。"
            if st.get("manifest_url")
            # 指標だけの取得（metadata_only）はサムネ・動画を保存しない。
            else "完了しました。posts（表示順と指標）の署名URLを返します。"
        )
        if status == "done" and apify_fallback_enabled() and not _is_async_poll(ctx):
            st = self._apply_apify_fallback(st, input.job_id, ctx, log)
            counts = st.get("counts")
            apify_count = counts.get("videos_apify") if isinstance(counts, dict) else None
            if isinstance(apify_count, int) and apify_count > 0:
                done_msg += f"（worker が落とせなかった {apify_count} 本は Apify で補完）"
        fail_msg = (
            f"失敗しました: {st.get('error_code') or ''} {st.get('stop_reason') or ''}".strip()
        )
        msg = {
            "queued": "順番待ちです。少し待って再度照会してください。",
            "running": "取得中です(数分)。",
            "done": done_msg,
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
