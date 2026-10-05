"""検索上位チェック（search_surface_check）の 2 段目: 上位の動画の中身を裏で分析して追記する。

背景（2026-09-28 小俣さんの要望と裁定「2 段構えで自動」）: 検索上位チェックは約 2 分で結論を返す。
上位の動画は動画そのものを見ないと、フック・テロップ・構成・CTA というショート動画の評価軸で
語れない。そこで 1 段目は今どおり返し、**同じ上位 N 本**（検索し直さない）を video_algorithm の
分析エンジンで裏で分析して、終わったら（5〜9 分後）同じ会話に追記し、章を足したレポートを届ける。

仕組み（video_algorithm の切り離し＝detached_jobs.py の部品をそのまま使う）:
- gateway（``server.dispatch_tool``）が search_surface_check の skill.run が返った直後に
  ``maybe_schedule`` を呼ぶ。対象なら 2 段目のジョブを**同じ登録簿**（``detached_jobs.REGISTRY``）へ
  登録してすぐ切り離し、1 段目の slack_summary の末尾（レポート行の前）に予告を 1 行足す。
  対象外なら何もしない（1 段目は今と完全に同じ）。
- 宛先は署名検証済み claim から作る（``detached_jobs.destination_from_claim``）。DM だけ。
- ジョブは登録簿の thread で ``SearchSurfaceCheckSkill.run_video_followup`` を走らせ、完了したら
  ``post_to_origin``（検証済み宛先・2 回まで・DM 退避・エスケープ）で追記する。
- 同時実行の上限と順番待ちは video_algorithm の切り離しと**共有**する
  （VIDEO_ALGORITHM_MAX_BACKGROUND 既定 2・待ち 10 件）。どちらも mcp の同じタスクで media job と
  Gemini を使う重い処理なので、別枠にすると同時に 4 本走ってメモリと Gemini の 429 を踏む。
- 再デプロイ時は ``notify_interrupted`` がこのジョブの宛先にも中断文を送る（登録時に文を渡す）。
- 二重依頼（同じ人・同じ KW の 2 段目が走っている）は登録しない。
- 月間上限（1 人月の動画分析の本数）: 予告に「最大 N 本使います」と書く。1 段目の時点で残りを
  読み（``VideoQuotaStore.peek_remaining``・消費しない）、残り 0 本なら予告の代わりに「上限に
  達しているため分析しません（残り 0 本）」を 1 行足して 2 段目を登録しない（結果が予告より先に
  届いて食い違うのを防ぐ）。上限 ON で依頼者のメールが無いときも登録しない。
- 使い回し: 同じ人・同じ KW（正規化）・同じ上位 N 本の URL 集合の 2 段目が 24 時間以内に成功して
  いれば（全本を動画で分析でき、レポートも出せたときだけ覚える＝``is_reusable``）、
  分析し直さず前回の追記文（章を足したレポートの URL つき）を先頭に「（24 時間以内の同じ
  分析の結果です）」を付けて届ける。月間上限も Gemini も使わない。**プロセス内の TTL キャッシュ**
  なので、再デプロイ・タスクの入れ替わりで消える（消えたら分析し直す）。
- 待ち行列では、利用者が明示的に頼んだ動画分析（video_algorithm の切り離し）を、この自動の
  2 段目より先に始める（``detached_jobs.PRIORITY_AUTO``）。

段階公開のための env（TD の env で変えられる）:
- ``USE_SURFACE_VIDEO_FOLLOWUP``: 既定 OFF＝今と完全に同じ。
- ``SURFACE_VIDEO_FOLLOWUP_ALLOWED_EMAILS``: カンマ区切り。**空なら誰にも適用しない**。
- ``SURFACE_VIDEO_FOLLOWUP_MAX_VIDEOS``: 既定 5（1〜10 に丸める）。
  動画分析の月間上限を 1 回で最大この本数使う。

利用者向けの文には内部語（job_id・error_code・S3 URL・ツール名）を出さない。
"""

from __future__ import annotations

import asyncio
import functools
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog

from teamagent.mcp_gateway import detached_jobs
from teamagent.skills._shared.slack_blocks import RichMessage, render_or_none
from teamagent.skills._shared.slack_mrkdwn import markdown_bold_to_mrkdwn
from teamagent.skills.base import SkillContext

logger = structlog.get_logger(__name__)

