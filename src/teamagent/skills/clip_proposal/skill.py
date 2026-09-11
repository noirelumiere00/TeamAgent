"""clip_proposal（2秒で切り抜くん）の submit / status Skill。

ジョブ機構は omiyage_report / proposal_builder と同じ ``ProposalJobStore`` に相乗りし、
**job_id は ``clp_`` プレフィクス ＋ request_summary の ``kind`` 属性**で分離する。
新しい dispatcher Lambda 改修はしない（migration 窓を要求しない）。

⚠ **本 PR では ``@register`` しない。** SkillRegistry へ載せると
``tests/scripts/test_tool_scope_registry_contract.py`` が scope 台帳 / OC toolFilter との
完全一致を要求する（＝ MCP 露出と OC 再ビルドの 4 点セット）。それは便C の積み荷なので、
ここでは骨格だけを置く。``USE_CLIP_PROPOSAL_TOOLS`` が未設定なら ``enabled()`` が False で、
利用者の画面は一切変わらない。

安全装置（計画 §2-2 の表）:
- 本人限定: ``identity_verified`` と ``verified_slack_user_id`` が無ければ PermissionError。
  ``CLIP_PROPOSAL_USERS`` 未設定・空文字は **全員拒否**。
- 素材の同意: 候補は「依頼スレッド内、かつ ``file.user == verified_slack_user_id``」のみ。
- 配達: verified 由来の ``channel_id`` と ``thread_ts`` が **両方**揃ったスレッドのみ。
  揃わなければ本人 DM 固定へ倒す（チャンネル直投稿はしない）。
- ジョブの覗き見: ジョブ行に ``sha256(user_email + salt)`` を持ち、不一致なら
  ``JOB_NOT_FOUND``（存在も漏らさない）。**transcript はジョブ行に保存しない**。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, cast

from pydantic import BaseModel

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.skills.base import BaseSkill, SkillContext
from teamagent.skills.clip_proposal.analysis import ClipAnalysisError, ClipProposalAnalysis
from teamagent.skills.clip_proposal.limits import (
    DailyQuota,
    build_busy_message,
    build_queued_message,
    shared_quota,
)
from teamagent.skills.clip_proposal.notices import build_delivery_comment, build_notices
from teamagent.skills.clip_proposal.schema import (
    CLIP_JOB_ID_PREFIX,
    ClipProposalCostSummary,
    ClipProposalResult,
    ClipProposalStatusInput,
    ClipProposalStatusOutput,
    ClipProposalSubmitInput,
    ClipProposalSubmitOutput,
)
from teamagent.skills.clip_proposal.template_fill import build_fill_plan

CLIP_JOB_KIND = "clip_proposal"

_JOB_NOT_FOUND = "JOB_NOT_FOUND"
_JOB_START_FAILED = "JOB_START_FAILED"
_CLIP_BUILD_FAILED = "CLIP_BUILD_FAILED"
_TEMPLATE_UNAVAILABLE = "CLIP_TEMPLATE_UNAVAILABLE"

#: 受付文で案内する所要目安（分）。media job 6 本 ＋ Gemini コールの実測見込み。
ETA_MINUTES_DEFAULT = 15
#: status の再照会間隔（完成予定ではない）。
RETRY_AFTER_SECONDS_DEFAULT = 60
#: 添付動画のサイズ上限（バイト）。取りに行く前に弾く。
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024

_SAFE_ERROR_CODE = re.compile(r"\b(?:CLIP|MEDIA|GEMINI)_[A-Z0-9_]{1,56}\b")

DeliveryTarget = Literal["thread", "dm", "none"]


def enabled() -> bool:
    """``USE_CLIP_PROPOSAL_TOOLS``（既定 OFF）。未設定なら誰の画面も変わらない。"""

    return os.environ.get("USE_CLIP_PROPOSAL_TOOLS", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def allowed_users() -> frozenset[str]:
    """``CLIP_PROPOSAL_USERS``（カンマ区切りのメール）。**未設定・空文字は全員拒否**。"""

    raw = os.environ.get("CLIP_PROPOSAL_USERS", "")
    return frozenset(
        part.strip().lower() for part in raw.split(",") if part.strip() and "@" in part
    )


def new_clip_job_id() -> str:
    return f"{CLIP_JOB_ID_PREFIX}{uuid.uuid4().hex}"


def requester_fingerprint(user_email: str) -> str:
    """ジョブ行に置く依頼者の識別子（生メールは置かない）。

    salt は ``CLIP_REQUESTER_SALT``。未設定でもハッシュは成立するが、同じ環境の
    ジョブ行どうしで同一人物が判別できるだけで、メール本体は復元できない。
    """

    salt = os.environ.get("CLIP_REQUESTER_SALT", "clip-proposal-v1")
    return hashlib.sha256(f"{salt}:{user_email.strip().lower()}".encode()).hexdigest()


@dataclass(frozen=True)
class VerifiedRequester:
    """本人限定の門を通った依頼者。ここを通らない経路からは何も実行しない。"""

    slack_user_id: str
    email: str

    @property
    def fingerprint(self) -> str:
        return requester_fingerprint(self.email)


def verify_requester(ctx: SkillContext) -> VerifiedRequester:
    """``identity_verified`` ＋ ``verified_slack_user_id`` ＋ 許可名簿の三重門。"""

    metadata = ctx.metadata or {}
    if not metadata.get("identity_verified"):
        raise PermissionError("clip_proposal requires a verified identity")
    slack_user_id = metadata.get("verified_slack_user_id")
    if not isinstance(slack_user_id, str) or not slack_user_id.strip():
        raise PermissionError("clip_proposal requires verified_slack_user_id")
    email = metadata.get("user_email")
    email = email.strip().lower() if isinstance(email, str) else ""
    roster = allowed_users()
    if not roster or email not in roster:
        raise PermissionError("clip_proposal is not enabled for this user")
    return VerifiedRequester(slack_user_id=slack_user_id.strip(), email=email)


def resolve_delivery_target(metadata: dict[str, Any]) -> tuple[DeliveryTarget, str, str]:
    """配達先を決める純関数。返り値 ``(target, channel_id, thread_ts)``。

    スレッド配達は **``channel_id`` と ``thread_ts`` が両方揃っているときだけ**。
    ``thread_ts`` が無い（＝チャンネル直投稿になる）ときは本人 DM 固定へ倒す。
    """

    channel = metadata.get("channel_id")
    channel = channel.strip() if isinstance(channel, str) else ""
    thread_ts = metadata.get("thread_ts")
    thread_ts = thread_ts.strip() if isinstance(thread_ts, str) else ""
    if channel and thread_ts:
        return "thread", channel, thread_ts
    return "dm", "", ""


def _safe_failure_code(exc: BaseException) -> str:
    if isinstance(exc, ClipAnalysisError):
        return exc.code
    match = _SAFE_ERROR_CODE.search(str(exc))
    return match.group(0) if match else _CLIP_BUILD_FAILED


def _launch_daemon_thread(target: Callable[[], None], name: str) -> None:
    threading.Thread(target=target, name=name, daemon=True).start()


class ActiveJobIndex:
    """走行中ジョブの重複 submit 検出（プロセス内・mcp は desiredCount=1）。

    同じ人が同じ素材で二度 submit したとき、**2 本目のジョブを作らない**。
    作ってしまうと日次枠と推論費用が黙って倍になり、資料も 2 通届く。
    代わりに 1 本目の ``job_id`` をそのまま返し、「もう受け付けています」と答える。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, str] = {}

    @staticmethod
    def key(fingerprint: str, input: ClipProposalSubmitInput) -> str:
        """依頼者 × 素材の識別子。素材が特定できないときはクライアント名で寄せる。"""

        material = input.file_id or input.video_url or input.client_name
        return f"{fingerprint}:{material.strip().lower()}"

    def find(self, key: str) -> str | None:
        with self._lock:
            return self._active.get(key)

    def claim(self, key: str, job_id: str) -> None:
        with self._lock:
            self._active[key] = job_id

    def release(self, key: str) -> None:
        with self._lock:
            self._active.pop(key, None)


