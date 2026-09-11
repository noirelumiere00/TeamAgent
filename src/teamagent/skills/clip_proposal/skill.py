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
- 既定 OFF: ``run()`` の先頭（本人確認より前）で ``enabled()`` を見る。``@register``
  していないことだけに頼ると、便C で registry へ載せた瞬間に env フラグ抜きで走る。
- 本人限定: ``identity_verified`` と ``verified_slack_user_id`` が無ければ PermissionError。
  ``CLIP_PROPOSAL_USERS`` 未設定・空文字は **全員拒否**。
- 素材の同意: 候補は「依頼スレッド内、かつ ``file.user == verified_slack_user_id``」のみ。
- 配達: verified 由来の ``channel_id`` と ``thread_ts`` が **両方**揃ったスレッドのみ。
  揃わなければ本人 DM 固定へ倒す（チャンネル直投稿はしない）。宛先は ``_deliver`` が
  ``resolve_delivery_target`` で決めて deliverer へ **引数で渡す**（deliverer 側に
  宛先を決めさせない）。
- ジョブの覗き見: ジョブ行に ``sha256(user_email + salt)`` を持ち、不一致なら
  ``JOB_NOT_FOUND``（存在も漏らさない）。**transcript はジョブ行に保存しない**。
- 同時走行: ``JobSlots`` で本数を絞る。溢れた依頼は **断らず** 順番待ちに入れ、
  背景スレッドがスロットの空きを待ってから着手する（``status=busy`` の約束の実体）。
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
from teamagent.skills.clip_proposal.analysis import (
    ClipAnalysisError,
    ClipProposalAnalysis,
    spend_of,
)
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
#: 同時に走らせてよい解析の本数。mcp は desiredCount=1 なのでプロセス内で絞る。
MAX_CONCURRENT_JOBS_DEFAULT = 2

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