TOOL = "search_surface_check"
# usage_events と登録簿の tool 名（1 段目の search_surface_check と分けて数える）。
USAGE_SKILL = "search_surface_check_video"

ENABLED_ENV = "USE_SURFACE_VIDEO_FOLLOWUP"
ALLOWED_EMAILS_ENV = "SURFACE_VIDEO_FOLLOWUP_ALLOWED_EMAILS"
MAX_VIDEOS_ENV = "SURFACE_VIDEO_FOLLOWUP_MAX_VIDEOS"

DEFAULT_MAX_VIDEOS = 5
MIN_MAX_VIDEOS = 1
MAX_MAX_VIDEOS = 10

# 登録簿のキーの接頭辞（video_algorithm の切り離しのキーと衝突させない）。
_KEY_PREFIX = "surface_video"

# 使い回しの有効期間と、キャッシュに置く件数の上限（古いものから捨てる）。
REUSE_TTL_S = 24 * 60 * 60
REUSE_MAX_ENTRIES = 256
REUSED_PREFIX = "（24 時間以内の同じ分析の結果です）"
# 使い回しの追記を投稿するまで待つ秒数。すぐ投稿すると、Aico が返す 1 段目の文面（予告つき）より
# 先に届いてしまうので、少し置く。待ちは threading.Timer で登録簿の終了処理と無関係なので、
# この秒数のうちに再デプロイされると追記は黙って消える
# （予告だけ届く。もう一度依頼すれば分析し直す）。
REUSE_POST_DELAY_S = 15.0


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes"}