_ACTIVE_JOBS = ActiveJobIndex()


def shared_active_jobs() -> ActiveJobIndex:
    return _ACTIVE_JOBS


def reset_active_jobs() -> ActiveJobIndex:
    """共有の走行中インデックスを作り直す（テスト用・本番経路からは呼ばない）。"""

    global _ACTIVE_JOBS
    _ACTIVE_JOBS = ActiveJobIndex()
    return _ACTIVE_JOBS


def build_duplicate_message(job_id: str) -> str:
    """重複 submit への返し。**やり直しをさせない**。"""

    return (
        "同じ素材の切り抜き提案をすでに受け付けています"
        f"（受付番号 {job_id}）。そのまま進めますので、もう一度送っていただく必要はありません。"
        "できたらこのスレッドに資料を添付します。"
    )


#: (analysis, out_dir, request_id) -> pptx path。テンプレ差し替えの実体。
DeckBuilder = Callable[[ClipProposalAnalysis, str, str], str]
#: (path, comment, ctx) -> (delivered, target)
Deliverer = Callable[[str, str, SkillContext], tuple[bool, DeliveryTarget]]
ThreadLauncher = Callable[[Callable[[], None], str], None]


def _default_deck_builder(analysis: ClipProposalAnalysis, out_dir: str, request_id: str) -> str:
    """消毒済みテンプレ（``CLIP_TEMPLATE_PATH``）へ差し替えて PPTX を組む。

    テンプレ資産は本 PR には同梱していない（計画 §4 論点 5・6 の別トラック）。
    未配置なら ``CLIP_TEMPLATE_UNAVAILABLE`` で失敗させ、**空の資料を作らない**。
    """

    from pathlib import Path

    from teamagent.skills.clip_proposal.template_fill import apply_fill_plan

    template_path = os.environ.get("CLIP_TEMPLATE_PATH", "").strip()
    if not template_path or not Path(template_path).is_file():
        raise ClipAnalysisError(_TEMPLATE_UNAVAILABLE)
    plan = build_fill_plan(analysis)
    output = str(Path(out_dir) / f"clip_proposal_{request_id}.pptx")
    return apply_fill_plan(template_path, plan, output)


