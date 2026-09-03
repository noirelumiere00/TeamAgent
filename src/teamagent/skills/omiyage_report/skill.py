"""お土産資料 便1 の submit / status Skill。

ジョブ機構は proposal_builder と同じ ``ProposalJobStore``（DynamoDB / 共有memory）に
相乗りするが、**job_id は ``omy_`` プレフィクス + request_summary の ``kind`` 属性で
分離**する。status はスキーマの pattern（``^omy_``）と kind 照合の二段で、
proposal_builder の job を照会・破壊できない（逆方向は proposal_builder 側の
``pb_`` プレフィクスガードが守る）。

フロー（確定済みカスタマージャーニー + 2026-08-24 FMT化裁定）:
  submit → preflight（不足なら needs_input・ジョブを作らない）
  → アドミッション（同時実行が上限なら busy・順番待ち・ジョブを作らない）→ queued
  → daemon thread: 検索軸ごとに tiktok_search 実測（一般KW / ブランド名 / 競合名）
  → 動画解析（DL→フレーム→視覚AIでクラスタ分類+テロップ読取・並列/コスト上限つき）
  → 決定論集計（metrics）→ 契約準拠の計測JSON（deck_plan: deck_meta + slide_plan）
  → 計測JSON+監査JSONをS3へ保存 → レンダラ（計測JSONだけを入力に描く）で PPTX 化
  → 依頼元スレッドへ添付（失敗時は本人DMへフォールバック）→ mark_done。
  一部の検索・解析が失敗しても部分結果で作成し、資料と結果メッセージで開示する（場面4）。
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import shutil
import tempfile
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.adapters.tiktok_scraper import TikTokScrapeError, TikTokSearchResult
from teamagent.media.contracts import TIKTOK_N_PER_KW_MAX
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.omiyage_report.compose import (
    build_all_failed_message,
    build_analysis_note,
    build_delivery_failed_note,
    build_next_step,
    build_partial_message,
    build_summary_lines,
)
from teamagent.skills.omiyage_report.contract import SPEC_VERSION
from teamagent.skills.omiyage_report.deck_plan import (
    build_audit,
    build_deck_plan,
    top5_candidates,
)
from teamagent.skills.omiyage_report.fmt.build import build_delivery_comment
from teamagent.skills.omiyage_report.fmt.editable import EDIT_MARKER
from teamagent.skills.omiyage_report.metrics import (
    AxisData,
    AxisRole,
    OmiyageMeasurement,
    PostRecord,
    measure,
)
from teamagent.skills.omiyage_report.preflight import (
    CompletionSource,
    build_accepted_message,
    build_busy_message,
    build_needs_input_message,
    run_preflight,
)
from teamagent.skills.omiyage_report.schema import (
    OmiyageAxisSummary,
    OmiyageReportResult,
    OmiyageReportStatusInput,
    OmiyageReportStatusOutput,
    OmiyageReportSubmitInput,
    OmiyageReportSubmitOutput,
    OmiyageVideoAnalysisSummary,
)
from teamagent.skills.omiyage_report.video_analysis import (
    OmiyageVideoAnalyzer,
    VideoAnalysisReport,
)

OMIYAGE_JOB_KIND = "omiyage_report"
_JOB_ID_PREFIX = "omy_"

_OMIYAGE_BUILD_FAILED = "OMIYAGE_BUILD_FAILED"
_OMIYAGE_SEARCH_FAILED = "OMIYAGE_SEARCH_FAILED"
_JOB_START_FAILED = "JOB_START_FAILED"
_JOB_STATE_WRITE_FAILED = "JOB_STATE_WRITE_FAILED"
_RESULT_INVALID = "RESULT_INVALID"

_RETRY_SECONDS_DEFAULT = 60
_HEARTBEAT_SECONDS_DEFAULT = 30
_STALE_SECONDS_DEFAULT = 180

# 同時に走らせてよいお土産資料ジョブの本数。1 ジョブが検索軸ごとの実スクレイプ＋
# 動画DL＋Bedrock 視覚推論＋PPTX レンダを daemon thread で回すため、無制限だと
# mcp タスク（実測 2026-08-27: cpu 1024 / mem 4096・desiredCount=1）を数本で食い潰す。
_MAX_CONCURRENT_JOBS_DEFAULT = 3

# 1軸あたりの取得本数。深掘り実測（2026-08-24・「シャンプー」120本/47s）は可能だが、
# dispatcher Lambda が受理する n_per_kw の上限（TIKTOK_N_PER_KW_MAX）を超える値を
# 送ると全軸 TIKTOK_MEDIA_JOB_FAILED になる（2026-09-02 本番事故: 既定120で全滅）。
# 既定・上限とも契約側の単一情報源に揃え、env や明示指定でも上限を超えさせない。
# 届かなかった軸は取得できた上限で集計し、資料の確認範囲に開示する。
_SEARCH_DEPTH_DEFAULT = TIKTOK_N_PER_KW_MAX
_SEARCH_DEPTH_MIN = 10
_SEARCH_TIMEOUT_SECONDS_DEFAULT = 360

_SAFE_ERROR_CODE = re.compile(r"\b(?:TIKTOK|MEDIA)_[A-Z0-9_]{1,56}\b")

_Searcher = Callable[..., TikTokSearchResult]
# (deck_plan_json, out_dir, request_id) → (画像モードPPTXパス, 編集用PPTXパス)。
# 入力は契約 DeckPlan の JSON だけ（レンダラ無作文原則のエンジン⇔レンダラ境界）。
_DeckBuilder = Callable[[str, str, str], tuple[str, str]]
_ThreadLauncher = Callable[[Callable[[], None], str], None]
# 動画解析器の生成（request_id → analyzer）。media 基盤の無い環境では None を返す。
_AnalyzerFactory = Callable[[str], OmiyageVideoAnalyzer | None]
# 計測JSON/監査JSONの保存（key, body, content_type → 保存先URI。未設定環境は ""）。
_PlanUploader = Callable[[str, bytes, str], str]


def new_omiyage_job_id() -> str:
    return f"{_JOB_ID_PREFIX}{uuid.uuid4().hex}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _launch_daemon_thread(target: Callable[[], None], name: str) -> None:
    threading.Thread(target=target, name=name, daemon=True).start()


def _parse_job_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _envint(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError:
        value = default
    return min(maximum, max(minimum, value))


def _configured_retry_seconds() -> int:
    return _envint(
        "OMIYAGE_JOB_RETRY_AFTER_SECONDS",
        _RETRY_SECONDS_DEFAULT,
        minimum=5,
        maximum=300,
    )


def _configured_heartbeat_seconds() -> int:
    return _envint(
        "OMIYAGE_JOB_HEARTBEAT_SECONDS",
        _HEARTBEAT_SECONDS_DEFAULT,
        minimum=5,
        maximum=300,
    )


def _configured_stale_seconds() -> int:
    configured = _envint(
        "OMIYAGE_JOB_STALE_SECONDS",
        _STALE_SECONDS_DEFAULT,
        minimum=60,
        maximum=86_400,
    )
    return max(configured, _configured_heartbeat_seconds() * 3)


def configured_search_depth() -> int:
    """1軸あたりの取得本数（既定＝dispatcher 上限 TIKTOK_N_PER_KW_MAX・env では下げるだけ）。"""
    return _envint(
        "OMIYAGE_SEARCH_DEPTH",
        _SEARCH_DEPTH_DEFAULT,
        minimum=_SEARCH_DEPTH_MIN,
        maximum=TIKTOK_N_PER_KW_MAX,
    )


def clamp_search_depth(search_depth: int) -> int:
    """明示指定の深度も dispatcher 上限で必ず clamp する（1..TIKTOK_N_PER_KW_MAX）。"""
    return min(TIKTOK_N_PER_KW_MAX, max(1, search_depth))


def _configured_search_timeout_seconds() -> int:
    return _envint(
        "OMIYAGE_SEARCH_TIMEOUT_SECONDS",
        _SEARCH_TIMEOUT_SECONDS_DEFAULT,
        minimum=60,
        maximum=900,
    )


def _configured_max_concurrent_jobs() -> int:
    return _envint(
        "OMIYAGE_MAX_CONCURRENT_JOBS",
        _MAX_CONCURRENT_JOBS_DEFAULT,
        minimum=1,
        maximum=16,
    )


class JobAdmission:
    """走行中ジョブ本数の入口制御（プロセス内カウンタ）。

    mcp は desiredCount=1（実測 2026-08-27）なので、プロセス内の
    ``BoundedSemaphore`` がそのまま系全体の上限になる。DynamoDB の分散カウンタは
    不要で、増やせば「台帳が読めないと受付できない」fail-closed 面が増えるだけ。

    ``BoundedSemaphore`` を選ぶのは、二重 release（＝上限がじわじわ緩む静かな
    バグ）をその場で ValueError として顕在化させるため。
    """

    def __init__(self, limit: int | None = None) -> None:
        self._limit = _configured_max_concurrent_jobs() if limit is None else max(1, limit)
        self._semaphore = threading.BoundedSemaphore(self._limit)

    @property
    def limit(self) -> int:
        return self._limit

    def try_acquire(self) -> bool:
        """空きがあれば 1 枠取る。**待たない**（submit は即答が契約）。"""

        return self._semaphore.acquire(blocking=False)

    def release(self) -> None:
        self._semaphore.release()


# プロセス共有の既定インスタンス。Skill は呼び出しのたびに instantiate されるので、
# インスタンス変数に持たせると上限が効かない（media_job の boto3 キャッシュと同型）。
_ADMISSION = JobAdmission()


def reset_job_admission(limit: int | None = None) -> JobAdmission:
    """共有 admission を作り直す（テスト用・本番経路からは呼ばない）。"""

    global _ADMISSION
    _ADMISSION = JobAdmission(limit)
    return _ADMISSION


def _safe_failure_code(exc: BaseException) -> str:
    match = _SAFE_ERROR_CODE.search(str(exc))
    return match.group(0) if match else "SEARCH_FAILED"


def _default_searcher(*args: Any, **kwargs: Any) -> TikTokSearchResult:
    from teamagent.adapters.tiktok_scraper import search_tiktok

    return search_tiktok(*args, **kwargs)


def _default_deck_builder(deck_plan_json: str, out_dir: str, request_id: str) -> tuple[str, str]:
    """本番: FMT 正レンダラで画像モードPPTX（正）+ 編集用ネイティブPPTX（併走）を組む。

    入力は契約 DeckPlan の JSON だけ（レンダラ無作文原則）。画像モードは
    media worker slides オペ（chromium・1920x1080・scale=1）経由で、mcp イメージに
    python-pptx が無くても成立する。編集用は stdlib OOXML ライタ（fmt.editable）。
    """
    from teamagent.skills.omiyage_report import fmt

    raw = json.loads(deck_plan_json)
    generated_on = (
        str(raw.get("generated_on", "")) if isinstance(raw, dict) else ""
    ) or _utc_now().strftime("%Y-%m-%d")
    artifacts = fmt.render_fmt_deck(raw, generated_on=generated_on)
    image_bytes = fmt.build_image_pptx(
        artifacts.html,
        request_fingerprint=f"{request_id}:omiyage-fmt-pptx",
    )
    image_path = Path(out_dir) / artifacts.image_filename
    image_path.write_bytes(image_bytes)
    editable_path = Path(out_dir) / artifacts.editable_filename
    editable_path.write_bytes(artifacts.editable_pptx)
    return str(image_path), str(editable_path)


def _default_analyzer_factory(request_id: str) -> OmiyageVideoAnalyzer | None:
    """media job 基盤が使える環境でだけ動画解析を実行する（無い環境は未実施を開示）。"""
    from teamagent.adapters.media_job import MediaJobClient

    if not (MediaJobClient.is_configured() or MediaJobClient.local_runtime_enabled()):
        return None
    return OmiyageVideoAnalyzer(request_id=request_id)


def _default_plan_uploader(key: str, body: bytes, content_type: str) -> str:
    """計測JSON・監査JSONの非公開S3保存（OMIYAGE_DECK_PLAN_BUCKET 未設定時は保存なし）。"""
    bucket = os.environ.get("OMIYAGE_DECK_PLAN_BUCKET", "")
    if not bucket:
        return ""
    import boto3

    prefix = os.environ.get("OMIYAGE_DECK_PLAN_PREFIX", "omiyage/deck-plans/")
    full_key = f"{prefix}{key}"
    boto3.client("s3").put_object(
        Bucket=bucket,
        Key=full_key,
        Body=body,
        ContentType=content_type,
    )
    return f"s3://{bucket}/{full_key}"


class _OmiyageAllSearchesFailedError(RuntimeError):
    """全検索軸の取得に失敗（部分結果すら作れない）。"""


@register
class OmiyageReportSubmitSkill(BaseSkill[OmiyageReportSubmitInput, OmiyageReportSubmitOutput]):
    """お土産資料（便1）ジョブを受け付け、MCP 内 daemon thread で生成を継続する。"""

    name: ClassVar[str] = "omiyage_report_submit"
    description: ClassVar[str] = (
        "お土産資料の最終成果物（PPTX）はこのツールでのみ生成する。調査ツール（x_voice_search / "
        "search_surface_check / web_research / tiktok_acquire）で裏取りした材料は research_notes "
        "に添えること（任意・1行1要点+出典URL・「生活者の声」「検索面の勢力図」の章に反映）。"
        "「◯◯のお土産資料つくって。競合は△△」を受け付ける。対象ブランド・競合(1社以上)・"
        "一般検索キーワード(1つ以上)がそろえば job_id を即返し、TikTok検索実測→決定論集計"
        "（露出シェア/キーワード登場率/#PR比較）→PPTX生成→依頼元スレッド添付まで"
        "バックグラウンドで進める。不足時は status=needs_input で不足リストと補完候補・"
        "回答欄を返す（ジョブは作らない）ので、営業の回答で埋めて再submitする。"
        "同時実行の上限に達している時は status=busy（順番待ち・ジョブは作らない）を返すので、"
        "retry_after_seconds を置いてから同じ入力でそのまま再submitする。"
        "進行確認は omiyage_report_status。queued/running中は再submitしない。"
        "所要は目安10〜30分（TikTok取得と動画分析）。queued時の retry_after_seconds は"
        "status再照会の間隔であって完成予定ではないので、営業へは message の所要目安を"
        "そのまま伝え、スレッドで『まだ？』と聞かれたら同じjob_idでstatusを照会する。"
    )
    input_schema: ClassVar[type[BaseModel]] = OmiyageReportSubmitInput
    output_schema: ClassVar[type[BaseModel]] = OmiyageReportSubmitOutput
    version: ClassVar[str] = "1.0"
    owner: ClassVar[str] = "Aico"
    audit_tag: ClassVar[str] = "omiyage-report-submit"

    def __init__(
        self,
        *,
        store: ProposalJobStore | None = None,
        searcher: _Searcher | None = None,
        deck_builder: _DeckBuilder = _default_deck_builder,
        slack: Any | None = None,
        completion_source: CompletionSource | None = None,
        thread_launcher: _ThreadLauncher = _launch_daemon_thread,
        analyzer_factory: _AnalyzerFactory = _default_analyzer_factory,
        plan_uploader: _PlanUploader = _default_plan_uploader,
        heartbeat_seconds: int | None = None,
        retry_after_seconds: int | None = None,
        search_depth: int | None = None,
        search_timeout_seconds: int | None = None,
        analysis_per_axis: int | None = None,
        admission: JobAdmission | None = None,
    ) -> None:
        self._store = store or ProposalJobStore()
        # None のままにして「呼び出し時点の共有インスタンス」を見る（reset_job_admission
        # をテストが後から呼んでも効くようにする）。
        self._admission_override = admission
        self._searcher = searcher or _default_searcher
        self._deck_builder = deck_builder
        self._slack = slack
        self._completion_source = completion_source
        self._thread_launcher = thread_launcher
        self._analyzer_factory = analyzer_factory
        self._plan_uploader = plan_uploader
        self._analysis_per_axis = (
            _envint("OMIYAGE_VA_PER_AXIS", 10, minimum=1, maximum=30)
            if analysis_per_axis is None
            else max(1, analysis_per_axis)
        )
        self._heartbeat_seconds = (
            _configured_heartbeat_seconds()
            if heartbeat_seconds is None
            else max(0, heartbeat_seconds)
        )
        self._retry_after_seconds = (
            _configured_retry_seconds()
            if retry_after_seconds is None
            else max(0, retry_after_seconds)
        )
        self._search_depth = (
            configured_search_depth() if search_depth is None else clamp_search_depth(search_depth)
        )
        self._search_timeout_seconds = (
            _configured_search_timeout_seconds()
            if search_timeout_seconds is None
            else max(1, search_timeout_seconds)
        )

    @property
    def _admission(self) -> JobAdmission:
        return _ADMISSION if self._admission_override is None else self._admission_override

    def run(
        self,
        input: OmiyageReportSubmitInput,
        ctx: SkillContext,
    ) -> OmiyageReportSubmitOutput:
        log = ctx.bind_logger(self.name)
        preflight = run_preflight(input, self._completion_source)
        if not preflight.ready:
            log.info(
                "omiyage_report_needs_input",
                missing=list(preflight.missing),
                suggestion_fields=[s.field for s in preflight.suggestions],
            )
            return OmiyageReportSubmitOutput(
                status="needs_input",
                missing=list(preflight.missing),
                suggestions=list(preflight.suggestions),
                message=build_needs_input_message(input, preflight),
            )

        # 入口で走行本数を絞る。**ジョブを作る前**に判定するので、順番待ちになった
        # 依頼は台帳にも残らない（status 照会の対象が増えない・掃除も要らない）。
        admission = self._admission
        if not admission.try_acquire():
            log.info("omiyage_report_admission_rejected", limit=admission.limit)
            return OmiyageReportSubmitOutput(
                status="busy",
                retry_after_seconds=self._retry_after_seconds,
                message=build_busy_message(
                    limit=admission.limit,
                    retry_after_seconds=self._retry_after_seconds,
                ),
            )

        # ここから先で背景スレッドの起動に失敗（or 台帳書込で例外）したら、取った枠は
        # その場で返す。返し損ねると「実行中 0 本なのに永久に順番待ち」になる。
        handed_off = False
        try:
            job_id = new_omiyage_job_id()
            request_summary = {
                "kind": OMIYAGE_JOB_KIND,
                "request_id": ctx.request_id,
                "brand": input.brand,
                "competitors": list(input.competitors),
                "keywords": list(input.keywords),
                "official_tiktok_account": input.official_tiktok_account,
                "search_depth": self._search_depth,
                # 本文は台帳に持たない（小さく保つ）。添付の有無と長さだけ記録する。
                "research_notes_chars": len(input.research_notes),
            }
            self._store.create_job(job_id, request_summary)

            job_input = input.model_copy(deep=True)
            job_ctx = SkillContext(
                request_id=ctx.request_id,
                user_id=ctx.user_id,
                metadata=copy.deepcopy(ctx.metadata),
            )
            try:
                self._thread_launcher(
                    lambda: self._run_with_admission(admission, job_id, job_input, job_ctx),
                    f"omiyage-report-{job_id}",
                )
            except Exception as exc:
                self._store.mark_failed(job_id, _JOB_START_FAILED, expected_statuses=("queued",))
                log.warning(
                    "omiyage_report_thread_start_failed",
                    job_id=job_id,
                    error_type=type(exc).__name__,
                )
                return OmiyageReportSubmitOutput(
                    status="failed",
                    job_id=job_id,
                    message="お土産資料jobの開始に失敗しました。",
                )
            handed_off = True
        finally:
            if not handed_off:
                admission.release()

        log.info("omiyage_report_submitted", job_id=job_id)
        return OmiyageReportSubmitOutput(
            status="queued",
            job_id=job_id,
            retry_after_seconds=self._retry_after_seconds,
            message=build_accepted_message(input),
        )

    # ------------------------------------------------------------------
    # background job
    # ------------------------------------------------------------------

    def _run_with_admission(
        self,
        admission: JobAdmission,
        job_id: str,
        input: OmiyageReportSubmitInput,
        ctx: SkillContext,
    ) -> None:
        """背景実行の外側で枠を必ず返す（``_run_background`` 本体の責務は変えない）。"""

        try:
            self._run_background(job_id, input, ctx)
        finally:
            admission.release()

    def _run_background(
        self,
        job_id: str,
        input: OmiyageReportSubmitInput,
        ctx: SkillContext,
    ) -> None:
        log = ctx.bind_logger(self.name)
        try:
            claimed = self._store.mark_running(job_id)
        except Exception as exc:
            log.warning(
                "omiyage_report_job_claim_failed",
                job_id=job_id,
                error_type=type(exc).__name__,
            )
            try:
                self._store.mark_failed(job_id, _JOB_STATE_WRITE_FAILED)
            except Exception as write_exc:
                log.warning(
                    "omiyage_report_failure_write_failed",
                    job_id=job_id,
                    error_type=type(write_exc).__name__,
                )
            return
        if not claimed:
            log.warning("omiyage_report_job_claim_rejected", job_id=job_id)
            return

        heartbeat_stop = threading.Event()
        heartbeat_thread: threading.Thread | None = None
        if self._heartbeat_seconds:
            heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                args=(job_id, heartbeat_stop, log),
                name=f"omiyage-heartbeat-{job_id}",
                daemon=True,
            )
            heartbeat_thread.start()

        try:
            result = self._execute(input, ctx)
            result_json = result.model_dump_json()
            try:
                stored = self._store.mark_done(job_id, result_json)
            except Exception as exc:
                log.warning(
                    "omiyage_report_result_write_failed",
                    job_id=job_id,
                    error_type=type(exc).__name__,
                )
                self._store.mark_failed(
                    job_id,
                    _JOB_STATE_WRITE_FAILED,
                    expected_statuses=("running",),
                )
            else:
                if stored:
                    log.info(
                        "omiyage_report_job_done",
                        job_id=job_id,
                        report_status=result.status,
                        slack_delivered=result.slack_delivered,
                    )
                else:
                    log.warning("omiyage_report_terminal_write_rejected", job_id=job_id)
        except Exception as exc:
            error_code = (
                _OMIYAGE_SEARCH_FAILED
                if isinstance(exc, _OmiyageAllSearchesFailedError)
                else _OMIYAGE_BUILD_FAILED
            )
            log.warning(
                "omiyage_report_job_failed",
                job_id=job_id,
                error_code=error_code,
                error_type=type(exc).__name__,
            )
            try:
                self._store.mark_failed(job_id, error_code, expected_statuses=("running",))
            except Exception as write_exc:
                log.warning(
                    "omiyage_report_failure_write_failed",
                    job_id=job_id,
                    error_type=type(write_exc).__name__,
                )
        finally:
            heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=1.0)

    def _heartbeat_loop(self, job_id: str, stop: threading.Event, log: Any) -> None:
        while not stop.wait(self._heartbeat_seconds):
            try:
                if not self._store.heartbeat(job_id):
                    return
            except Exception as exc:
                log.warning(
                    "omiyage_report_heartbeat_failed",
                    job_id=job_id,
                    error_type=type(exc).__name__,
                )

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------

    def _axis_plan(self, input: OmiyageReportSubmitInput) -> list[tuple[AxisRole, str, str]]:
        plan: list[tuple[AxisRole, str, str]] = [
            ("general", f"一般KW「{kw}」検索", kw) for kw in input.keywords
        ]
        plan.append(("brand", f"ブランド名「{input.brand}」検索", input.brand))
        plan.extend(
            ("competitor", f"競合「{competitor}」検索", competitor)
            for competitor in input.competitors
        )
        return plan

    def _search_axes(
        self,
        input: OmiyageReportSubmitInput,
        ctx: SkillContext,
        log: Any,
    ) -> list[AxisData]:
        axes: list[AxisData] = []
        for role, label, query in self._axis_plan(input):
            try:
                result = self._searcher(
                    query,
                    search_type="keyword",
                    max_videos=self._search_depth,
                    request_id=ctx.request_id,
                    timeout_s=self._search_timeout_seconds,
                )
            except TikTokScrapeError as exc:
                code = _safe_failure_code(exc)
                log.warning(
                    "omiyage_report_axis_failed",
                    role=role,
                    failure_code=code,
                )
                axes.append(
                    AxisData(
                        role=role,
                        label=label,
                        query=query,
                        requested=self._search_depth,
                        posts=(),
                        failed=True,
                        failure_code=code,
                    )
                )
                continue
            posts = tuple(
                PostRecord(
                    video_id=video.id,
                    url=video.url,
                    author=video.author.unique_id,
                    caption=video.desc,
                    hashtags=tuple(video.hashtags),
                    rank=index + 1,
                    plays=video.play_count,
                    likes=video.digg_count,
                    comments=video.comment_count,
                    shares=video.share_count,
                    saves=video.collect_count,
                    followers=video.author.follower_count,
                    nickname=video.author.nickname,
                    cover_url=video.cover_url,
                    duration_sec=video.duration,
                )
                for index, video in enumerate(result.videos)
            )
            log.info("omiyage_report_axis_fetched", role=role, fetched=len(posts))
            axes.append(
                AxisData(
                    role=role,
                    label=label,
                    query=query,
                    requested=self._search_depth,
                    posts=posts,
                )
            )
        return axes

    def _analysis_targets(self, measurement: OmiyageMeasurement) -> list[PostRecord]:
        """動画解析の対象（ブランド軸+競合第1軸の上位N + Q5候補・video_id 重複排除）。

        Q5/表紙の実フレーム埋め込み（image_kind=real_frame）は解析済みフレームを
        再利用するため、組成側と同一定義の TOP5 候補（deck_plan.top5_candidates）も
        解析対象へ含める。analyzer の max_videos 既定(25)はこの合計と対。
        """
        axes = [measurement.brand_axis, next(iter(measurement.competitor_axes), None)]
        seen: set[str] = set()
        targets: list[PostRecord] = []
        for axis_measurement in axes:
            if axis_measurement is None:
                continue
            for post in axis_measurement.axis.posts[: self._analysis_per_axis]:
                if post.video_id not in seen:
                    seen.add(post.video_id)
                    targets.append(post)
        for post in top5_candidates(measurement):
            if post.video_id not in seen:
                seen.add(post.video_id)
                targets.append(post)
        return targets

    def _run_video_analysis(
        self,
        measurement: OmiyageMeasurement,
        ctx: SkillContext,
        log: Any,
    ) -> VideoAnalysisReport | None:
        """動画解析（best-effort）。失敗・未実施でも資料生成は止めず開示に回す。"""
        try:
            analyzer = self._analyzer_factory(ctx.request_id)
        except Exception as exc:
            log.warning(
                "omiyage_report_analyzer_init_failed",
                error_type=type(exc).__name__,
            )
            return None
        if analyzer is None:
            log.info("omiyage_report_video_analysis_unavailable")
            return None
        targets = self._analysis_targets(measurement)
        if not targets:
            return None
        try:
            report = analyzer.analyze(targets)
        except Exception as exc:
            log.warning(
                "omiyage_report_video_analysis_failed",
                error_type=type(exc).__name__,
            )
            return None
        log.info(
            "omiyage_report_video_analysis_done",
            requested=report.requested,
            analyzed=report.analyzed,
            failed=len(report.failures),
            skipped=len(report.skipped_video_ids),
            cost_usd_estimate=round(report.cost_usd_estimate, 4),
        )
        return report

    @staticmethod
    def _analysis_summary(analysis: VideoAnalysisReport | None) -> OmiyageVideoAnalysisSummary:
        if analysis is None:
            return OmiyageVideoAnalysisSummary(executed=False)
        return OmiyageVideoAnalysisSummary(
            executed=True,
            requested=analysis.requested,
            analyzed=analysis.analyzed,
            failed=len(analysis.failures),
            skipped=len(analysis.skipped_video_ids),
            skip_reason=analysis.skip_reason,
            cost_usd_estimate=round(analysis.cost_usd_estimate, 4),
            cost_cap_usd=analysis.cost_cap_usd,
            model_id=analysis.model_id,
        )

    def _store_plan_artifacts(
        self,
        job_key: str,
        plan_json: str,
        audit_json: str,
        log: Any,
    ) -> tuple[str, str]:
        """計測JSONと監査JSONをS3へ保存する（未設定・失敗は "" で開示）。"""
        uris: list[str] = []
        for name, body in (("deck_plan.json", plan_json), ("audit.json", audit_json)):
            try:
                uri = self._plan_uploader(
                    f"{job_key}/{name}", body.encode("utf-8"), "application/json"
                )
            except Exception as exc:
                log.warning(
                    "omiyage_report_plan_upload_failed",
                    artifact=name,
                    error_type=type(exc).__name__,
                )
                uri = ""
            uris.append(uri)
        return uris[0], uris[1]

    def _execute(
        self,
        input: OmiyageReportSubmitInput,
        ctx: SkillContext,
    ) -> OmiyageReportResult:
        log = ctx.bind_logger(self.name)
        axes = self._search_axes(input, ctx, log)
        if all(axis.failed for axis in axes):
            raise _OmiyageAllSearchesFailedError(build_all_failed_message())

        measurement = measure(
            axes,
            brand=input.brand,
            competitors=input.competitors,
            keywords=input.keywords,
            official_account=input.official_tiktok_account,
        )
        analysis = self._run_video_analysis(measurement, ctx, log)
        generated_on = _utc_now().strftime("%Y-%m-%d")
        plan = build_deck_plan(
            measurement,
            analysis,
            generated_on=generated_on,
            search_depth=self._search_depth,
            research_notes=input.research_notes,
        )
        plan_json = plan.model_dump_json()
        audit = build_audit(
            measurement,
            analysis,
            plan,
            generated_on=generated_on,
            search_depth=self._search_depth,
            research_notes=input.research_notes,
        )
        deck_plan_uri, audit_uri = self._store_plan_artifacts(
            f"{generated_on}/{ctx.request_id}",
            plan_json,
            json.dumps(audit, ensure_ascii=False),
            log,
        )

        summary_lines = build_summary_lines(measurement)
        next_step = build_next_step()
        failed_labels = [axis.label for axis in measurement.failed_axes]
        analysis_note = build_analysis_note(analysis)
        status: Literal["ready", "partial"] = (
            "partial" if failed_labels or analysis is None or analysis.analyzed == 0 else "ready"
        )

        workdir = tempfile.mkdtemp(prefix="omiyage-report-")
        try:
            # レンダラ境界: 計測JSON（契約 DeckPlan）だけを渡し、画像モード（正）+
            # 編集用（併走）の2ファイルを受け取る
            image_path, editable_path = self._deck_builder(plan_json, workdir, ctx.request_id)
            filename = Path(image_path).name
            disclosures: list[str] = []
            if failed_labels:
                disclosures.append(build_partial_message(failed_labels))
            if analysis_note:
                disclosures.append(analysis_note)
            # 配信文の順序は固定: 要点(+開示) → 修正ループ案内（固定文） → 次の一手
            comment = build_delivery_comment([*summary_lines, *disclosures], next_step)
            title = f"{input.brand}様 TikTok検索データ確認資料"
            delivered, delivery_target = asyncio.run(
                self._deliver(
                    path=image_path,
                    title=title,
                    comment=comment,
                    ctx=ctx,
                    extra_files=[(editable_path, f"{title}（{EDIT_MARKER}）")],
                )
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        if delivered:
            message = (
                "お土産資料を生成し、依頼元スレッドへ添付しました。"
                if delivery_target == "thread"
                else "お土産資料を生成しました（スレッド添付に失敗したためDMへ送付しました）。"
            )
            if failed_labels:
                message += " " + build_partial_message(failed_labels)
            if analysis_note:
                message += " " + analysis_note
        else:
            message = build_delivery_failed_note()

        return OmiyageReportResult(
            status=status,
            message=message,
            summary_lines=summary_lines,
            next_step=next_step,
            slack_delivered=delivered,
            delivery_target=delivery_target,
            axes=[
                OmiyageAxisSummary(
                    role=axis.role,
                    label=axis.label,
                    query=axis.query,
                    requested=axis.requested,
                    fetched=len(axis.posts),
                    failed=axis.failed,
                    failure_code=axis.failure_code,
                )
                for axis in axes
            ],
            pptx_filename=filename,
            spec_version=SPEC_VERSION,
            video_analysis=self._analysis_summary(analysis),
            deck_plan_s3_uri=deck_plan_uri,
            audit_s3_uri=audit_uri,
        )

    async def _deliver(
        self,
        *,
        path: str,
        title: str,
        comment: str,
        ctx: SkillContext,
        extra_files: list[tuple[str, str]] | None = None,
    ) -> tuple[bool, Literal["thread", "dm", "none"]]:
        """依頼元スレッド最優先で添付し、失敗時のみ本人DMへフォールバックする。

        ``extra_files`` は (path, title) の追加添付（FMT便1の編集用PPTX等）。
        コメントは先頭ファイルにだけ付け、同じスレッドへ続けて添付する。
        全ファイル成功のときのみ配達成功として扱う。
        """
        slack = self._slack
        if slack is None:
            from teamagent.adapters.slack_client import SlackClient

            slack = SlackClient.from_env(
                timeout_seconds=_envint(
                    "OMIYAGE_SLACK_UPLOAD_TIMEOUT_SECONDS",
                    240,
                    minimum=30,
                    maximum=900,
                )
            )
            self._slack = slack

        files: list[tuple[str, str]] = [(path, title), *(extra_files or [])]

        async def upload_all(target: str, thread_ts: str | None) -> bool:
            for index, (file_path, file_title) in enumerate(files):
                ok = await slack.upload_file(
                    target,
                    file_path,
                    ctx.request_id,
                    title=file_title,
                    initial_comment=comment if index == 0 else None,
                    thread_ts=thread_ts,
                )
                if not ok:
                    return False
            return True

        channel = ctx.metadata.get("channel_id")
        channel = channel if isinstance(channel, str) and channel else None
        thread_ts = ctx.metadata.get("thread_ts")
        thread_ts = thread_ts if isinstance(thread_ts, str) and thread_ts else None
        if channel and await upload_all(channel, thread_ts):
            return True, "thread"

        requester = ctx.metadata.get("user_email")
        requester = requester.strip() if isinstance(requester, str) and requester.strip() else None
        if requester:
            user_id = await slack.lookup_user_id_by_email(requester, ctx.request_id)
            if user_id:
                dm = await slack.open_dm(user_id, ctx.request_id)
                if dm and await upload_all(dm, None):
                    return True, "dm"
        return False, "none"


@register
class OmiyageReportStatusSkill(BaseSkill[OmiyageReportStatusInput, OmiyageReportStatusOutput]):
    """omiyage_report_submit が返した job_id の状態照会（omy_ 以外は受け付けない）。"""

    name: ClassVar[str] = "omiyage_report_status"
    description: ClassVar[str] = (
        "omiyage_report_submitが返したjob_id（omy_...）のqueued/running/done/failedを照会する。"
        "doneなら要点3行・次の一手・スレッド添付済みフラグ・検索軸ごとの取得状況を返す。"
        "running中は再submitせず、retry_after_seconds後に同じjob_idを再照会する。"
    )
    input_schema: ClassVar[type[BaseModel]] = OmiyageReportStatusInput
    output_schema: ClassVar[type[BaseModel]] = OmiyageReportStatusOutput
    version: ClassVar[str] = "1.0"
    owner: ClassVar[str] = "Aico"
    audit_tag: ClassVar[str] = "omiyage-report-status"

    def __init__(
        self,
        *,
        store: ProposalJobStore | None = None,
        stale_after_seconds: int | None = None,
        retry_after_seconds: int | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._store = store or ProposalJobStore()
        self._stale_after_seconds = (
            _configured_stale_seconds()
            if stale_after_seconds is None
            else max(1, stale_after_seconds)
        )
        self._retry_after_seconds = (
            _configured_retry_seconds()
            if retry_after_seconds is None
            else max(0, retry_after_seconds)
        )
        self._clock = clock

    def run(
        self,
        input: OmiyageReportStatusInput,
        ctx: SkillContext,
    ) -> OmiyageReportStatusOutput:
        log = ctx.bind_logger(self.name)
        row = self._store.get_job(input.job_id)
        if row is None:
            return OmiyageReportStatusOutput(
                job_id=input.job_id,
                status="failed",
                error_code="JOB_NOT_FOUND",
                message="そのjob_idは見つかりません。",
            )
        if not self._is_omiyage_row(row):
            # 異種 job（例: proposal_builder）は読み取りだけで拒否し、書き込まない。
            return OmiyageReportStatusOutput(
                job_id=input.job_id,
                status="failed",
                error_code="JOB_KIND_MISMATCH",
                message="そのjob_idはお土産資料のjobではありません。",
            )

        raw_status = row.get("status")
        if raw_status in ("queued", "running"):
            error_code = self._active_failure_code(row)
            if error_code is not None:
                if self._store.mark_failed(
                    input.job_id,
                    error_code,
                    expected_statuses=(raw_status,),
                    **self._timestamp_cas_args(row),
                ):
                    log.warning(
                        "omiyage_report_active_job_failed_closed",
                        job_id=input.job_id,
                        previous_status=raw_status,
                        error_code=error_code,
                    )
                row = self._store.get_job(input.job_id) or row
                if (
                    row.get("status") in ("queued", "running")
                    and (latest := self._active_failure_code(row)) is not None
                ):
                    log.error(
                        "omiyage_report_fail_closed_write_rejected",
                        job_id=input.job_id,
                        error_code=latest,
                    )
                    raise RuntimeError("omiyage job state could not be terminalized")

        raw_status = row.get("status")
        status = raw_status if raw_status in ("queued", "running", "done", "failed") else None
        if status is None:
            return OmiyageReportStatusOutput(
                job_id=input.job_id,
                status="failed",
                error_code="JOB_STATE_INVALID",
                message="jobの状態を判定できません。",
            )
        log.info("omiyage_report_status", job_id=input.job_id, status=status)
        if status == "done":
            return self._done_output(input.job_id, row)
        if status == "failed":
            error_code = row.get("error_code")
            code = error_code if isinstance(error_code, str) else "JOB_STATE_INVALID"
            message = (
                build_all_failed_message()
                if code == _OMIYAGE_SEARCH_FAILED
                else "お土産資料の生成に失敗しました。同じ内容で再依頼いただければ再実行します。"
            )
            return OmiyageReportStatusOutput(
                job_id=input.job_id,
                status="failed",
                error_code=code,
                message=message,
            )
        message = (
            "お土産資料の生成は順番待ちです。"
            if status == "queued"
            else "TikTok検索の実測と資料生成を進めています。"
        )
        return OmiyageReportStatusOutput(
            job_id=input.job_id,
            status=status,
            retry_after_seconds=self._retry_after_seconds,
            message=message,
        )

    @staticmethod
    def _is_omiyage_row(row: dict[str, Any]) -> bool:
        summary_raw = row.get("request_summary")
        if not isinstance(summary_raw, str):
            return False
        try:
            summary = json.loads(summary_raw)
        except ValueError:
            return False
        return isinstance(summary, dict) and summary.get("kind") == OMIYAGE_JOB_KIND

    def _active_failure_code(self, row: dict[str, Any]) -> str | None:
        timestamp = _parse_job_timestamp(row.get("updated_at"))
        if timestamp is None:
            return "JOB_STATE_INVALID"
        return "MCP_RESTARTED" if self._is_stale(timestamp) else None

    @staticmethod
    def _timestamp_cas_args(row: dict[str, Any]) -> dict[str, Any]:
        updated_at = row.get("updated_at")
        if isinstance(updated_at, str):
            return {"expected_updated_at": updated_at}
        if "updated_at" in row or row.get("_updated_at_invalid") is True:
            return {"expected_updated_at_invalid": True}
        return {"expected_updated_at_missing": True}

    def _is_stale(self, updated_at: datetime) -> bool:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return (now.astimezone(UTC) - updated_at).total_seconds() > self._stale_after_seconds

    def _done_output(self, job_id: str, row: dict[str, Any]) -> OmiyageReportStatusOutput:
        raw_result = row.get("result_json")
        try:
            if isinstance(raw_result, str):
                result = OmiyageReportResult.model_validate_json(raw_result)
            else:
                result = OmiyageReportResult.model_validate(raw_result)
        except Exception:
            transitioned = self._store.mark_failed(
                job_id,
                _RESULT_INVALID,
                expected_statuses=("done",),
                **self._timestamp_cas_args(row),
            )
            if not transitioned:
                latest = self._store.get_job(job_id)
                if latest is None or latest.get("status") != "failed":
                    raise RuntimeError("invalid omiyage result could not be terminalized") from None
            return OmiyageReportStatusOutput(
                job_id=job_id,
                status="failed",
                error_code=_RESULT_INVALID,
                message="完了結果を検証できませんでした。",
            )
        return OmiyageReportStatusOutput(
            job_id=job_id,
            status="done",
            report_status=result.status,
            result_message=result.message,
            summary_lines=result.summary_lines,
            next_step=result.next_step,
            slack_delivered=result.slack_delivered,
            delivery_target=result.delivery_target,
            axes=result.axes,
            video_analysis=result.video_analysis,
            deck_plan_s3_uri=result.deck_plan_s3_uri,
            audit_s3_uri=result.audit_s3_uri,
            message="お土産資料の生成が完了しました。",
        )


__all__ = [
    "OMIYAGE_JOB_KIND",
    "OmiyageReportStatusSkill",
    "OmiyageReportSubmitSkill",
    "clamp_search_depth",
    "configured_search_depth",
    "new_omiyage_job_id",
]