def _max_videos_from_env() -> int:
    raw = os.environ.get(MAX_VIDEOS_ENV, "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_VIDEOS
    except ValueError:
        value = DEFAULT_MAX_VIDEOS
    return min(MAX_MAX_VIDEOS, max(MIN_MAX_VIDEOS, value))


@dataclass(frozen=True)
class FollowupPolicy:
    """2 段目を使うかどうかの決まり（env から読む）。既定は「使わない」。"""

    enabled: bool = False
    allowed_emails: frozenset[str] = frozenset()
    max_videos: int = DEFAULT_MAX_VIDEOS

    @classmethod
    def from_env(cls) -> FollowupPolicy:
        return cls(
            enabled=_truthy(os.environ.get(ENABLED_ENV)),
            allowed_emails=frozenset(
                e.strip().lower()
                for e in os.environ.get(ALLOWED_EMAILS_ENV, "").split(",")
                if e.strip()
            ),
            max_videos=_max_videos_from_env(),
        )


def load_policy() -> FollowupPolicy:
    """呼び出しごとに env を読む（TD 差し替えだけで段階を進められる。テストの差し替え口）。"""
    return FollowupPolicy.from_env()


def decide(
    policy: FollowupPolicy, *, verified_caller: Any, metadata: dict[str, Any]
) -> tuple[detached_jobs.Destination | None, str]:
    """2 段目を始めてよいかを決める。``(宛先, 理由)``。宛先が None なら始めない（今と同じ）。

    video_algorithm の切り離し（detached_jobs.decide）と同じ考え方: LEGACY（claim 無し）・
    未検証・allowlist 外（空は全員拒否）・宛先が決まらない・DM 以外は始めない。
    """
    if not policy.enabled:
        return None, "disabled"
    if verified_caller is None:
        return None, "legacy"
    if metadata.get("identity_verified") is not True:
        return None, "unverified"
    email = metadata.get("user_email")
    if not isinstance(email, str) or email.strip().lower() not in policy.allowed_emails:
        return None, "not_allowed"
    destination = detached_jobs.destination_from_claim(verified_caller)
    if destination is None:
        return None, "no_destination"
    if not destination.is_dm:
        return None, "not_dm"
    return destination, "ok"


def followup_key(slack_user_id: str, keyword: str) -> str:
    return f"{_KEY_PREFIX}\x1f" + detached_jobs.inflight_key(slack_user_id, keyword)


def reuse_key(slack_user_id: str, keyword: str, urls: list[str]) -> str:
    """使い回しのキー＝同じ人・同じ KW（正規化）・同じ上位 N 本の URL 集合。"""
    return followup_key(slack_user_id, keyword) + "\x1f" + "\x1e".join(sorted(set(urls)))


# ── 使い回し（プロセス内の TTL キャッシュ）─────────────────────────────────


@dataclass(frozen=True)
class CachedFollowup:
    """前回の 2 段目の結果（追記の文面と、章を足したレポートの URL）。

    ``source`` は前回の出力（SurfaceVideoFollowupOutput）。使い回しの追記を Block Kit で
    描き直すのに使う（無ければ文字だけで届ける）。
    """

    slack_text: str
    report_url: str | None
    stored_at: float
    source: Any = None


class FollowupCache:
    """2 段目の結果を 24 時間だけ覚えておく（プロセス内・再起動で消える）。"""

    def __init__(
        self,
        *,
        ttl_s: float = REUSE_TTL_S,
        max_entries: int = REUSE_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, CachedFollowup] = OrderedDict()

    def get(self, key: str) -> CachedFollowup | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if self._clock() - entry.stored_at >= self._ttl_s:
                del self._entries[key]
                return None
            return entry

    def put(self, key: str, *, slack_text: str, report_url: str | None, source: Any = None) -> None:
        with self._lock:
            self._entries.pop(key, None)
            self._entries[key] = CachedFollowup(
                slack_text=slack_text,
                report_url=report_url,
                stored_at=self._clock(),
                source=source,
            )
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


CACHE = FollowupCache()


def is_reusable(result: Any) -> bool:
    """使い回してよい結果か＝全本を動画で分析でき、章つきレポートを出せたときだけ。

    一部だけの結果（月間上限で予約が足りない・一部を取得できない・サムネだけの分析に落ちた・
    レポートを出せなかった）を覚えると、一時的な失敗が 24 時間固定される（B5 レビュー指摘）。
    """
    if getattr(result, "status", None) != "ok" or not getattr(result, "report_url", None):
        return False
    digest = getattr(result, "digest", None)
    return (
        digest is not None
        and digest.requested > 0
        and digest.watched == digest.requested
        and not digest.cover_only_ranks
        and not digest.failed_ranks
    )


def followup_rich(result: Any, *, request_id: str, reused: bool = False) -> RichMessage | None:
    """2 段目の追記の Block Kit 版（``search_surface_check/slack_render.py``）。

    分析できた結果（status=ok）だけ。描けない・想定外の出力・描画の例外は None
    （今の文字だけの追記に戻す＝結果を消さない）。
    """

    def _render() -> RichMessage | None:
        from teamagent.skills.search_surface_check.schema import SurfaceVideoFollowupOutput
        from teamagent.skills.search_surface_check.slack_render import followup_message

        if not isinstance(result, SurfaceVideoFollowupOutput):
            return None
        return followup_message(result, reused=reused)

    return render_or_none(_render, request_id=request_id, kind="search_surface_check_video")


def reused_text(entry: CachedFollowup) -> str:
    """使い回しの追記文。先頭に断り書きを付け、概算は今回の費用（0）に置き換える。"""
    lines = entry.slack_text.split("\n")
    if lines and lines[-1].startswith("_概算 $"):
        lines[-1] = "_概算 $0.0000（前回の分析を使い回しました）_"
    return "\n".join([REUSED_PREFIX, *lines])


# ── 月間上限の残り（予告の前に読む・消費しない）──────────────────────────────


def quota_gate(ctx: SkillContext) -> tuple[str, int | None]:
    """``(状態, 残り本数)``。状態は off / no_identity / unknown / exhausted / available。

    - off: 上限を使わない設定（VIDEO_QUOTA_ENABLED 未設定）。
    - no_identity: 上限 ON で依頼者のメールが無い（2 段目の予約が必ず失敗するので登録しない）。
    - unknown: 台帳を読めない（予約と同じく止めない側に倒す）。
    """
    from teamagent.adapters.quota_store import VideoQuotaStore

    if not VideoQuotaStore.enabled():
        return "off", None
    email = str(ctx.metadata.get("user_email") or "").strip().lower()
    if not email:
        return "no_identity", None
    remaining = VideoQuotaStore().peek_remaining(email, request_id=ctx.request_id)
    if remaining is None:
        return "unknown", None
    return ("exhausted" if remaining <= 0 else "available"), remaining


# ── 利用者向けの文（内部語を出さない）──────────────────────────────────────


def interrupted_text(keyword: str) -> str:
    return (
        f"「{keyword}」上位の動画の中身の分析は、システム更新で中断されました。"
        "お手数ですが、検索上位チェックをもう一度依頼してください"
        "（もう一度依頼すると、動画分析の回数を使い直します）。"
    )


def failure_text(keyword: str, error: BaseException) -> str:
    return f"「{keyword}」上位の動画の中身: {detached_jobs.user_message_for_error(error)}"


def busy_line() -> str:
    return (
        "上位の動画の中身の分析は、いま混み合っているため今回は行いませんでした"
        "（動画分析の回数は使っていません）"
    )


def in_progress_line(*, same_conversation: bool) -> str:
    where = "この会話" if same_conversation else "最初にご依頼いただいた会話"
    return f"上位の動画の中身は、前のご依頼の分を分析中です。終わったら{where}に追記します"


_QUEUED_SUFFIX = "。混み合っているため、順番待ちのあと始めます"


# ── 1 通で届ける（10-05 小俣さん裁定: 検索上位＋全 KW の動画の中身を、全部そろってから 1 通）──
# 対象（2 段目の対象＝本人確認済みの DM・allowlist）で 2 段目を始められたときだけ、1 段目の結果は
# その場で出さず、ジョブの完了時に検索上位と動画の中身をまとめて 1 通で投稿する。
# 始められなかった（混雑・二重・上限 0・終了処理中）ときは今までどおり 1 段目をその場で出す。
# ジョブが失敗・中断しても検索上位の結果は必ず届ける（動画の中身が無い旨を添える）。


def one_shot_wait_text(keyword: str, count: int, *, queued: bool, reused: bool = False) -> str:
    from teamagent.skills.search_surface_check.confirm import estimate_minutes

    if reused:
        return (
            f"検索上位チェック「{keyword}」を作成中です。上位{count}本の動画の中身は 24 時間以内の"
            "同じ分析を使うので、まもなく検索上位とまとめて 1 通でお届けします"
            "（動画分析の回数は使いません）。"
        )
    return (
        f"検索上位チェック「{keyword}」を作成中です。検索上位と、上位{count}本の動画の中身"
        "（フック・テロップ・構成・CTA など）を全部そろえてから、この会話に 1 通でお届けします"
        f"（目安 約{estimate_minutes(count)}分・動画分析の回数を最大 {count} 本使います）"
        + (_QUEUED_SUFFIX if queued else "")
        + "。"
    )


def _defer(output: Any, text: str) -> None:
    """1 段目の応答を「作成中」の 1 行にする（結果は後で 1 通で出す）。"""
    output.slack_summary = text
    output.followup_note = ""
    output.report_url = None
    output.deferred = True


def one_shot_failed_note(keyword: str) -> str:
    return f"上位の動画の中身「{keyword}」は分析できませんでした（検索上位の結果だけお届けします）"


def one_shot_interrupted_text(stage1: Any, keyword: str) -> str:
    """再デプロイの中断文。検索上位の結果は失わずに届ける（動画の中身だけ依頼し直し）。"""
    return (
        markdown_bold_to_mrkdwn(str(stage1.slack_summary or ""))
        + "\n\n"
        + f"上位の動画の中身「{keyword}」の分析は、システム更新で中断されました。"
        "動画の中身が必要なら、もう一度依頼してください（動画分析の回数を使い直します）。"
    )


def one_shot_payload(
    stage1: Any,
    skill_input: Any,
    result: Any,
    error: BaseException | None,
    *,
    keyword: str,
    request_id: str,
    reused: bool = False,
) -> tuple[str, RichMessage | None]:
    """1 通の（文字, Block Kit）。動画の中身が描けなければ検索上位だけ＋分析できなかった旨。"""
    from teamagent.skills.search_surface_check.slack_render import (
        one_shot_message,
        surface_message,
    )

    surface_text = str(stage1.slack_summary or "")
    final_url = getattr(result, "report_url", None) if error is None else None
    if final_url and stage1.report_url and final_url != stage1.report_url:
        surface_text = surface_text.replace(stage1.report_url, final_url)
    if error is None and getattr(result, "status", None) == "ok":
        video_text = str(getattr(result, "slack_text", "") or "")
        if reused:
            video_text = "\n".join([REUSED_PREFIX, video_text])
        text = surface_text + "\n\n" + video_text
        rich = render_or_none(
            lambda: one_shot_message(stage1, skill_input, result, reused=reused),
            request_id=request_id,
            kind="search_surface_check_one_shot",
        )
        if rich is not None:
            return text, rich
    detail = (
        failure_text(keyword, error)
        if error is not None
        else str(getattr(result, "slack_text", "") or one_shot_failed_note(keyword))
    )
    text = surface_text + "\n\n" + detail
    note = one_shot_failed_note(keyword)
    marked = stage1.model_copy(update={"followup_note": note})
    rich = render_or_none(
        lambda: surface_message(marked, skill_input),
        request_id=request_id,
        kind="search_surface_check",
    )
    return text, rich


# ── 完了処理（ジョブの thread で走る）──────────────────────────────────────


def _complete(
    result: Any,
    error: BaseException | None,
    interrupted: bool,
    *,
    keyword: str,
    destination: detached_jobs.Destination,
    request_id: str,
    started: float,
    user_email: str | None,
    usage_user_id: str | None,
    fallback_user_id: str | None,
    record_usage: Callable[..., None],
    loop: asyncio.AbstractEventLoop,
    cache_key: str | None = None,
    stage1: Any = None,
    skill_input: Any = None,
) -> None:
    """追記の投稿 → 使い回し用に覚える → usage 記録。順番待ちのまま中断したら中断文だけ送る。

    ``stage1``（1 通で届けるときの 1 段目の結果）があれば、検索上位と動画の中身を 1 通で出す。
    失敗・中断でも検索上位の結果は必ず出す。
    """
    if isinstance(error, detached_jobs.DetachInterruptedError):
        logger.warning("surface_video_followup_queued_interrupted", request_id=request_id)
        if not interrupted:
            detached_jobs.post_to_origin(
                one_shot_interrupted_text(stage1, keyword)
                if stage1 is not None
                else interrupted_text(keyword),
                destination,
                request_id=request_id,
            )
        return
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    delivered = False
    if interrupted:
        # 中断文を送った後に完了した分は、矛盾する 2 通目を出さない。
        logger.warning("surface_video_followup_done_after_interrupt", request_id=request_id)
    else:
        rich: RichMessage | None = None
        if stage1 is not None:
            text, rich = one_shot_payload(
                stage1, skill_input, result, error, keyword=keyword, request_id=request_id
            )
        elif error is not None:
            text = failure_text(keyword, error)
        else:
            text = str(getattr(result, "slack_text", "") or "")
            rich = followup_rich(result, request_id=request_id)
        if text:
            delivered = detached_jobs.post_to_origin(
                markdown_bold_to_mrkdwn(text),
                destination,
                request_id=request_id,
                fallback_user_id=fallback_user_id,
                rich=rich,
            )
    if cache_key is not None and error is None and is_reusable(result):
        # 中断文を送った後に完了した分も覚える（もう一度依頼されたら回数を使わずに届けられる）。
        CACHE.put(
            cache_key,
            slack_text=str(getattr(result, "slack_text", "") or ""),
            report_url=getattr(result, "report_url", None),
            source=result,
        )
    cost = float(getattr(result, "total_cost_usd", 0.0) or 0.0) if result is not None else 0.0
    status = "ok" if error is None else "error"
    logger.info(
        "surface_video_followup_finished",
        request_id=request_id,
        latency_ms=elapsed_ms,
        tool_cost_usd=cost,
        followup_status=getattr(result, "status", None),
        delivered=delivered,
        status=status,
        error=None if error is None else type(error).__name__,
    )
    record = functools.partial(
        record_usage,
        request_id=request_id,
        skill=USAGE_SKILL,
        user_email=user_email,
        user_id=usage_user_id,
        cost_usd=cost,
        latency_ms=elapsed_ms,
        skill_args={},
        status=status,
        error_code=None if error is None else type(error).__name__,
    )
    try:
        loop.call_soon_threadsafe(record)
    except RuntimeError:
        logger.warning("usage_event_schedule_failed", request_id=request_id, error="LoopClosed")


# ── 登録（gateway から呼ぶ）─────────────────────────────────────────────


def maybe_schedule(
    *,
    skill: Any,
    output: Any,
    skill_input: Any,
    ctx: SkillContext,
    verified_caller: Any,
    metadata: dict[str, Any],
    usage_user_id: str | None,
    record_usage: Callable[..., None],
    loop: asyncio.AbstractEventLoop,
) -> str:
    """対象なら 2 段目を登録し、1 段目の slack_summary に 1 行足す。理由コードを返す。

    例外は外へ出さない（2 段目の不具合で 1 段目の返却を落とさない）。
    """
    try:
        return _maybe_schedule(
            skill=skill,
            output=output,
            skill_input=skill_input,
            ctx=ctx,
            verified_caller=verified_caller,
            metadata=metadata,
            usage_user_id=usage_user_id,
            record_usage=record_usage,
            loop=loop,
        )
    except Exception as exc:
        logger.warning(
            "surface_video_followup_schedule_failed",
            request_id=ctx.request_id,
            error=type(exc).__name__,
        )
        return "error"


def _maybe_schedule(
    *,
    skill: Any,
    output: Any,
    skill_input: Any,
    ctx: SkillContext,
    verified_caller: Any,
    metadata: dict[str, Any],
    usage_user_id: str | None,
    record_usage: Callable[..., None],
    loop: asyncio.AbstractEventLoop,
) -> str:
    from teamagent.skills.search_surface_check.schema import (
        SearchSurfaceCheckInput,
        SearchSurfaceCheckOutput,
    )
    from teamagent.skills.search_surface_check.skill import SearchSurfaceCheckSkill
    from teamagent.skills.search_surface_check.summary import (
        FOLLOWUP_QUOTA_EXHAUSTED_LINE,
    )
    from teamagent.skills.search_surface_check.video_digest import (
        followup_label,
        select_followup_videos,
    )

    if not (
        isinstance(skill, SearchSurfaceCheckSkill)
        and isinstance(output, SearchSurfaceCheckOutput)
        and isinstance(skill_input, SearchSurfaceCheckInput)
    ):
        return "not_applicable"
    policy = load_policy()
    destination, reason = decide(policy, verified_caller=verified_caller, metadata=metadata)
    if destination is None:
        logger.info("surface_video_followup_decision", request_id=ctx.request_id, reason=reason)
        return reason
    # 本数は依頼者の指定（確認で 5 か 10）が先、無ければ env の既定。
    max_videos = getattr(skill_input, "max_videos", None) or policy.max_videos
    videos = select_followup_videos(output, max_videos)
    if not videos:
        logger.info(
            "surface_video_followup_decision", request_id=ctx.request_id, reason="no_videos"
        )
        return "no_videos"
    keyword = followup_label(output, videos)
    # 1 通で届けるときの 1 段目の結果（予告の行を足す前の写し）。始められたときだけ使う。
    stage1 = output.model_copy(deep=True)
    slack_user_id = str(verified_caller.slack_user_id)
    cache_key = reuse_key(slack_user_id, keyword, [p.url for p in videos])
    cached = CACHE.get(cache_key)
    if cached is not None:
        _post_reused_later(
            cached,
            destination=destination,
            request_id=f"{ctx.request_id}-video",
            fallback_user_id=slack_user_id,
            stage1=stage1,
            skill_input=skill_input,
            keyword=keyword,
        )
        _defer(output, one_shot_wait_text(keyword, len(videos), queued=False, reused=True))
        logger.info("surface_video_followup_decision", request_id=ctx.request_id, reason="reused")
        return "reused"
    quota_state, remaining = quota_gate(ctx)
    if quota_state == "no_identity":
        logger.info(
            "surface_video_followup_decision", request_id=ctx.request_id, reason="no_identity"
        )
        return "no_identity"
    if quota_state == "exhausted":
        _add_line(output, FOLLOWUP_QUOTA_EXHAUSTED_LINE)
        logger.info(
            "surface_video_followup_decision", request_id=ctx.request_id, reason="quota_exhausted"
        )
        return "quota_exhausted"
    shared = detached_jobs.load_policy()  # 同時実行の上限・待ち行列は動画分析の切り離しと共有
    if shared.max_background <= 0:
        logger.info("surface_video_followup_decision", request_id=ctx.request_id, reason="capacity")
        return "capacity"
    registry = detached_jobs.REGISTRY
    job_ctx = SkillContext(
        request_id=f"{ctx.request_id}-video",
        user_id=ctx.user_id,
        metadata=dict(ctx.metadata),
    )
    will_wait = registry.active_count() >= shared.max_background
    job, state = registry.start(
        key=followup_key(slack_user_id, keyword),
        max_background=shared.max_background,
        max_queued=shared.max_queued,
        tool=USAGE_SKILL,
        query=keyword,
        request_id=job_ctx.request_id,
        destination=destination,
        target=functools.partial(
            skill.run_video_followup, output, skill_input, job_ctx, videos=videos
        ),
        on_detached_done=functools.partial(
            _complete,
            keyword=keyword,
            destination=destination,
            request_id=job_ctx.request_id,
            started=time.perf_counter(),
            user_email=metadata.get("user_email"),
            usage_user_id=usage_user_id,
            fallback_user_id=slack_user_id,
            record_usage=record_usage,
            loop=loop,
            cache_key=cache_key,
            stage1=stage1,
            skill_input=skill_input,
        ),
        interrupted_message=one_shot_interrupted_text(stage1, keyword),
        priority=detached_jobs.PRIORITY_AUTO,
    )
    line: str | None
    if state == "duplicate" and job is not None:
        same = job.destination.channel_id == destination.channel_id
        line = in_progress_line(same_conversation=same)
    elif state == "busy":
        line = busy_line()
    elif state == "started" and job is not None:
        detach_state = job.detach()
        if detach_state == detached_jobs.DETACH_INTERRUPTED:
            state, line = "closing", None  # 終了処理中＝届けられない約束はしない
        else:
            if detach_state == detached_jobs.DETACH_DONE:
                job.deliver_in_background()
            line = None
            _defer(output, one_shot_wait_text(keyword, len(videos), queued=will_wait))
    else:
        line = None  # closing（終了処理中）
    if line:
        _add_line(output, line)
    logger.info(
        "surface_video_followup_decision",
        request_id=ctx.request_id,
        reason=state,
        videos=len(videos),
        queued=will_wait,
        quota_remaining=remaining,
    )
    return state


def _add_line(output: Any, line: str) -> None:
    """1 段目の文面（レポート行の前）に 1 行足し、直接投稿の Block Kit にも同じ行を渡す。"""
    from teamagent.skills.search_surface_check.summary import insert_before_report_line

    output.slack_summary = insert_before_report_line(output.slack_summary, line)
    output.followup_note = line


def _post_reused_later(
    entry: CachedFollowup,
    *,
    destination: detached_jobs.Destination,
    request_id: str,
    fallback_user_id: str,
    stage1: Any = None,
    skill_input: Any = None,
    keyword: str = "",
) -> None:
    """使い回しの追記を、1 段目の返信が届くころに別 thread で投稿する（登録簿は使わない）。

    ``stage1`` があれば検索上位とまとめて 1 通で出す（前回の結果が描けなければ検索上位＋前回の文）。
    """

    def _post() -> None:
        if stage1 is not None and entry.source is not None:
            text, rich = one_shot_payload(
                stage1,
                skill_input,
                entry.source,
                None,
                keyword=keyword,
                request_id=request_id,
                reused=True,
            )
        elif stage1 is not None:
            text = str(stage1.slack_summary or "") + "\n\n" + reused_text(entry)
            rich = None
        else:
            text = reused_text(entry)
            rich = (
                followup_rich(entry.source, request_id=request_id, reused=True)
                if entry.source is not None
                else None
            )
        delivered = detached_jobs.post_to_origin(
            markdown_bold_to_mrkdwn(text),
            destination,
            request_id=request_id,
            fallback_user_id=fallback_user_id,
            rich=rich,
        )
        logger.info("surface_video_followup_reused", request_id=request_id, delivered=delivered)

    timer = threading.Timer(REUSE_POST_DELAY_S, _post)
    timer.name = f"{USAGE_SKILL}-reuse-{request_id}"
    timer.daemon = True
    timer.start()


__all__ = [
    "ALLOWED_EMAILS_ENV",
    "CACHE",
    "ENABLED_ENV",
    "MAX_VIDEOS_ENV",
    "REUSED_PREFIX",
    "REUSE_POST_DELAY_S",
    "REUSE_TTL_S",
    "TOOL",
    "USAGE_SKILL",
    "CachedFollowup",
    "FollowupCache",
    "FollowupPolicy",
    "busy_line",
    "decide",
    "failure_text",
    "followup_key",
    "followup_rich",
    "in_progress_line",
    "interrupted_text",
    "is_reusable",
    "load_policy",
    "maybe_schedule",
    "quota_gate",
    "reuse_key",
    "reused_text",
]