class ClipProposalSubmitSkill(BaseSkill[ClipProposalSubmitInput, ClipProposalSubmitOutput]):
    """切り抜き提案ジョブを受け付け、mcp 内 daemon thread で生成を継続する。"""

    name: ClassVar[str] = "clip_proposal_submit"
    description: ClassVar[str] = (
        "ビデオリリース本編（MP4）から切り抜き提案の PPTX を作る。"
        "依頼スレッドに本人が添付した動画だけを素材にし、文字起こし→訴求軸5→界隈5/"
        "インサイト5→切り抜き10本まで進めてスレッドへ資料を添付する。"
        "混雑時は status=busy（順番待ち・自動着手／再依頼は不要）、"
        "日次上限に当たったときは status=deferred（枠が空く時刻を案内）を返す。"
        "進行確認は clip_proposal_status。queued/running 中は再submitしない。"
    )
    input_schema: ClassVar[type[BaseModel]] = ClipProposalSubmitInput
    output_schema: ClassVar[type[BaseModel]] = ClipProposalSubmitOutput
    version: ClassVar[str] = "1.0"
    owner: ClassVar[str] = "Aico"
    audit_tag: ClassVar[str] = "clip-proposal-submit"

    def __init__(
        self,
        *,
        store: ProposalJobStore | None = None,
        quota: DailyQuota | None = None,
        analyzer: Any | None = None,
        deck_builder: DeckBuilder = _default_deck_builder,
        deliverer: Deliverer | None = None,
        thread_launcher: ThreadLauncher = _launch_daemon_thread,
        active_jobs: ActiveJobIndex | None = None,
        eta_minutes: int = ETA_MINUTES_DEFAULT,
        retry_after_seconds: int = RETRY_AFTER_SECONDS_DEFAULT,
    ) -> None:
        self._store = store or ProposalJobStore()
        self._quota_override = quota
        self._analyzer = analyzer
        self._deck_builder = deck_builder
        self._deliverer = deliverer
        self._thread_launcher = thread_launcher
        self._active_override = active_jobs
        self._eta_minutes = max(1, eta_minutes)
        self._retry_after_seconds = max(0, retry_after_seconds)

    @property
    def _quota(self) -> DailyQuota:
        return shared_quota() if self._quota_override is None else self._quota_override

    @property
    def _active_jobs(self) -> ActiveJobIndex:
        return shared_active_jobs() if self._active_override is None else self._active_override

    def run(self, input: ClipProposalSubmitInput, ctx: SkillContext) -> ClipProposalSubmitOutput:
        log = ctx.bind_logger(self.name)
        requester = verify_requester(ctx)

        # 重複 submit は **日次枠を消費する前** に弾く（連打で枠と費用が倍にならない）。
        dedupe_key = ActiveJobIndex.key(requester.fingerprint, input)
        running = self._active_jobs.find(dedupe_key)
        if running is not None:
            log.info("clip_proposal_duplicate_submit", job_id=running)
            return ClipProposalSubmitOutput(
                status="queued",
                job_id=running,
                retry_after_seconds=self._retry_after_seconds,
                client_name=input.client_name,
                message=build_duplicate_message(running),
            )

        # 日次上限は **ジョブを作る前**に判定し、受理時に加算する（失敗しても戻さない）。
        decision = self._quota.try_reserve(requester.fingerprint)
        if not decision.accepted:
            log.info(
                "clip_proposal_deferred",
                reason=decision.reason,
                used_by_user=decision.used_by_user,
                used_total=decision.used_total,
            )
            return ClipProposalSubmitOutput(
                status="deferred",
                client_name=input.client_name,
                message=decision.message,
            )

        job_id = new_clip_job_id()
        request_summary = {
            "kind": CLIP_JOB_KIND,
            "request_id": ctx.request_id,
            "requester": requester.fingerprint,
            "client_name": input.client_name,
            "file_id": input.file_id,
            # 本文・transcript・URL は台帳に持たない（小さく保つ・覗き見させない）。
            "has_video_url": bool(input.video_url),
        }
        try:
            self._store.create_job(job_id, request_summary)
        except Exception as exc:
            log.warning("clip_proposal_job_create_failed", error_type=type(exc).__name__)
            return ClipProposalSubmitOutput(
                status="failed",
                client_name=input.client_name,
                message="切り抜き提案jobの受付に失敗しました。",
            )

        job_ctx = SkillContext(
            request_id=ctx.request_id,
            user_id=ctx.user_id,
            metadata=copy.deepcopy(ctx.metadata),
        )
        job_input = input.model_copy(deep=True)
        self._active_jobs.claim(dedupe_key, job_id)
        try:
            self._thread_launcher(
                lambda: self._run_background(
                    job_id, job_input, job_ctx, requester, dedupe_key=dedupe_key
                ),
                f"clip-proposal-{job_id}",
            )
        except Exception as exc:
            self._active_jobs.release(dedupe_key)
            self._store.mark_failed(job_id, _JOB_START_FAILED, expected_statuses=("queued",))
            log.warning("clip_proposal_thread_start_failed", error_type=type(exc).__name__)
            return ClipProposalSubmitOutput(
                status="failed",
                job_id=job_id,
                client_name=input.client_name,
                message="切り抜き提案jobの開始に失敗しました。",
            )

        log.info("clip_proposal_submitted", job_id=job_id)
        return ClipProposalSubmitOutput(
            status="queued",
            job_id=job_id,
            retry_after_seconds=self._retry_after_seconds,
            client_name=input.client_name,
            message=build_queued_message(
                client_name=input.client_name, eta_minutes=self._eta_minutes
            ),
        )

    def busy_output(self, *, position: int, wait_minutes: int) -> ClipProposalSubmitOutput:
        """順番待ちの返り（**再依頼を求めない**）。"""

        return ClipProposalSubmitOutput(
            status="busy",
            retry_after_seconds=max(self._retry_after_seconds, wait_minutes * 60),
            message=build_busy_message(position=position, wait_minutes=wait_minutes),
        )

    # ------------------------------------------------------------------
    # background job
    # ------------------------------------------------------------------

    def _run_background(
        self,
        job_id: str,
        input: ClipProposalSubmitInput,
        ctx: SkillContext,
        requester: VerifiedRequester,
        *,
        dedupe_key: str = "",
    ) -> None:
        import shutil
        import tempfile

        log = ctx.bind_logger(self.name)
        self._store.mark_running(job_id)
        workdir = tempfile.mkdtemp(prefix="clip-proposal-")
        try:
            analysis = self._analyze(input, ctx)
            self._quota.add_cost(analysis.cost_usd)
            path = self._deck_builder(analysis, workdir, ctx.request_id)
            notices = build_notices(quality_note=analysis.quality_note)
            comment = build_delivery_comment(
                client_name=analysis.client_name,
                clip_count=analysis.clip_count,
                notices=notices,
                dropped_clip_count=len(analysis.dropped),
            )
            delivered, target = self._deliver(path, comment, ctx)
            result = ClipProposalResult(
                status="ready" if analysis.clip_count >= 10 else "partial",
                message=comment,
                notices=notices,
                client_name=analysis.client_name,
                clip_count=analysis.clip_count,
                dropped_clip_count=len(analysis.dropped),
                quality_note=analysis.quality_note,
                pptx_filename=os.path.basename(path),
                slack_delivered=delivered,
                delivery_target=target,
                cost=ClipProposalCostSummary(
                    gemini_calls=analysis.gemini_calls,
                    cost_usd=analysis.cost_usd,
                    cost_cap_usd=getattr(self._analyzer, "cost_cap_usd", 0.0) or 0.0,
                ),
            )
            self._store.mark_done(job_id, result.model_dump_json())
            log.info("clip_proposal_done", job_id=job_id, clips=analysis.clip_count)
        except Exception as exc:
            code = _safe_failure_code(exc)
            self._store.mark_failed(job_id, code, expected_statuses=("queued", "running"))
            log.warning("clip_proposal_failed", job_id=job_id, error_code=code)
        finally:
            # workdir（原本動画・抽出フレーム・生成 PPTX）は必ず消す。
            shutil.rmtree(workdir, ignore_errors=True)
            if dedupe_key:
                self._active_jobs.release(dedupe_key)

    def _analyze(self, input: ClipProposalSubmitInput, ctx: SkillContext) -> ClipProposalAnalysis:
        if self._analyzer is None:
            raise ClipAnalysisError("CLIP_ANALYZER_UNAVAILABLE")
        return cast("ClipProposalAnalysis", self._analyzer.run_for_request(input, ctx))

    def _deliver(self, path: str, comment: str, ctx: SkillContext) -> tuple[bool, DeliveryTarget]:
        if self._deliverer is None:
            return False, "none"
        return self._deliverer(path, comment, ctx)