def require_enabled() -> None:
    """既定 OFF を **構造で** 効かせる（``@register`` していないことに頼らない）。

    便C で registry へ載せた瞬間、env フラグが無くても ``run()`` が走ってしまう。
    台帳テスト（``test_tool_scope_registry_contract``）は登録の有無しか見ないので、
    この抜けは検出されない。``run()`` の先頭・本人確認より前で止める。
    """

    if not enabled():
        raise PermissionError("clip_proposal is disabled (USE_CLIP_PROPOSAL_TOOLS)")


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

    スレッド配達は次の **3 つが揃っているときだけ**:

    1. ``identity_verified is True``。``mcp_gateway/server.py:482`` は
       ``channel_id = verified_caller.channel_id if verified_caller else raw.get(...)``
       で、``identity_verified=True`` を立てる経路だけが ``verified_caller`` 由来。
       つまりこの 1 行が「raw 由来の channel_id は使わない」の実体になる。
       未検証の metadata で呼ばれたら、値が入っていても DM へ倒す。
    2. ``channel_id`` が非空。
    3. ``thread_ts`` が非空（無いとチャンネル直投稿になる）。
    """

    if metadata.get("identity_verified") is not True:
        return "dm", "", ""
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
    def key(fingerprint: str, input: ClipProposalSubmitInput, *, thread_ts: str = "") -> str:
        """依頼者 × スレッド × 素材の識別子。**寄せてはいけないものを寄せない**。

        ``client_name`` は素材の識別子ではない。同じクライアント向けの 2 本目の動画を
        1 本目と同一視して握り潰すため、鍵に入れない。``thread_ts`` は必ず入れる
        （最も自然な入口 ＝ 動画を貼って「切り抜き提案作って」では ``file_id`` も
        ``client_name`` も空になりうるので、スレッドが唯一の区別になる）。

        素材もスレッドも特定できないときは **空文字を返して重複判定から外す**。
        鍵を潰して同一視すると、2 本目の依頼はジョブも背景タスクも作られないまま
        「受け付けています」とだけ答えることになり、利用者は永久に待つ。
        """

        material = (input.file_id or input.video_url).strip().lower()
        thread = thread_ts.strip()
        if not material and not thread:
            return ""
        return f"{fingerprint}:{thread}:{material}"

    def find(self, key: str) -> str | None:
        with self._lock:
            return self._active.get(key)

    def claim_if_free(self, key: str, job_id: str) -> str | None:
        """空いていれば ``job_id`` で押さえて None、埋まっていれば先客の job_id を返す。

        **検査と確保を 1 つのロックで行う**（find → claim の二段だと、同じ人の 2 本の
        submit が同時に走ったとき両方とも「空いている」を見てジョブを 2 本作る）。
        空鍵（＝素材もスレッドも特定できない依頼）は押さえずに素通しする。
        """

        if not key:
            return None
        with self._lock:
            existing = self._active.get(key)
            if existing is not None:
                return existing
            self._active[key] = job_id
            return None

    def release(self, key: str) -> None:
        if not key:
            return
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


def configured_max_concurrent_jobs() -> int:
    """同時に走らせてよい解析の本数（``CLIP_MAX_CONCURRENT_JOBS`` 既定 2）。"""

    raw = os.environ.get("CLIP_MAX_CONCURRENT_JOBS", "").strip()
    try:
        value = MAX_CONCURRENT_JOBS_DEFAULT if not raw else int(raw)
    except ValueError:
        value = MAX_CONCURRENT_JOBS_DEFAULT
    return min(8, max(1, value))


class JobSlots:
    """走行本数の門（順番待ちつき・プロセス内）。

    description が約束する ``status=busy``（順番待ち・自動着手・再依頼は不要）の
    **実体**。これが無いと日次枠 20 本が同時に走り、各々が 18MB の proxy を載せて
    メモリを食う。mcp は desiredCount=1 なので、タスクが落ちると走行中のジョブが
    全部消える一方で日次枠は消費済みのまま戻らない。

    順番待ちは「受け付けてから待たせる」。断って再送させない（計画 §2-2）。
    """

    def __init__(self, limit: int | None = None) -> None:
        self._cv = threading.Condition()
        self._limit = max(1, configured_max_concurrent_jobs() if limit is None else limit)
        self._running = 0
        self._waiting = 0

    @property
    def limit(self) -> int:
        return self._limit

    def enqueue(self) -> int:
        """受付時に呼ぶ。返り値は **自分の前にいる本数**（0 なら即着手できる）。"""

        with self._cv:
            position = max(0, self._running + self._waiting - self._limit + 1)
            self._waiting += 1
            return position

    def cancel(self) -> None:
        """``enqueue`` したが着手させない（ジョブ作成やスレッド起動に失敗した）。"""

        with self._cv:
            self._waiting = max(0, self._waiting - 1)
            self._cv.notify_all()

    def start(self, timeout: float | None = None) -> bool:
        """背景スレッドから呼ぶ。スロットが空くまで待ってから着手する。"""

        with self._cv:
            if not self._cv.wait_for(lambda: self._running < self._limit, timeout=timeout):
                return False
            self._waiting = max(0, self._waiting - 1)
            self._running += 1
            return True

    def finish(self) -> None:
        with self._cv:
            self._running = max(0, self._running - 1)
            self._cv.notify_all()

    def snapshot(self) -> tuple[int, int]:
        with self._cv:
            return self._running, self._waiting


_JOB_SLOTS = JobSlots()


def shared_job_slots() -> JobSlots:
    return _JOB_SLOTS


def reset_job_slots(limit: int | None = None) -> JobSlots:
    """共有の走行スロットを作り直す（テスト用・本番経路からは呼ばない）。"""

    global _JOB_SLOTS
    _JOB_SLOTS = JobSlots(limit)
    return _JOB_SLOTS


def build_duplicate_message(job_id: str) -> str:
    """重複 submit への返し。**やり直しをさせない**。"""

    return (
        "同じ素材の切り抜き提案をすでに受け付けています"
        f"（受付番号 {job_id}）。そのまま進めますので、もう一度送っていただく必要はありません。"
        "できたらこのスレッドに資料を添付します。"
    )


#: (analysis, out_dir, request_id) -> pptx path。テンプレ差し替えの実体。
DeckBuilder = Callable[[ClipProposalAnalysis, str, str], str]
#: (path, comment, ctx, target, channel_id, thread_ts) -> delivered
#:
#: 宛先は ``_deliver`` が ``resolve_delivery_target`` で決めて **引数で渡す**。
#: deliverer に ctx だけ渡して自分で決めさせると、便C で実 deliverer
#: （``SlackClient.upload_file``）を足す担当が resolve を呼び忘れた瞬間に、
#: C/G 始まりのチャンネルへ資料を直投稿する経路が開く。
Deliverer = Callable[[str, str, SkillContext, DeliveryTarget, str, str], bool]
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
        job_slots: JobSlots | None = None,
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
        self._slots_override = job_slots
        self._eta_minutes = max(1, eta_minutes)
        self._retry_after_seconds = max(0, retry_after_seconds)

    @property
    def _quota(self) -> DailyQuota:
        return shared_quota() if self._quota_override is None else self._quota_override

    @property
    def _active_jobs(self) -> ActiveJobIndex:
        return shared_active_jobs() if self._active_override is None else self._active_override

    @property
    def _slots(self) -> JobSlots:
        return shared_job_slots() if self._slots_override is None else self._slots_override

    def run(self, input: ClipProposalSubmitInput, ctx: SkillContext) -> ClipProposalSubmitOutput:
        log = ctx.bind_logger(self.name)
        require_enabled()
        requester = verify_requester(ctx)

        # 重複 submit は **日次枠を消費する前** に弾く（連打で枠と費用が倍にならない）。
        # 枠の確保もここで同時に行う（検査と確保を分けると同時 submit で 2 本作る）。
        job_id = new_clip_job_id()
        thread_ts = ctx.metadata.get("thread_ts") if ctx.metadata else ""
        dedupe_key = ActiveJobIndex.key(
            requester.fingerprint,
            input,
            thread_ts=thread_ts if isinstance(thread_ts, str) else "",
        )
        running = self._active_jobs.claim_if_free(dedupe_key, job_id)
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
            # 受け付けられなかった依頼で走行枠を塞がない（明日また送れる）。
            self._active_jobs.release(dedupe_key)
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

        # 走行スロットの順番待ちへ入れる（断らない）。position>0 なら status=busy を返すが、
        # ジョブも背景スレッドも作る。着手はスロットが空いたとき背景側で自動的に起きる。
        position = self._slots.enqueue()

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
            self._active_jobs.release(dedupe_key)
            self._slots.cancel()
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
        try:
            self._thread_launcher(
                lambda: self._run_background(
                    job_id, job_input, job_ctx, requester, dedupe_key=dedupe_key
                ),
                f"clip-proposal-{job_id}",
            )
        except Exception as exc:
            self._active_jobs.release(dedupe_key)
            self._slots.cancel()
            self._store.mark_failed(job_id, _JOB_START_FAILED, expected_statuses=("queued",))
            log.warning("clip_proposal_thread_start_failed", error_type=type(exc).__name__)
            return ClipProposalSubmitOutput(
                status="failed",
                job_id=job_id,
                client_name=input.client_name,
                message="切り抜き提案jobの開始に失敗しました。",
            )

        if position > 0:
            log.info("clip_proposal_queued_behind", job_id=job_id, position=position)
            return self.busy_output(
                job_id=job_id,
                position=position,
                wait_minutes=position * self._eta_minutes,
                client_name=input.client_name,
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

    def busy_output(
        self,
        *,
        position: int,
        wait_minutes: int,
        job_id: str = "",
        client_name: str = "",
    ) -> ClipProposalSubmitOutput:
        """順番待ちの返り（**再依頼を求めない**）。

        ジョブは既に作ってあり、背景スレッドがスロットの空きを待っている。
        ``job_id`` を返すので、待っている間も ``clip_proposal_status`` で進行が引ける。
        """

        return ClipProposalSubmitOutput(
            status="busy",
            job_id=job_id,
            retry_after_seconds=max(self._retry_after_seconds, wait_minutes * 60),
            client_name=client_name,
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
        # 走行スロットが空くまで待ってから着手する（受付時の「順番が来たら自動で始めます」）。
        # 待っている間 job は queued のまま＝status が「作成中」と嘘をつかない。
        self._slots.start()
        workdir = ""
        try:
            # mark_running（DB）と mkdtemp も try の内側に置く。ここで例外が出たとき
            # finally を通らないと **走行スロットが永久に 1 つ減る**。積み重なると
            # すべての依頼が空かないスロットを待ち続けて、mcp 再起動まで止まる。
            self._store.mark_running(job_id)
            workdir = tempfile.mkdtemp(prefix="clip-proposal-")
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
            # 失敗しても **そこまでに課金された分は計上する**。戻さないのが規律
            # （limits.py:9-10）だが、本数カウンタだけ守って費用カウンタを素通りさせると、
            # 出力が壊れ続ける日は CLIP_DAILY_COST_CAP_USD が一度も発火しない。
            spent = spend_of(exc)
            if spent > 0:
                self._quota.add_cost(spent)
            code = _safe_failure_code(exc)
            try:
                self._store.mark_failed(job_id, code, expected_statuses=("queued", "running"))
            except Exception:
                # 台帳が落ちている回でも finally（スロット返却）まで必ず到達させる。
                log.warning("clip_proposal_mark_failed_failed", job_id=job_id)
            log.warning(
                "clip_proposal_failed", job_id=job_id, error_code=code, spent_usd=round(spent, 6)
            )
        finally:
            # workdir（原本動画・抽出フレーム・生成 PPTX）は必ず消す。
            if workdir:
                shutil.rmtree(workdir, ignore_errors=True)
            if dedupe_key:
                self._active_jobs.release(dedupe_key)
            self._slots.finish()

    def _analyze(self, input: ClipProposalSubmitInput, ctx: SkillContext) -> ClipProposalAnalysis:
        if self._analyzer is None:
            raise ClipAnalysisError("CLIP_ANALYZER_UNAVAILABLE")
        return cast("ClipProposalAnalysis", self._analyzer.run_for_request(input, ctx))

    def _deliver(self, path: str, comment: str, ctx: SkillContext) -> tuple[bool, DeliveryTarget]:
        """**宛先をここで決めて** deliverer へ引数で渡す（deliverer に決めさせない）。"""

        if self._deliverer is None:
            return False, "none"
        target, channel_id, thread_ts = resolve_delivery_target(ctx.metadata or {})
        delivered = self._deliverer(path, comment, ctx, target, channel_id, thread_ts)
        return bool(delivered), target


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
        require_enabled()
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
    "MAX_CONCURRENT_JOBS_DEFAULT",
    "ActiveJobIndex",
    "ClipProposalStatusSkill",
    "ClipProposalSubmitSkill",
    "DeliveryTarget",
    "JobSlots",
    "VerifiedRequester",
    "allowed_users",
    "configured_max_concurrent_jobs",
    "enabled",
    "new_clip_job_id",
    "requester_fingerprint",
    "require_enabled",
    "reset_active_jobs",
    "reset_job_slots",
    "resolve_delivery_target",
    "shared_active_jobs",
    "shared_job_slots",
    "verify_requester",
]