class ClipProposalStatusSkill(BaseSkill[ClipProposalStatusInput, ClipProposalStatusOutput]):
    """切り抜き提案ジョブの進行確認。``job_id`` を覚えていなくても答える。"""

    name: ClassVar[str] = "clip_proposal_status"
    description: ClassVar[str] = (
        "切り抜き提案ジョブの進行を返す。job_id を省略すると本人の直近 1 件を見る。"
        "他人のジョブは存在も返さない。"
    )
    input_schema: ClassVar[type[BaseModel]] = ClipProposalStatusInput
    output_schema: ClassVar[type[BaseModel]] = ClipProposalStatusOutput
    version: ClassVar[str] = "1.0"
    owner: ClassVar[str] = "Aico"
    audit_tag: ClassVar[str] = "clip-proposal-status"

    def __init__(
        self,
        *,
        store: ProposalJobStore | None = None,
        retry_after_seconds: int = RETRY_AFTER_SECONDS_DEFAULT,
        recent_lookup: Callable[[str], str | None] | None = None,
    ) -> None:
        self._store = store or ProposalJobStore()
        self._retry_after_seconds = max(0, retry_after_seconds)
        self._recent_lookup = recent_lookup

    def run(self, input: ClipProposalStatusInput, ctx: SkillContext) -> ClipProposalStatusOutput:
        requester = verify_requester(ctx)
        job_id = input.job_id
        if not job_id and self._recent_lookup is not None:
            job_id = self._recent_lookup(requester.fingerprint) or ""
        if not job_id:
            return ClipProposalStatusOutput(
                status="not_found",
                error_code=_JOB_NOT_FOUND,
                message="進行中の切り抜き提案は見つかりませんでした。",
            )

        row = self._store.get_job(job_id)
        if not self._belongs_to(row, requester):
            # 存在も漏らさない（他人の job_id を総当たりされても差が出ない）。
            return ClipProposalStatusOutput(
                job_id="",
                status="not_found",
                error_code=_JOB_NOT_FOUND,
                message="進行中の切り抜き提案は見つかりませんでした。",
            )
        assert row is not None
        status = str(row.get("status") or "queued")
        if status == "done":
            return self._done_output(job_id, row)
        if status == "failed":
            return ClipProposalStatusOutput(
                job_id=job_id,
                status="failed",
                # ProposalJobStore は失敗コードを ``error_code`` 列に置く
                # （``failure_code`` ではない）。
                error_code=str(row.get("error_code") or _CLIP_BUILD_FAILED),
                message="切り抜き提案の作成に失敗しました。もう一度お申し付けください。",
            )
        return ClipProposalStatusOutput(
            job_id=job_id,
            status="running" if status == "running" else "queued",
            retry_after_seconds=self._retry_after_seconds,
            message="作成中です。できたらこのスレッドに資料を添付します。",
        )

    @staticmethod
    def _belongs_to(row: dict[str, Any] | None, requester: VerifiedRequester) -> bool:
        if not row:
            return False
        summary = row.get("request_summary")
        if isinstance(summary, str):
            try:
                summary = json.loads(summary)
            except ValueError:
                return False
        if not isinstance(summary, dict):
            return False
        if summary.get("kind") != CLIP_JOB_KIND:
            return False
        return summary.get("requester") == requester.fingerprint

    def _done_output(self, job_id: str, row: dict[str, Any]) -> ClipProposalStatusOutput:
        raw = row.get("result_json") or row.get("result") or "{}"
        try:
            result = ClipProposalResult.model_validate_json(
                raw if isinstance(raw, str) else json.dumps(raw)
            )
        except Exception:
            return ClipProposalStatusOutput(
                job_id=job_id,
                status="failed",
                error_code="RESULT_INVALID",
                message="結果の読み出しに失敗しました。",
            )
        return ClipProposalStatusOutput(
            job_id=job_id,
            status="done",
            result_status=result.status,
            result_message=result.message,
            notices=list(result.notices),
            clip_count=result.clip_count,
            slack_delivered=result.slack_delivered,
            delivery_target=result.delivery_target,
            cost=result.cost,
            message=result.message,
        )


__all__ = [
    "CLIP_JOB_KIND",
    "ETA_MINUTES_DEFAULT",
    "MAX_ATTACHMENT_BYTES",
    "ClipProposalStatusSkill",
    "ClipProposalSubmitSkill",
    "DeliveryTarget",
    "VerifiedRequester",
    "allowed_users",
    "enabled",
    "new_clip_job_id",
    "requester_fingerprint",
    "resolve_delivery_target",
    "verify_requester",
]
