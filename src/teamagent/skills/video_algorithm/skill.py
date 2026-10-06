"""VideoAlgorithm Skill 本体（VSEO 動画アルゴリズム読み解き）。

検索KW → 上位N本（tiktok_search）→ 各動画 download→proxy→Gemini構造分析 → 5本横断
→ HTML タイムラインレポート + Slack 要約。

3層分離: Skill 層。検索/取得/圧縮/Gemini は adapters。重い I/O は ThreadPool で並列化。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from threading import Event, Lock, Thread
from typing import Any, ClassVar, Literal

import structlog
from pydantic import BaseModel, ValidationError

from teamagent.adapters.gemini_client import GeminiClient
from teamagent.adapters.retry import retry_long_job_once
from teamagent.adapters.tiktok_video_fallback import (
    ACQUIRED_VIA_APIFY,
    apify_fallback_enabled,
    fallback_deadline_s,
    fallback_job_id,
    fallback_max_videos,
    fill_missing_videos,
)
from teamagent.adapters.video_algorithm_cache import (
    CachedVideoAlgorithmResult,
    VideoAlgorithmCacheLease,
    VideoAlgorithmCacheLeaseHeldError,
    VideoAlgorithmCacheLeaseLostError,
    VideoAlgorithmCacheLeaseUnavailableError,
    VideoAlgorithmResultCache,
)
from teamagent.prompts.loader import load_prompt
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.video_algorithm.analysis import cross_analyze
from teamagent.skills.video_algorithm.cover_read import (
    COVER_PROMPT_SKILL,
    COVER_PROMPT_VERSION,
    COVER_WIDTH,
    CoverImage,
    CoverReader,
    CoverSettings,
    cover_version,
    image_of,
)
from teamagent.skills.video_algorithm.evidence import TIER_MAJORITY, TIER_REQUIRED, Roster
from teamagent.skills.video_algorithm.facts import JST
from teamagent.skills.video_algorithm.report import render_report
from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    CoverSource,
    FrameShot,
    ThumbColor,
    VideoAlgorithmInput,
    VideoAlgorithmOutput,
    VideoAlgorithmStatusInput,
    VideoAlgorithmStatusOutput,
    VideoMeta,
    VideoVSEOAnalysis,
)
from teamagent.skills.video_algorithm.slides import ordered_features
from teamagent.skills.video_algorithm.synthesis import synthesis_version_from_env
from teamagent.skills.video_algorithm.synthesis_input import SynthesisContext

logger = structlog.get_logger(__name__)


@contextmanager
def _stage(stage: str, request_id: str, rank: int | None = None) -> Iterator[None]:
    """工程ごとの所要時間を ``video_algorithm_stage`` として出す（計測のみ・処理は変えない）。

    2026-09-25 の 555 秒の内訳（media の Fargate 起動待ち・Gemini・仕上げ）を推測でなく
    実測で分けるための計器。失敗した工程も outcome=error で所要を残し、例外はそのまま流す。
    """
    started = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        logger.info(
            "video_algorithm_stage",
            stage=stage,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            rank=rank,
            outcome=outcome,
            request_id=request_id,
        )


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)

Searcher = Callable[[str, int, str], list[VideoMeta]]
Downloader = Callable[[str], tuple[bytes, str]]
Proxy = Callable[[bytes, str], tuple[bytes, str]]
# サムネ（一覧の表紙）の取得の注入口（テスト用。URL → 画像の bytes）。
CoverFetcher = Callable[[str], bytes]
# 表紙の読み取りが先に取った画像を待つ関数（1 本 1 回の取得を表示・色にも使い回す）。
Prefetched = Callable[[], CoverImage | None]

_MAX_FIELD_RESETS = 8  # 寛容パース: 最大何フィールドまで default に戻して動画を救済するか
_OVERFETCH_BUFFER = 4  # over-fetch: 目標+この本数を検索し DL/分析失敗を後続候補でバックフィル
_MAX_POOL = 30  # 検索の絶対上限（スクレイパ実証済み。レート制限/遮断を踏み抜かない）

# gemini-3.5-flash@global での 429 実測を受け、並列数を本番 env で調整可能にする。
_MAX_WORKERS_ENV = "VIDEO_ALGORITHM_MAX_WORKERS"
_MAX_WORKERS_DEFAULT = 3
_MAX_WORKERS_MIN = 1
_MAX_WORKERS_MAX = 8

# 二段構え（Apify 補完）の request 単位の予算。失敗 1 本ごとに同期 run（実測 ≈68s）を起こすため、
# 集約本数（TIKTOK_APIFY_FALLBACK_MAX_VIDEOS）と壁時計（MCP ツール呼び出し 300s 天井の内側）の
# 両方で頭打ちにし、DL 経路全滅でも tool 呼び出しが天井を超えないようにする。
_APIFY_WALLCLOCK_ENV = "VIDEO_ALGORITHM_APIFY_WALLCLOCK_S"
_APIFY_WALLCLOCK_DEFAULT_S = 240
_APIFY_S3_MARGIN_S = 30


PROMPT_VERSION_ENV = "VIDEO_ALGO_PROMPT_VERSION"
DEFAULT_PROMPT_VERSION = "v2"


def _int_or_zero(value: Any) -> int:
    """取得結果の整数欄（投稿日時など）。壊れた値は 0（不明）にする。"""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _str_list(value: Any) -> list[str]:
    """取得結果の文字列の並び（ハッシュタグなど）。文字列以外は捨てる。"""
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        return []
    return [str(x) for x in value if isinstance(x, str) and x.strip()]


def _kw_key(value: Any) -> str:
    """KW の照合キー（NFKC・大小・空白の畳みを同一視。search_surface_check の記録キーと同じ）。"""
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _posts_for_query(
    posts: list[dict[str, Any]], query: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """取得ジョブの posts から、分析する KW の投稿だけを表示順のまま選ぶ。

    tiktok_acquire は1ジョブに複数 KW を入れることがある（KW の数や本数によって、
    まとめて1ジョブ・KW ごとに分割のどちらにもなる）。KW で絞らずに先頭から読むと、
    別 KW の上位投稿でこの KW を分析してしまう。

    返り値は (選んだ投稿, ジョブに入っている KW)。
    - 一致する投稿がある → それだけ
    - ジョブが1KWだけ → 全件（依頼の言い回しがジョブの KW と少し違っても従来どおり読む）
    - 複数 KW のジョブで一致なし → 空（別 KW で代用しない）
    """

    job_keywords: list[str] = []
    for post in posts:
        kw = str(post.get("kw") or "").strip()
        if kw and kw not in job_keywords:
            job_keywords.append(kw)
    wanted = _kw_key(query)
    matched = [post for post in posts if _kw_key(post.get("kw")) == wanted]
    if matched:
        return matched, job_keywords
    if len(job_keywords) <= 1:
        return posts, job_keywords
    return [], job_keywords


def _echo_fields(input: VideoAlgorithmInput) -> dict[str, Any]:
    """区分・提案文の前提の echo（クライアント名・競合・避けたい訴求）。"""
    roster = Roster.of(input.client_name, input.competitors)
    return {
        "client_name": roster.client_name,
        "competitors": list(roster.competitors),
        "avoid_terms": [t.strip() for t in (input.avoid_terms or []) if t and t.strip()],
    }


# 冒頭のコマの秒（pick_timecodes の先頭と同じ）と、表紙の代用に使ってよいコマの秒の上限。
OPENING_FRAME_SEC = 0.8
OPENING_FRAME_MAX_SEC = 1.0


def _now_jst_iso() -> str:
    """取得日時（JST・秒まで）。順位は「この時点」の値として資料に出す。"""
    return datetime.now(JST).isoformat(timespec="seconds")


# クライアント名が無いときの Slack の最後の 1 行（仕様 v3 §3-4）。結果キャッシュのキーに
# client_name が入り、名前だけ違う依頼を再描画に回す経路（PR-5）がまだ無いので、名前を足して
# 依頼し直すと検索と動画の分析からやり直しになる。「動画の再分析なし」とは書かない（PR-5 で
# 再描画の経路を入れたら、文言とテスト test_client_note_matches_the_cache_behaviour を直す）。
CLIENT_MISSING_NOTE = (
    "クライアント名と競合を教えてもらえれば、区分と提案文を入れた版に作り直します"
    "（動画の分析からやり直すため数分かかります）"
)
_SLACK_POINTS = 3


def _tier_points(out: VideoAlgorithmOutput) -> str:
    """Slack に出す共通点（必須条件→多数派・段階の名前と本数はコードの集計）。無ければ空。"""
    roster = Roster.of(out.client_name, out.competitors)
    ctx = SynthesisContext.build(out.videos, out.query, board=out.board, roster=roster)
    feats = ordered_features(ctx.features, TIER_REQUIRED) + ordered_features(
        ctx.features, TIER_MAJORITY
    )
    return "／".join(f"{f.tier}『{f.label}』（{f.count}/{f.n}本）" for f in feats[:_SLACK_POINTS])


def prompt_version_from_env() -> str:
    """env VIDEO_ALGO_PROMPT_VERSION（未設定・空なら v2）。

    MCP（orchestrator/factory.py）と Slack（runtime/slack_bot.py）の両経路がこれで版を決める
    （v1 に戻す手段を片方の経路だけにしないため）。
    """
    return os.environ.get(PROMPT_VERSION_ENV, "").strip() or DEFAULT_PROMPT_VERSION


def _max_workers_from_env() -> int:
    raw = os.environ.get(_MAX_WORKERS_ENV)
    try:
        value = _MAX_WORKERS_DEFAULT if raw is None else int(raw)
    except ValueError:
        value = _MAX_WORKERS_DEFAULT
    return min(_MAX_WORKERS_MAX, max(_MAX_WORKERS_MIN, value))


def _apify_wallclock_budget_s() -> int:
    raw = os.environ.get(_APIFY_WALLCLOCK_ENV)
    try:
        value = _APIFY_WALLCLOCK_DEFAULT_S if raw is None else int(raw)
    except ValueError:
        value = _APIFY_WALLCLOCK_DEFAULT_S
    return min(900, max(30, value))


class _ApifyFallbackBudget:
    """request 単位の Apify 補完予算（本数の集約上限 + 壁時計）。ThreadPool 共有のため lock。"""

    def __init__(
        self,
        *,
        max_videos: int,
        wallclock_s: int,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_videos = max(0, max_videos)
        self._wallclock_s = wallclock_s
        self._monotonic = monotonic
        self._started = monotonic()
        self._used = 0
        self._lock = Lock()

    def remaining_s(self) -> float:
        return self._wallclock_s - (self._monotonic() - self._started)

    def try_take(self, needed_s: int) -> tuple[bool, str]:
        """1 本ぶんの枠を取る。取れなければ (False, 理由: wallclock | cap)。"""

        with self._lock:
            if self.remaining_s() < needed_s:
                return False, "wallclock"
            if self._used >= self._max_videos:
                return False, "cap"
            self._used += 1
            return True, ""


class _LeaseHeartbeat:
    """長時間のGemini処理中もS3 leaseを更新し、ownership喪失を課金境界へ伝える。"""

    def __init__(
        self,
        cache: VideoAlgorithmResultCache,
        lease: VideoAlgorithmCacheLease,
        request_id: str,
    ) -> None:
        self._cache = cache
        self._lease = lease
        self._request_id = request_id
        self._stop = Event()
        self._lost: VideoAlgorithmCacheLeaseLostError | None = None
        self._unavailable: VideoAlgorithmCacheLeaseUnavailableError | None = None
        # background heartbeat と課金境界の同期renewが同じETagで競合しないよう直列化する。
        self._renew_lock = Lock()
        self._thread = Thread(
            target=self._run,
            name=f"video-algorithm-lease-{request_id[:24]}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        interval = self._cache.lease_heartbeat_seconds
        while not self._stop.wait(interval):
            with self._renew_lock:
                if self._stop.is_set():
                    return
                try:
                    self._cache.renew_lease(self._lease, request_id=self._request_id)
                except VideoAlgorithmCacheLeaseLostError as error:
                    self._lost = error
                    return
                except VideoAlgorithmCacheLeaseUnavailableError as error:
                    # 一過性障害ではowner喪失と断定せず、最小TTL 301秒に対して最大5秒で再試行。
                    self._unavailable = error
                    interval = self._cache.lease_retry_seconds
                else:
                    self._unavailable = None
                    interval = self._cache.lease_heartbeat_seconds

    def assert_owned(self) -> None:
        """課金前後の境界で同期renewし、transient heartbeat失敗から回復確認する。"""

        with self._renew_lock:
            if self._lost is not None:
                raise RuntimeError(
                    "VIDEO_ALGORITHM_LEASE_LOST: 処理中リースの所有権を失ったため、"
                    "追加の課金処理を中止しました"
                ) from self._lost
            last_unavailable: VideoAlgorithmCacheLeaseUnavailableError | None = None
            for attempt in range(3):
                try:
                    self._cache.renew_lease(self._lease, request_id=self._request_id)
                except VideoAlgorithmCacheLeaseLostError as error:
                    self._lost = error
                    raise RuntimeError(
                        "VIDEO_ALGORITHM_LEASE_LOST: 処理中リースの所有権を失ったため、"
                        "追加の課金処理を中止しました"
                    ) from error
                except VideoAlgorithmCacheLeaseUnavailableError as error:
                    self._unavailable = error
                    last_unavailable = error
                    if attempt < 2 and self._stop.wait(self._cache.lease_retry_seconds):
                        break
                else:
                    self._unavailable = None
                    return
            raise RuntimeError(
                "VIDEO_ALGORITHM_CACHE_UNAVAILABLE: 処理中リースを確認できないため、"
                "追加の課金処理を中止しました"
            ) from last_unavailable

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def _default_report_dir() -> str:
    """Return the writable runtime directory for generated VSEO artifacts.

    Fargate runs with a read-only root filesystem, so the historical
    ``<cwd>/.local_out`` default silently disabled reports and proposal files.
    ``/tmp`` is the task's bounded writable volume; operators may override the
    directory explicitly for local development and tests.
    """

    configured = os.environ.get("TEAMAGENT_VSEO_REPORT_DIR", "").strip()
    if configured:
        return configured
    return os.path.join(tempfile.gettempdir(), "teamagent", "vseo_reports")


def _request_report_dir(request_id: str) -> str:
    safe_request_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", request_id).strip("-")[:64] or "request"
    return os.path.join(_default_report_dir(), f"{safe_request_id}-{uuid.uuid4().hex[:12]}")


def _reset_field(data: Any, loc: tuple[int | str, ...]) -> bool:
    """ValidationError の loc が指すフィールドを除去し default に戻す。成功で True。

    ネスト dict / list 要素の双方に対応。leaf が dict キーなら削除（→ schema default）、
    list の添字なら不正な 1 要素だけ除去する。捏造はせず「未取得」に倒すだけ。
    """
    if not loc:
        return False
    cur = data
    for key in loc[:-1]:
        if isinstance(cur, dict) and isinstance(key, str) and key in cur:
            cur = cur[key]
        elif isinstance(cur, list) and isinstance(key, int) and 0 <= key < len(cur):
            cur = cur[key]
        else:
            return False
    leaf = loc[-1]
    if isinstance(cur, dict) and isinstance(leaf, str) and leaf in cur:
        del cur[leaf]
        return True
    if isinstance(cur, list) and isinstance(leaf, int) and 0 <= leaf < len(cur):
        cur.pop(leaf)
        return True
    return False


def parse_analysis(text: str) -> VideoVSEOAnalysis | None:
    """Gemini 出力（所見＋JSONブロック）を VideoVSEOAnalysis にパース（防御的・寛容）。

    1 フィールドの enum ズレ等で動画を丸ごと失わないよう、ValidationError の原因
    フィールドだけを default に戻して再検証する（最大 _MAX_FIELD_RESETS 回）。
    初回失敗は loc/type を診断ログに残し（次にどのフィールドか判明させる）、
    救済したフィールドも必ずログに出す（サイレント補正にしない）。
    """
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    reset: list[str] = []
    while True:
        try:
            obj = VideoVSEOAnalysis.model_validate(data)
        except ValidationError as e:
            errs = e.errors()
            if not reset:  # 初回のみ全エラーを診断ログへ（PII 回避で loc/type のみ）
                logger.warning(
                    "video_algorithm_parse_validation_failed",
                    error_count=len(errs),
                    errors=[
                        {"loc": ".".join(map(str, x["loc"])), "type": x["type"]} for x in errs[:8]
                    ],
                )
            loc = errs[0]["loc"]
            if len(reset) >= _MAX_FIELD_RESETS or not _reset_field(data, loc):
                logger.warning("video_algorithm_parse_unrecovered", loc=".".join(map(str, loc)))
                return None
            reset.append(".".join(map(str, loc)))
            continue
        if reset:
            logger.info("video_algorithm_parse_recovered", reset_fields=reset)
        return obj


def _sniff_image_mime(data: bytes) -> str:
    """画像 bytes のマジックバイトから mime を判定（cover-only 分析で Gemini に正しく渡す）。

    TikTok の cover は jpeg のことが多いが webp/heic もあり得る。誤った mime を渡すと
    Gemini が拒否/誤読するため、判定不能時のみ image/jpeg にフォールバックする。
    """
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] in (b"ftypheic", b"ftypheif", b"ftypmif1"):
        return "image/heic"
    return "image/jpeg"


@register
class VideoAlgorithmSkill(BaseSkill[VideoAlgorithmInput, VideoAlgorithmOutput]):
    """検索上位動画を分析し『なぜ上位か』を読み解く Skill。"""

    name: ClassVar[str] = "video_algorithm"
    description: ClassVar[str] = (
        "検索KWの上位動画を取得し、各動画をGeminiで時刻付き構造分析（テロップ/ブランド認識/"
        "フック/CTA）→ 上位の共通点と1本ずつの構成を読み解き、HTMLレポートとスライドを生成"
    )
    input_schema: ClassVar[type[BaseModel]] = VideoAlgorithmInput
    output_schema: ClassVar[type[BaseModel]] = VideoAlgorithmOutput

    def __init__(
        self,
        gemini: GeminiClient | None = None,
        *,
        prompt_version: str = DEFAULT_PROMPT_VERSION,
        searcher: Searcher | None = None,
        downloader: Downloader | None = None,
        proxy: Proxy | None = None,
        report_dir: str | None = None,
        max_workers: int | None = None,
        overfetch_buffer: int = _OVERFETCH_BUFFER,
        publisher: Callable[..., str | None] | None = None,
        result_cache: VideoAlgorithmResultCache | None = None,
        apify_fallback: Any | None = None,
        synthesis_version: str | None = None,
        cover_fetcher: CoverFetcher | None = None,
        cover_settings: CoverSettings | None = None,
    ) -> None:
        self._gemini = gemini
        self._prompt_version = prompt_version
        # 統合（横断シンセシス）の版。None なら env VIDEO_ALGO_SYNTHESIS_VERSION（既定 v3・v2 で
        # 旧版へ戻す）。MCP（factory）と Slack（slack_bot）はどちらも引数なしで作るので env が効く。
        self._synthesis_version = synthesis_version or synthesis_version_from_env()
        self._searcher = searcher
        self._downloader = downloader
        self._proxy = proxy
        self._report_dir = report_dir
        self._max_workers = _max_workers_from_env() if max_workers is None else max_workers
        self._overfetch_buffer = overfetch_buffer
        self._publisher = publisher
        self._result_cache = result_cache
        # 二段構え（USE_TIKTOK_APIFY_FALLBACK=1）で使う ApifyClient。None なら env から生成。
        self._apify_fallback = apify_fallback
        # サムネ（一覧の表紙）の読み取り。取得の注入口（テスト）と設定（None なら run ごとに env）。
        self._cover_fetcher = cover_fetcher
        self._cover_settings = cover_settings

    # --- 依存の遅延解決（テスト差し替え可） ---
    def _client(self) -> GeminiClient:
        if self._gemini is None:
            self._gemini = GeminiClient.from_env()
        return self._gemini

    def _configured_model_id(self) -> str:
        """キャッシュキー用の実行予定 model id（認証クライアント生成前に確定）。"""

        model_id = getattr(self._gemini, "model_id", None)
        if isinstance(model_id, str) and model_id.strip():
            return model_id.strip()
        from teamagent.adapters.gemini_client import DEFAULT_MODEL_ID

        return os.environ.get("GEMINI_MODEL_ID", DEFAULT_MODEL_ID).strip() or DEFAULT_MODEL_ID

    @staticmethod
    def _reserve_quota(ctx: SkillContext, count: int, *, allow_partial: bool) -> int:
        """Gemini 分析を開始する batch 本数を事前確保し、**実際に確保できた本数**を返す。

        事後消費では並行リクエストが上限をすり抜けるため、失敗試行も含めて開始前に確保する。
        取得失敗後の cover-only 縮退も同じ「分析試行」1本として数える。

        断らずに進める方針:
          - `allow_partial=True`（＝すでに何本か分析できている波）で残数が足りなければ、
            **残数に丸めて確保**し、それも無理なら 0 を返して打ち切る（成果は捨てない）。
          - `allow_partial=False`（1波目＝まだ 1 本も分析していない）で足りなければ、
            残数と選択肢を書いた文面で raise する（残 0 のときだけ「上限に達しました」）。
        既定OFF（VIDEO_QUOTA_ENABLED 未設定）なら完全 no-op で count をそのまま返す。
        """

        from teamagent.adapters.quota_store import VideoQuotaStore, quota_block_message

        if count <= 0 or not VideoQuotaStore.enabled():
            return max(0, count)
        email = str(ctx.metadata.get("user_email", "") or "").strip().lower()
        if not email:
            # quota ON なのに主体不明を allowed no-op にするとコスト上限を迂回できる。
            # そのため、quota が有効な場合だけ fail-closed にする。
            raise RuntimeError(
                "VIDEO_QUOTA_IDENTITY_REQUIRED: 動画分析クォータの利用者メールを解決できません"
            )
        store = VideoQuotaStore()
        result = store.try_consume(email, count, request_id=ctx.request_id)
        if result.allowed:
            return count
        remaining = result.remaining
        if allow_partial and remaining > 0:
            # 残数に丸めてもう一度だけ確保を試す（並行消費で外れたらそこで打ち切る）。
            rounded = store.try_consume(email, remaining, request_id=ctx.request_id)
            if rounded.allowed:
                logger.info(
                    "video_algorithm_quota_rounded",
                    request_id=ctx.request_id,
                    requested=count,
                    reserved=remaining,
                    limit=result.limit,
                )
                return remaining
        if allow_partial:
            logger.info(
                "video_algorithm_quota_truncated",
                request_id=ctx.request_id,
                requested=count,
                remaining=remaining,
                limit=result.limit,
            )
            return 0
        raise RuntimeError(quota_block_message(result))

    def _posts_to_metas(self, posts: list[dict[str, Any]]) -> list[VideoMeta]:
        """tiktok_acquire の posts.normalized.json item を VideoMeta へ写像（S3委譲経路）。"""
        metas: list[VideoMeta] = []
        for p in posts:
            metas.append(
                VideoMeta(
                    rank=int(p.get("rank_display", 0) or 0),
                    url=str(p.get("url", "") or ""),
                    author=str(p.get("account_id") or p.get("account_name") or ""),
                    follower_count=int(p.get("followers", 0) or 0),
                    desc=str(p.get("title", "") or ""),
                    play_count=int(p.get("plays", 0) or 0),
                    digg_count=int(p.get("likes", 0) or 0),
                    comment_count=int(p.get("comments", 0) or 0),
                    share_count=int(p.get("shares", 0) or 0),
                    collect_count=int(p.get("saves", 0) or 0),
                    # eg_rate は既に百分率ポイント。/100 は二重換算になる。
                    engagement_rate=float(p.get("eg_rate", 0.0) or 0.0),
                    cover_url=None,
                    duration_sec=float(p.get("duration", 0.0) or 0.0),
                    # 取得済みの投稿日時・ハッシュタグ・音源名を捨てずに写す。
                    create_time=_int_or_zero(p.get("create_time")),
                    hashtags=_str_list(p.get("hashtags")),
                    music_title=str(p.get("music_title", "") or ""),
                )
            )
        return metas

    def _search(
        self, query: str, n: int, request_id: str, searcher: Searcher | None = None
    ) -> list[VideoMeta]:
        s = searcher or self._searcher
        if s is not None:
            return s(query, n, request_id)
        from teamagent.adapters.tiktok_scraper import search_tiktok

        res = search_tiktok(query, search_type="keyword", max_videos=n, request_id=request_id)
        metas: list[VideoMeta] = []
        for i, v in enumerate(res.videos):
            author = getattr(v, "author", None)
            author_name = (
                getattr(author, "unique_id", None) or getattr(author, "nickname", "") or ""
            )
            metas.append(
                VideoMeta(
                    rank=i + 1,
                    url=getattr(v, "url", ""),
                    author=str(author_name),
                    follower_count=int(getattr(author, "follower_count", 0) or 0),
                    desc=getattr(v, "desc", "") or "",
                    play_count=getattr(v, "play_count", 0) or 0,
                    digg_count=getattr(v, "digg_count", 0) or 0,
                    comment_count=getattr(v, "comment_count", 0) or 0,
                    share_count=getattr(v, "share_count", 0) or 0,
                    collect_count=getattr(v, "collect_count", 0) or 0,
                    # scraper は比率（0.029）を返すため、百分率ポイントへ揃える。
                    engagement_rate=float(getattr(v, "engagement_rate", 0.0) or 0.0) * 100.0,
                    cover_url=getattr(v, "cover_url", None),
                    duration_sec=float(getattr(v, "duration", 0.0) or 0.0),
                    # 取得済みの投稿日時・ハッシュタグ・音源名を捨てずに写す。
                    create_time=_int_or_zero(getattr(v, "create_time", 0)),
                    hashtags=_str_list(getattr(v, "hashtags", ())),
                    music_title=str(getattr(v, "music_title", "") or ""),
                )
            )
        return metas

    def _download(
        self, url: str, request_id: str, downloader: Downloader | None = None
    ) -> tuple[bytes, str]:
        d = downloader or self._downloader
        if d is not None:
            return d(url)
        from teamagent.adapters.media_job import MediaJobClient

        if MediaJobClient.is_configured():
            import hashlib

            fingerprint = hashlib.sha256(url.encode("utf-8")).hexdigest()
            return MediaJobClient().acquire_video(
                url,
                request_fingerprint=f"{request_id}:acquire:{fingerprint}",
            )
        # 3層DLチェーン（ブラウザ内DL→yt-dlp→…）。全滅時は _analyze_one が cover-only へ縮退。
        from teamagent.adapters.video_download import download_video_chained

        return download_video_chained(url, request_id=request_id)

    def _shrink(self, data: bytes, mime: str, request_id: str) -> tuple[bytes, str]:
        if self._proxy is not None:
            return self._proxy(data, mime)
        from teamagent.adapters.media_job import MediaJobClient

        if MediaJobClient.is_configured():
            import hashlib

            fingerprint = hashlib.sha256(data).hexdigest()
            return MediaJobClient().proxy_video(
                data,
                mime,
                request_fingerprint=f"{request_id}:proxy:{fingerprint}",
            )
        from teamagent.adapters.video_proxy import ensure_under_limit

        return ensure_under_limit(data, mime, request_id=request_id)

    # --- 1動画の分析（download→proxy→gemini→parse） ---
    def _analyze_one(
        self,
        meta: VideoMeta,
        *,
        query: str,
        client_name: str | None,
        system: str,
        request_id: str,
        downloader: Downloader | None = None,
        user_email: str = "",
        apify_budget: _ApifyFallbackBudget | None = None,
        media_extras: bool = True,
        scene_frames: bool = False,
        frame_width: int = 320,
        preview: bool = True,
        strict_extras: bool = True,
        opening_frame: bool = False,
        prefetched: Prefetched | None = None,
    ) -> AnalyzedVideo:
        """1 本を取得→圧縮→Gemini で分析する。

        ``media_extras=False`` はレポート用の付属物（実フレーム・サムネ色・Web プレビュー動画）を
        作らない（media job を呼ばない）。分析の中身（Gemini の JSON）は同じ。
        以下は検索上位チェックの 2 段目（場面ごとの構成表）が使う。既定は run と同じ動き:
        - ``scene_frames=True``: フレームを場面ごと（場面の中央の秒・最大 12 コマ）に抜く。
        - ``opening_frame=True``: 場面ごとのコマに冒頭（0.8 秒）のコマを足す（構成分解の左の
          「冒頭のコマ」と、表紙を取れないときの代用に使う）。
        - ``frame_width``: フレームの幅（px）。構成表の小さいコマは 180 で足りる。
        - ``preview=False``: Web プレビュー動画（1 本最大 6MB の data URI）を作らない。
        - ``strict_extras=False``: フレーム・サムネの media job が失敗しても、分析（課金済み）を
          捨てずに付属物なしで返す。
        - ``prefetched``: 表紙の読み取りが先に取った画像を待つ関数（表示・色に使い回す）。
        """
        acquired_via = ""
        try:
            with _stage("download", request_id, meta.rank):
                data, mime = retry_long_job_once(
                    lambda: self._download(meta.url, request_id, downloader=downloader)
                )
        except Exception as e:  # 取得失敗 → 二段構え（opt-in）→ それでも無理ならサムネ縮退
            logger.warning("video_algorithm_fetch_failed", rank=meta.rank, error=type(e).__name__)
            recovered = (
                self._apify_fallback_fetch(
                    meta, request_id=request_id, user_email=user_email, budget=apify_budget
                )
                if apify_fallback_enabled()
                else None
            )
            if recovered is None:
                return self._cover_only_analysis(
                    meta,
                    query=query,
                    system=system,
                    request_id=request_id,
                    cause=type(e).__name__,
                    media_extras=media_extras,
                    strict_extras=strict_extras,
                    prefetched=prefetched,
                )
            data, mime = recovered
            acquired_via = ACQUIRED_VIA_APIFY
        try:
            with _stage("shrink", request_id, meta.rank):
                data, mime = self._shrink(data, mime, request_id)
        except Exception as e:  # 圧縮失敗 → サムネのみの軽量分析へ縮退（全滅回避）
            logger.warning(
                "video_algorithm_fetch_failed",
                rank=meta.rank,
                error=type(e).__name__,
                stage="shrink",
            )
            return self._cover_only_analysis(
                meta,
                query=query,
                system=system,
                request_id=request_id,
                cause=type(e).__name__,
                media_extras=media_extras,
                strict_extras=strict_extras,
                prefetched=prefetched,
            )

        user_prompt = (
            f"# 検索KW: {query}\n"
            f"# この動画の表示順位: {meta.rank}位\n"
            + (f"# クライアント名（competitor判定用）: {client_name}\n" if client_name else "")
            + f"# キャプション本文: {meta.desc}\n\n"
            "この動画を実際に視聴し、システム指示のJSON形式で VSEO 構造分析を出力してください。"
        )
        try:
            with _stage("gemini", request_id, meta.rank):
                resp = self._client().analyze_video_bytes(
                    data=data,
                    mime_type=mime,
                    prompt=user_prompt,
                    request_id=request_id,
                    system=system,
                )
        except Exception as e:
            logger.warning("video_algorithm_gemini_failed", rank=meta.rank, error=type(e).__name__)
            return AnalyzedVideo(meta=meta, error=f"分析失敗: {type(e).__name__}")

        analysis = parse_analysis(resp.text)
        frames: list[FrameShot] = []
        if analysis is not None and media_extras:
            try:
                frames = self._extract_frames(
                    analysis,
                    data,
                    mime,
                    rank=meta.rank,
                    request_id=request_id,
                    scene_frames=scene_frames,
                    width=frame_width,
                    duration_sec=meta.duration_sec,
                    opening_frame=opening_frame,
                )
            except Exception as exc:
                if strict_extras:
                    raise
                logger.warning(
                    "video_algorithm_frames_skipped", rank=meta.rank, error=type(exc).__name__
                )
                frames = []
        # サムネ色（検索一覧タイル）: cover_url を取得、失敗時は先頭フレームを流用
        cover_uri: str = ""
        thumb: ThumbColor | None = None
        cover_source: CoverSource = ""
        if media_extras:
            with _stage("thumbnail", request_id, meta.rank):
                # 表紙の URL を先に使い、取れなければ冒頭（0.8 秒）のコマで代える（出どころを
                # 残す）。場面ごとの小さいコマ（先頭は表紙と限らない）では代えない。
                opening = (
                    []
                    if scene_frames and not opening_frame
                    else [f for f in frames if f.sec <= OPENING_FRAME_MAX_SEC][:1]
                )
                try:
                    cover_uri, thumb, cover_source = self._build_thumb(
                        meta.cover_url, opening, request_id, prefetched=prefetched
                    )
                except Exception as exc:
                    if strict_extras:
                        raise
                    logger.warning(
                        "video_algorithm_thumbnail_skipped",
                        rank=meta.rank,
                        error=type(exc).__name__,
                    )
        # タイムラインで実再生する軽量Webプレビュー動画（~480p・graceful。失敗時は静止フレーム）
        video_uri = ""
        if analysis is not None and media_extras and preview:
            video_uri = self._build_preview(data, mime, rank=meta.rank, request_id=request_id)
        return AnalyzedVideo(
            meta=meta,
            analysis=analysis,
            frames=frames,
            video_data_uri=video_uri,
            cover_data_uri=cover_uri,
            cover_source=cover_source,
            thumb=thumb,
            error=None if analysis else "JSONパース失敗",
            cost_usd=resp.cost_usd,
            model_id=getattr(resp, "model_id", None),
            acquired_via=acquired_via,
        )

    def _extract_frames(
        self,
        analysis: VideoVSEOAnalysis,
        data: bytes,
        mime: str,
        *,
        rank: int,
        request_id: str,
        scene_frames: bool,
        width: int,
        duration_sec: float = 0.0,
        opening_frame: bool = False,
    ) -> list[FrameShot]:
        """proxy 後の検証済み bytes を使い回して実フレームを抽出する。

        ``duration_sec`` は検索結果の実尺（場面ごとのコマを尺の内側に収めるのに使う）。
        """
        with _stage("frames", request_id, rank):
            from teamagent.skills.video_algorithm.frames import (
                MAX_SCENE_FRAMES,
                extract_frames,
                pick_timecodes,
                scene_timecodes,
            )

            # media の FrameOperation は 1 回 12 コマまで（contracts.FrameOperation）。冒頭のコマを
            # 足すときは場面を 11 までにして、合計を 12 に収める（13 にするとジョブごと失敗する）。
            scene_limit = MAX_SCENE_FRAMES - 1 if opening_frame else MAX_SCENE_FRAMES
            tcs = (
                scene_timecodes(analysis, duration_sec=duration_sec, max_frames=scene_limit)
                if scene_frames
                else pick_timecodes(analysis, max_frames=6)
            )
            if scene_frames and opening_frame and tcs:
                # 冒頭のコマ（0.8 秒）を足す（最初の場面の中央と 0.5 秒以内なら足さない）。
                if all(abs(s - OPENING_FRAME_SEC) > 0.5 for s, _c in tcs):
                    tcs = [(OPENING_FRAME_SEC, "冒頭"), *tcs]
            if not tcs:
                return []
            cap_by_sec = {round(s, 1): c for s, c in tcs}
            from teamagent.adapters.media_job import MediaJobClient

            if MediaJobClient.is_configured():
                import base64
                import hashlib

                fingerprint = hashlib.sha256(data).hexdigest()
                try:
                    media_shots = MediaJobClient().extract_frames(
                        data,
                        mime,
                        [s for s, _ in tcs],
                        width=width,
                        request_fingerprint=f"{request_id}:frames:{fingerprint}",
                    )
                    shots = [
                        (
                            second,
                            "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii"),
                        )
                        for second, image in media_shots
                    ]
                except Exception as exc:
                    logger.warning(
                        "video_algorithm_frames_failed",
                        rank=rank,
                        error=type(exc).__name__,
                    )
                    raise RuntimeError("MEDIA_FRAME_JOB_FAILED") from exc
            elif MediaJobClient.local_runtime_enabled():
                shots = extract_frames(
                    data, mime, [s for s, _ in tcs], width=width, request_id=request_id
                )
            else:
                MediaJobClient.require_configured()
                raise AssertionError("unreachable")
            return [
                FrameShot(sec=s, caption=cap_by_sec.get(round(s, 1), f"{s:.0f}s"), data_uri=uri)
                for s, uri in shots
            ]

    def _build_preview(self, data: bytes, mime: str, *, rank: int, request_id: str) -> str:
        """タイムラインで実再生する軽量 Web プレビュー動画（~480p）の data URI。"""
        with _stage("preview", request_id, rank):
            from teamagent.adapters.media_job import MediaJobClient

            if MediaJobClient.is_configured():
                import base64
                import hashlib

                fingerprint = hashlib.sha256(data).hexdigest()
                try:
                    preview, _preview_mime = MediaJobClient().proxy_video(
                        data,
                        mime,
                        request_fingerprint=f"{request_id}:preview:{fingerprint}",
                        limit_bytes=6 * 1024 * 1024,
                        preview=True,
                    )
                    return "data:video/mp4;base64," + base64.b64encode(preview).decode("ascii")
                except Exception as exc:
                    logger.warning(
                        "video_algorithm_preview_failed",
                        rank=rank,
                        error=type(exc).__name__,
                    )
                    raise RuntimeError("MEDIA_PREVIEW_JOB_FAILED") from exc
            elif MediaJobClient.local_runtime_enabled():
                from teamagent.adapters.video_proxy import make_web_preview

                return make_web_preview(data, mime, request_id=request_id)
            else:
                MediaJobClient.require_configured()
                raise AssertionError("unreachable")

    def _apify_fallback_fetch(
        self,
        meta: VideoMeta,
        *,
        request_id: str,
        user_email: str,
        budget: _ApifyFallbackBudget | None = None,
    ) -> tuple[bytes, str] | None:
        """二段構え: DL経路の失敗分を mcp 側 Apify（clockworks）で補完する（失敗は None）。

        共有ヘルパー ``fill_missing_videos`` を 1 本分だけ呼ぶ。request 単位の ``budget``
        （集約本数上限 TIKTOK_APIFY_FALLBACK_MAX_VIDEOS と壁時計 VIDEO_ALGORITHM_APIFY_WALLCLOCK_S）
        から枠を取れない時は Apify を呼ばず None（cover-only 縮退へ）。
        job_id には request_id が入るため、S3 の既存再利用と試行済みマーカーは **同一 request 内**
        でだけ効く（別 request は毎回 Apify を叩き、費用は request 単位の予算で頭打ち）。
        media job 基盤がある環境では取得物を ``media-jobs/<job>/input/apify-<key>.mp4`` へ置いて
        検査を通し、無い環境（ローカル runtime）では bytes をそのまま使う。
        """
        from teamagent.adapters.media_job import MediaJobClient

        needed_s = fallback_deadline_s()
        deadline_s = needed_s
        if budget is not None:
            allowed, reason = budget.try_take(needed_s + _APIFY_S3_MARGIN_S)
            if not allowed:
                logger.info("video_algorithm_apify_fallback_skipped", rank=meta.rank, reason=reason)
                return None
            deadline_s = max(1, min(needed_s, int(budget.remaining_s()) - _APIFY_S3_MARGIN_S))

        media_client: Any | None = None
        try:
            media_client = MediaJobClient() if MediaJobClient.is_configured() else None
            fingerprint = hashlib.sha256(meta.url.encode("utf-8")).hexdigest()
            outcome = fill_missing_videos(
                fallback_job_id(f"{request_id}:apify-fallback:{fingerprint}"),
                {fingerprint[:16]: meta.url},
                media_client=media_client,
                apify=self._apify_fallback,
                deadline_s=deadline_s,
                request_id=request_id,
                user_email=user_email,
                max_videos=1,
                keep_body=True,
            )
        except Exception as exc:
            logger.warning(
                "video_algorithm_apify_fallback_failed", rank=meta.rank, error=type(exc).__name__
            )
            return None
        for note in outcome.warnings:
            logger.info("video_algorithm_apify_fallback_note", rank=meta.rank, note=note)
        if not outcome.videos:
            return None
        staged = outcome.videos[0]
        body = staged.body
        if body is None and staged.ref is not None and media_client is not None:
            try:
                body = media_client.download(staged.ref, deadline_epoch_s=int(time.time()) + 60)
            except Exception as exc:
                logger.warning(
                    "video_algorithm_apify_fallback_download_failed",
                    rank=meta.rank,
                    error=type(exc).__name__,
                )
                return None
        if not body:
            return None
        logger.info(
            "video_algorithm_fetch_recovered_via_apify", rank=meta.rank, reused=staged.reused
        )
        return body, staged.content_type

    def _cover_only_analysis(
        self,
        meta: VideoMeta,
        *,
        query: str,
        system: str,
        request_id: str,
        cause: str,
        media_extras: bool = True,
        strict_extras: bool = True,
        prefetched: Prefetched | None = None,
    ) -> AnalyzedVideo:
        """動画DL全滅時の縮退: cover(サムネ静止画)1枚だけを Gemini に渡す軽量分析。

        cover も取れなければ従来どおり error カードに倒す（捏造しない）。静止画なので秒系
        フィールド（テロップ遷移秒/CTA秒/シーン分割）は観測不可＝プロンプトで空/既定に倒させる。
        cover は小サイズ画像なので _shrink（動画 transcode 経路）は通さず素通しする。
        """
        from teamagent.adapters.media_job import MediaJobClient

        cover: bytes | None
        if MediaJobClient.is_configured() and meta.cover_url:
            try:
                cover, _metadata = MediaJobClient().make_thumbnail_from_url(
                    meta.cover_url,
                    request_fingerprint=f"{request_id}:cover-analysis:{meta.rank}",
                    width=1280,
                )
            except Exception as exc:
                raise RuntimeError("MEDIA_COVER_JOB_FAILED") from exc
        elif MediaJobClient.local_runtime_enabled():
            from teamagent.skills.video_algorithm.thumbnails import fetch_cover

            cover = fetch_cover(meta.cover_url, request_id=request_id)
        else:
            MediaJobClient.require_configured()
            raise AssertionError("unreachable")
        if not cover:
            return AnalyzedVideo(meta=meta, error=f"取得失敗: {cause}")
        user_prompt = (
            f"# 検索KW: {query}\n"
            f"# この動画の表示順位: {meta.rank}位\n"
            f"# キャプション本文: {meta.desc}\n\n"
            "注記: 動画本体を取得できなかったため、入力は**サムネイル静止画1枚**です。"
            "秒単位のタイムライン・テロップ遷移・CTA出現秒・シーン分割は観測できません。"
            "静止画から読み取れる範囲（被写体・色/トーン・焼き込みテキスト・訴求の方向性）"
            "のみを、システム指示のJSON形式で出力してください。観測できない項目は"
            "推測で埋めず、空配列/既定値のままにしてください。"
        )
        try:
            resp = self._client().analyze_video_bytes(
                data=cover,
                mime_type=_sniff_image_mime(cover),
                prompt=user_prompt,
                request_id=request_id,
                system=system,
            )
        except Exception as e:
            logger.warning("video_algorithm_cover_failed", rank=meta.rank, error=type(e).__name__)
            return AnalyzedVideo(meta=meta, error=f"取得失敗: {cause}")
        analysis = parse_analysis(resp.text)
        cover_uri: str = ""
        thumb: ThumbColor | None = None
        cover_source: CoverSource = ""
        if media_extras:
            try:
                cover_uri, thumb, cover_source = self._build_thumb(
                    meta.cover_url, [], request_id, prefetched=prefetched
                )
            except Exception as exc:
                if strict_extras:
                    raise
                logger.warning(
                    "video_algorithm_thumbnail_skipped", rank=meta.rank, error=type(exc).__name__
                )
        return AnalyzedVideo(
            meta=meta,
            analysis=analysis,
            cover_data_uri=cover_uri,
            cover_source=cover_source,
            thumb=thumb,
            error="動画取得失敗・サムネのみ軽量分析" if analysis else f"取得失敗: {cause}",
            cost_usd=resp.cost_usd,
            model_id=getattr(resp, "model_id", None),
        )

    def _build_thumb(
        self,
        cover_url: str | None,
        frames: list[FrameShot],
        request_id: str,
        *,
        prefetched: Prefetched | None = None,
    ) -> tuple[str, ThumbColor | None, CoverSource]:
        """サムネ（表紙）とその色。表紙の URL を先に使い、取れなければ先頭のコマで代える。

        戻り値の 3 つ目は出どころ（"cover"＝表紙・"frame"＝コマで代用・""＝無し）。描画は
        代用のとき「表紙」と呼ばない（本番では 0.8 秒のコマを表紙として色を比べていた）。
        表紙もコマも作れなければ、media job の失敗として例外を上げる（呼び出し側の strict に従う）。

        ``prefetched`` があれば、表紙の読み取りが取った画像（幅 540・色つき）を待って使い、
        表紙の URL をもう一度取りに行かない（1 本 1 回の取得。失敗・時間切れならコマで代える）。
        """
        from teamagent.adapters.media_job import MediaJobClient
        from teamagent.skills.video_algorithm.thumbnails import (
            analyze_cover,
            build_thumb,
        )

        if prefetched is not None and cover_url:
            got = prefetched()
            built = self._thumb_from_image(got, request_id) if got is not None else None
            if built is not None:
                return built[0], built[1], "cover"
            cover_url = None

        head: bytes | None = None
        if frames and frames[0].data_uri.startswith("data:image/jpeg;base64,"):
            import base64

            try:
                head = base64.b64decode(frames[0].data_uri.split(",", 1)[1], validate=True)
            except Exception:
                head = None

        if MediaJobClient.is_configured():
            import base64
            import hashlib

            failure: Exception | None = None
            if cover_url:
                fingerprint = hashlib.sha256(cover_url.encode("utf-8")).hexdigest()
                try:
                    image, metadata = MediaJobClient().make_thumbnail_from_url(
                        cover_url,
                        request_fingerprint=f"{request_id}:thumbnail-url:{fingerprint}",
                        width=240,
                    )
                    return (
                        "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii"),
                        ThumbColor.model_validate(metadata),
                        "cover",
                    )
                except Exception as exc:
                    failure = exc
                    logger.warning(
                        "video_algorithm_cover_thumbnail_failed", error=type(exc).__name__
                    )
            if head is not None:
                fingerprint = hashlib.sha256(head).hexdigest()
                try:
                    image, metadata = MediaJobClient().make_thumbnail(
                        head,
                        _sniff_image_mime(head),
                        request_fingerprint=f"{request_id}:thumbnail:{fingerprint}",
                        width=240,
                    )
                except Exception as exc:
                    failure = exc
                else:
                    return (
                        "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii"),
                        ThumbColor.model_validate(metadata),
                        "frame",
                    )
            if failure is not None:
                logger.warning(
                    "video_algorithm_thumbnail_failed",
                    error=type(failure).__name__,
                )
                raise RuntimeError("MEDIA_THUMBNAIL_JOB_FAILED") from failure
            return "", None, ""

        if not MediaJobClient.local_runtime_enabled():
            MediaJobClient.require_configured()
            raise AssertionError("unreachable")
        res = build_thumb(cover_url, request_id=request_id) if cover_url else None
        if res is not None:
            return res[0], res[1], "cover"
        if head is not None:
            try:
                framed = analyze_cover(head, request_id=request_id)
            except Exception:
                framed = None
            if framed is not None:
                return framed[0], framed[1], "frame"
        return "", None, ""

    @staticmethod
    def _thumb_from_image(
        image: CoverImage, request_id: str
    ) -> tuple[str, ThumbColor | None] | None:
        """表紙の読み取りが取った画像から、表示用の画像と色を作る（取り直さない）。"""
        import base64

        if image.color is not None:  # media worker が同じ取得で色も計算した（幅 540 の JPEG）
            try:
                color: ThumbColor | None = ThumbColor.model_validate(image.color)
            except ValidationError:
                color = None
            return f"data:{image.mime};base64," + base64.b64encode(image.data).decode(
                "ascii"
            ), color
        from teamagent.adapters.media_job import MediaJobClient

        if MediaJobClient.is_configured():
            fingerprint = hashlib.sha256(image.data).hexdigest()
            data, metadata = MediaJobClient().make_thumbnail(
                image.data,
                image.mime,
                request_fingerprint=f"{request_id}:thumbnail:{fingerprint}",
                width=240,
            )
            return (
                "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii"),
                ThumbColor.model_validate(metadata),
            )
        from teamagent.skills.video_algorithm.thumbnails import analyze_cover

        try:
            analyzed = analyze_cover(image.data, request_id=request_id)
        except Exception:
            analyzed = None
        if analyzed is not None:
            return analyzed
        if image.mime in ("image/jpeg", "image/png", "image/webp"):
            return f"data:{image.mime};base64," + base64.b64encode(image.data).decode("ascii"), None
        return None

    # --- サムネ（一覧の表紙）の読み取り ---
    def _cover_settings_now(self) -> CoverSettings:
        return self._cover_settings or CoverSettings.from_env()

    def _cover_settings_for(self, input: VideoAlgorithmInput) -> CoverSettings:
        """この依頼の表紙の設定。表紙を出す出力（report／slides／pptx）が無ければ読まない。

        tiktok_search の深掘り（deep.build_filmstrips）は outputs=[] でコマと冒頭フックだけを使う。
        画面に出ない表紙のために Gemini の費用・待ち・統合の入力を増やさない（キャッシュのキーも
        従来のまま）。
        """
        settings = self._cover_settings_now()
        if not input.outputs and settings.enabled:
            return replace(settings, enabled=False)
        return settings

    def _cover_fetch(
        self, request_id: str, settings: CoverSettings
    ) -> Callable[[VideoMeta], CoverImage]:
        """表紙の取得（1 本 1 回・幅 540）。

        注入があればそれ、無ければ media job（本番）かローカル。
        """
        injected = self._cover_fetcher
        if injected is not None:
            return lambda meta: image_of(injected(meta.cover_url or ""), via="injected")

        def fetch(meta: VideoMeta) -> CoverImage:
            from teamagent.adapters.media_job import MediaJobClient

            url = meta.cover_url or ""
            if MediaJobClient.is_configured():
                fingerprint = hashlib.sha256(url.encode("utf-8")).hexdigest()
                image, metadata = MediaJobClient().make_thumbnail_from_url(
                    url,
                    request_fingerprint=f"{request_id}:cover-read:{fingerprint}",
                    width=COVER_WIDTH,
                    timeout_s=int(settings.fetch_timeout_s),
                )
                return image_of(image, via="media", color=dict(metadata))
            if MediaJobClient.local_runtime_enabled():
                from teamagent.skills.video_algorithm import thumbnails

                data = thumbnails.fetch_cover(url, request_id=request_id)
                if not data:
                    raise RuntimeError("COVER_LOCAL_FETCH_FAILED")
                return image_of(data, via="local")
            MediaJobClient.require_configured()
            raise AssertionError("unreachable")

        return fetch

    def _start_cover_reader(self, request_id: str, settings: CoverSettings) -> CoverReader | None:
        """読み取りを始める（Gemini のクライアントは main で先に作る）。作れなければ読まない。"""
        try:
            gemini = self._client()
            system = load_prompt(COVER_PROMPT_SKILL, COVER_PROMPT_VERSION, "system")
            version = cover_version(settings, self._configured_model_id())
        except Exception as exc:
            logger.warning(
                "video_algorithm_cover_reader_unavailable",
                request_id=request_id,
                error=type(exc).__name__,
            )
            return None
        return CoverReader(
            gemini=gemini,
            system=system,
            fetch=self._cover_fetch(request_id, settings),
            request_id=request_id,
            settings=settings,
            version=version,
        )

    @staticmethod
    def _prefetch_for(reader: CoverReader | None, meta: VideoMeta) -> Prefetched | None:
        if reader is None or meta.rank not in reader.ranks():
            return None
        rank = meta.rank
        return lambda: reader.image_for(rank)

    # --- 外から使う薄い入口（検索上位チェックの 2 段目など・検索しない） ---
    @staticmethod
    def reserve_video_quota(ctx: SkillContext, count: int) -> int:
        """動画分析の月間上限を ``count`` 本ぶん予約し、**確保できた本数**を返す（0 もある）。

        skill.run の 2 波目以降と同じ ``allow_partial=True``（残数に丸める・足りなければ 0）。
        上限を使わない設定（VIDEO_QUOTA_ENABLED 未設定）なら ``count`` をそのまま返す。
        上限を使う設定で依頼者のメールが無いときは、run と同じく RuntimeError で止める。
        予約は返却しない（run と同じ。失敗した試行も 1 本と数える）。
        """
        return VideoAlgorithmSkill._reserve_quota(ctx, count, allow_partial=True)

    def analyze_videos(
        self,
        metas: list[VideoMeta],
        *,
        query: str,
        client_name: str | None,
        request_id: str,
        user_email: str = "",
        media_extras: bool = False,
        scene_frames: bool = False,
        frame_width: int = 320,
        preview: bool = True,
        system_addendum: str = "",
    ) -> list[AnalyzedVideo]:
        """選び済みの動画を分析して順位順で返す（検索も quota の予約もしない）。

        run と同じ部品（``_analyze_one``: 取得→圧縮→Gemini。取得できなければサムネだけの分析へ
        縮退）を、run と同じ並列数（VIDEO_ALGORITHM_MAX_WORKERS・既定 3）で回す。
        run と違い、1 本の例外（media job の失敗など）はその 1 本だけの失敗カードにして、
        ほかの動画の分析（課金済み）を捨てない。フレーム・サムネの media job の失敗は、付属物なしで
        分析を返す（``strict_extras=False``）。quota は呼び出し側が先に予約しておくこと。

        ``system_addendum`` は 1 本ずつの system プロンプト（video_algorithm の v1/v2）の末尾に足す
        指示（検索上位チェックの 2 段目が場面ごとの役割・テロップ・発話・狙いを頼むのに使う）。
        video_algorithm の run はこれを使わないので、run の出力と結果キャッシュは変わらない。
        ``scene_frames``・``frame_width``・``preview`` は ``_analyze_one`` と同じ。
        """
        if not metas:
            return []
        system = load_prompt("video_algorithm", self._prompt_version, "system")
        if system_addendum.strip():
            system = system.rstrip() + "\n\n" + system_addendum.strip() + "\n"
        apify_budget = _ApifyFallbackBudget(
            max_videos=fallback_max_videos(),
            wallclock_s=_apify_wallclock_budget_s(),
        )

        def _one(meta: VideoMeta) -> AnalyzedVideo:
            try:
                return self._analyze_one(
                    meta,
                    query=query,
                    client_name=client_name,
                    system=system,
                    request_id=request_id,
                    user_email=user_email,
                    apify_budget=apify_budget,
                    media_extras=media_extras,
                    scene_frames=scene_frames,
                    frame_width=frame_width,
                    preview=preview,
                    strict_extras=False,
                )
            except Exception as exc:  # 1 本の失敗で他の分析を捨てない
                logger.warning(
                    "video_algorithm_analyze_one_failed",
                    request_id=request_id,
                    rank=meta.rank,
                    error=type(exc).__name__,
                )
                return AnalyzedVideo(meta=meta, error=f"分析失敗: {type(exc).__name__}")

        workers = max(1, min(self._max_workers, len(metas)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(_one, metas))
        results.sort(key=lambda v: v.meta.rank)
        return results

    def run(self, input: VideoAlgorithmInput, ctx: SkillContext) -> VideoAlgorithmOutput:
        log = ctx.bind_logger(self.name)
        log.info(
            "video_algorithm_start",
            query=input.query,
            max_videos=input.max_videos,
            board_size=input.board_size,
        )

        # 300s timeout 後の同一依頼再発話を課金ゼロで返す。キャッシュヒットはクォータも消費しない。
        result_cache = self._result_cache
        if result_cache is None and VideoAlgorithmResultCache.enabled():
            result_cache = VideoAlgorithmResultCache()
        cache_key: str | None = None
        if result_cache is not None:
            requested_by = str(ctx.metadata.get("user_email") or ctx.user_id or "unknown")
            cache_key = result_cache.cache_key(
                query=input.query,
                max_videos=input.max_videos,
                prompt_version=self._prompt_version,
                model_id=self._configured_model_id(),
                board_size=input.board_size,
                outputs=input.outputs,
                kw_set=input.kw_set,
                client_name=input.client_name,
                acquire_job_id=input.acquire_job_id,
                search_volume=input.search_volume,
                requester=requested_by,
                competitors=input.competitors,
                avoid_terms=input.avoid_terms,
                # 統合の版が違えば同じ KW でも作り直す（旧版の synthesis を返さない）。
                synthesis_version=self._synthesis_version,
                # 表紙の読み取りの版（止めているときは空＝従来のキー）。
                cover_version=cover_version(
                    self._cover_settings_for(input), self._configured_model_id()
                )
                or None,
            )
            with _stage("cache_lookup", ctx.request_id):
                cached = self._read_cached_output(result_cache, cache_key, ctx)
            if cached is not None and self._cache_has_requested_artifacts(
                cached[0], cached[1], input
            ):
                return self._reuse_cached_output(
                    cached,
                    input=input,
                    ctx=ctx,
                    result_cache=result_cache,
                    cache_key=cache_key,
                    lease=None,
                )

        lease: VideoAlgorithmCacheLease | None = None
        if result_cache is not None and cache_key is not None:
            try:
                with _stage("lease", ctx.request_id):
                    lease = result_cache.acquire_lease(cache_key, request_id=ctx.request_id)
            except VideoAlgorithmCacheLeaseHeldError as error:
                # acquire 直前に元実行が core を commit した race を一度だけ再確認する。
                cached = self._read_cached_output(result_cache, cache_key, ctx)
                if cached is not None and self._cache_has_requested_artifacts(
                    cached[0], cached[1], input
                ):
                    return self._reuse_cached_output(
                        cached,
                        input=input,
                        ctx=ctx,
                        result_cache=result_cache,
                        cache_key=cache_key,
                        lease=None,
                    )
                raise RuntimeError(
                    "VIDEO_ALGORITHM_IN_PROGRESS: 同じ条件の動画分析がまだ処理中です。"
                    "元の処理が完了するまで再実行しないでください。"
                ) from error
            if lease is None:
                # cacheを有効にした実行は、排他状態が不明なまま課金処理へfail-openしない。
                raise RuntimeError(
                    "VIDEO_ALGORITHM_CACHE_UNAVAILABLE: 二重課金防止リースを確立できないため、"
                    "動画分析を開始しませんでした"
                )

        heartbeat: _LeaseHeartbeat | None = None
        if result_cache is not None and lease is not None:
            heartbeat = _LeaseHeartbeat(result_cache, lease, ctx.request_id)
            heartbeat.start()
        try:
            if result_cache is not None and cache_key is not None and lease is not None:
                # miss→lease 取得の間に完了した結果を再利用し、不要な再分析を避ける。
                cached = self._read_cached_output(result_cache, cache_key, ctx)
                if cached is not None:
                    return self._reuse_cached_output(
                        cached,
                        input=input,
                        ctx=ctx,
                        result_cache=result_cache,
                        cache_key=cache_key,
                        lease=lease,
                        assert_lease_owned=(
                            heartbeat.assert_owned if heartbeat is not None else None
                        ),
                    )
            with _stage("total", ctx.request_id):
                return self._run_uncached(
                    input,
                    ctx,
                    result_cache=result_cache,
                    cache_key=cache_key,
                    lease=lease,
                    assert_lease_owned=heartbeat.assert_owned if heartbeat is not None else None,
                )
        except VideoAlgorithmCacheLeaseLostError as error:
            raise RuntimeError(
                "VIDEO_ALGORITHM_LEASE_LOST: 処理中リースの所有権を失ったため、"
                "追加の課金処理を中止しました"
            ) from error
        except VideoAlgorithmCacheLeaseUnavailableError as error:
            raise RuntimeError(
                "VIDEO_ALGORITHM_CACHE_UNAVAILABLE: 処理中リースを確認できないため、"
                "追加の課金処理を中止しました"
            ) from error
        finally:
            if heartbeat is not None:
                heartbeat.close()
            if result_cache is not None and lease is not None:
                result_cache.release_lease(lease, request_id=ctx.request_id)

    def _read_cached_output(
        self,
        result_cache: VideoAlgorithmResultCache,
        cache_key: str,
        ctx: SkillContext,
    ) -> tuple[VideoAlgorithmOutput, CachedVideoAlgorithmResult] | None:
        payload = result_cache.get(cache_key, request_id=ctx.request_id)
        if payload is None:
            return None
        try:
            return VideoAlgorithmOutput.model_validate(payload.output), payload
        except ValidationError:
            ctx.bind_logger(self.name).warning("video_algorithm_cache_validation_failed")
            return None

    def _cache_has_requested_artifacts(
        self,
        out: VideoAlgorithmOutput,
        payload: CachedVideoAlgorithmResult,
        input: VideoAlgorithmInput,
    ) -> bool:
        if payload.stage != "complete":
            return False
        # 発行先が無いローカル/テスト環境では None が正しい完了状態。
        if self._publisher is None and not os.environ.get("VSEO_REPORT_BUCKET"):
            return True
        required = {
            "report": out.report_url,
            "slides": out.slides_url,
            "pptx": out.pptx_url,
        }
        return all(required[kind] for kind in input.outputs)

    @staticmethod
    def _put_cached_result(
        result_cache: VideoAlgorithmResultCache,
        cache_key: str,
        *,
        output: dict[str, Any],
        stage: Literal["paid_core", "complete"],
        lease: VideoAlgorithmCacheLease,
        request_id: str,
        assert_lease_owned: Callable[[], None] | None,
        failure_message: str,
    ) -> None:
        """lease保持中の一過性result I/Oを再試行し、課金済みcoreの取りこぼしを防ぐ。"""

        for attempt in range(3):
            try:
                # heartbeatのlock下で同期renewするため、background CASとの自己競合も避ける。
                if assert_lease_owned is not None:
                    assert_lease_owned()
                else:
                    result_cache.assert_lease_owned(lease, request_id=request_id)
                committed = result_cache.put(
                    cache_key,
                    output=output,
                    stage=stage,
                    lease=lease,
                    request_id=request_id,
                )
            except VideoAlgorithmCacheLeaseUnavailableError:
                if attempt < 2:
                    # Wait between attempts. Retrying within milliseconds hits the
                    # same transient condition three times and discards a run that
                    # has already been billed.
                    time.sleep(result_cache.lease_retry_seconds)
                    continue
                raise
            if not committed:
                raise RuntimeError(failure_message)
            return
        raise AssertionError("unreachable")

    def _reuse_cached_output(
        self,
        cached: tuple[VideoAlgorithmOutput, CachedVideoAlgorithmResult],
        *,
        input: VideoAlgorithmInput,
        ctx: SkillContext,
        result_cache: VideoAlgorithmResultCache,
        cache_key: str,
        lease: VideoAlgorithmCacheLease | None,
        assert_lease_owned: Callable[[], None] | None = None,
    ) -> VideoAlgorithmOutput:
        out, payload = cached
        if not self._cache_has_requested_artifacts(out, payload, input):
            if lease is None:
                raise RuntimeError("VIDEO_ALGORITHM_IN_PROGRESS: 成果物を別リクエストが生成中です")
            return self._finalize_cached_output(
                out,
                input=input,
                ctx=ctx,
                result_cache=result_cache,
                cache_key=cache_key,
                lease=lease,
                assert_lease_owned=assert_lease_owned,
            )
        backfilled = sum(
            1 for video in out.videos if video.analysis and video.meta.rank > input.max_videos
        )
        # 名簿・避けたい訴求はキャッシュキーに入る（同じ値の依頼だけが再利用する）。
        # echo は今回の入力の値にする。
        for key, value in _echo_fields(input).items():
            setattr(out, key, value)
        out.total_cost_usd = 0.0
        out.slack_summary = self._slack_summary(out, backfilled)
        ctx.bind_logger(self.name).info(
            "video_algorithm_cache_return",
            requested=input.max_videos,
            board_size=input.board_size,
            stage=payload.stage,
        )
        return out

    def _run_uncached(
        self,
        input: VideoAlgorithmInput,
        ctx: SkillContext,
        *,
        result_cache: VideoAlgorithmResultCache | None,
        cache_key: str | None,
        lease: VideoAlgorithmCacheLease | None,
        assert_lease_owned: Callable[[], None] | None,
    ) -> VideoAlgorithmOutput:
        log = ctx.bind_logger(self.name)
        run_started = time.monotonic()
        cover_settings = self._cover_settings_for(input)

        target = input.max_videos  # 深掘り分析（DL+Gemini）する本数。重い。
        # 取得（スクレイプ）= 上位ボード board_size 本。メタのみ＝軽い。
        # 深掘り分析の予備候補も兼ねる（DL/分析失敗を後続候補でバックフィル）。天井は _MAX_POOL。
        board_target = min(max(input.board_size, target + self._overfetch_buffer), _MAX_POOL)

        # 取得段の委譲: caller-owned job_id から immutable 成果物を読む(スクレイプ無)。
        # per-call override はローカルで組み立て self へ保存しない(共有インスタンス安全)。
        call_searcher: Searcher | None = None
        call_downloader: Downloader | None = None
        # 複数 KW の取得ジョブにこの KW が入っていなかったとき、ジョブに入っていた KW。
        absent_from_job: list[str] = []
        if input.acquire_job_id:
            from teamagent.adapters.tiktok_s3_source import (
                TikTokS3Source,
                media_audit_principal_hash,
            )

            requested_by = ctx.metadata.get("user_email") or ctx.user_id or "unknown"
            _src = TikTokS3Source(
                input.acquire_job_id,
                audit_principal_hash=media_audit_principal_hash(requested_by),
            )

            def _s3_search(q: str, n: int, rid: str) -> list[VideoMeta]:
                posts, job_keywords = _posts_for_query(_src.posts(), q)
                if not posts and len(job_keywords) > 1:
                    absent_from_job[:] = job_keywords
                    log.warning(
                        "video_algorithm_s3_query_not_in_job",
                        job_id=input.acquire_job_id,
                        job_keywords=len(job_keywords),
                    )
                return self._posts_to_metas(posts[:n])

            call_searcher = _s3_search
            call_downloader = _src.download
            log.info("video_algorithm_s3_source", job_id=input.acquire_job_id)

        with _stage("search", ctx.request_id):
            pool = self._search(input.query, board_target, ctx.request_id, searcher=call_searcher)
        generated_at = _now_jst_iso()
        if not pool:
            empty_summary = f"🔎 「{input.query}」の検索結果を取得できませんでした。"
            if absent_from_job:
                # slack_summary は利用者へそのまま出る（SOUL: 引数名 job_id などは出さない）。
                empty_summary = (
                    f"🔎 「{input.query}」は渡された取得結果に入っていません"
                    f"（入っているKW: {'・'.join(absent_from_job)}）。"
                    "このKWを取得した結果を使うと分析できます。"
                )
            empty = VideoAlgorithmOutput(
                query=input.query,
                slack_summary=empty_summary,
                **_echo_fields(input),
                generated_at=generated_at,
            )
            if result_cache is not None and cache_key is not None and lease is not None:
                self._put_cached_result(
                    result_cache,
                    cache_key,
                    output=empty.model_dump(mode="json"),
                    stage="complete",
                    lease=lease,
                    request_id=ctx.request_id,
                    assert_lease_owned=assert_lease_owned,
                    failure_message=(
                        "VIDEO_ALGORITHM_CACHE_COMMIT_FAILED: 空の分析結果を保存できませんでした"
                    ),
                )
            return empty

        system = load_prompt("video_algorithm", self._prompt_version, "system")
        # 深掘り候補は「実際に DL して Gemini に渡せる動画」だけに絞る。
        # カルーセル/画像投稿は TikTok 側に video オブジェクトが無く duration=0 で届くため、
        # そのまま試行すると DL 失敗で 1 枠を空費し、結果が target 本に届かないことがある
        # （ユーザー実測の「5本揃わない」の主因）。ボード表示は pool のまま＝取得の事実は保つ。
        analyzable = [m for m in pool if float(getattr(m, "duration_sec", 0.0) or 0.0) > 0.0]
        skipped_non_video = len(pool) - len(analyzable)
        if skipped_non_video:
            log.info(
                "video_algorithm_skipped_non_video",
                skipped=skipped_non_video,
                analyzable=len(analyzable),
                target=target,
            )
        # duration が全件 0（取得経路が尺を返さない等）の場合は従来どおり pool を使う＝
        # 「情報が無いだけ」で分析ゼロに落とす方が有害なため fail-open。
        candidates = analyzable or pool
        # 二段構えの request 単位予算（集約本数 + 壁時計）。バックフィルの波を跨いで共有する。
        apify_budget = _ApifyFallbackBudget(
            max_videos=fallback_max_videos(),
            wallclock_s=_apify_wallclock_budget_s(),
        )
        # サムネ（一覧の表紙）: 上位の群は表示順の上位 target 本（画像投稿を除く）。動画の分析の
        # 成否では切らない。1 波目の quota の予約が通ってから始める（止まる依頼で課金しない）。
        cover_top = candidates[:target]
        cover_top_ranks = {m.rank for m in cover_top}
        all_zero = not analyzable
        reader: CoverReader | None = None
        board_submitted = False
        # 上位から波状に分析し、成功が target 本に達するか候補が尽きるまで（再検索はしない）
        results: list[AnalyzedVideo] = []
        attempted = 0
        quota_truncated = False
        try:
            while sum(1 for v in results if v.analysis) < target and attempted < len(candidates):
                need = target - sum(1 for v in results if v.analysis)
                batch = candidates[attempted : attempted + need]
                # 事前 consume: DL/Gemini/parse の失敗もコスト試行として数え、上限の並行
                # すり抜けを防ぐ。
                # バックフィル batch もここを通るため、実際に開始した分析本数が台帳へ乗る。
                if assert_lease_owned is not None:
                    assert_lease_owned()
                # 1波目（まだ 0 本）で足りなければ残数と選択肢を出して止める＝利用者に選ばせる。
                # 2波目以降は残数に丸めて進め、0 なら打ち切って**そこまでの成果を返す**。
                with _stage("quota", ctx.request_id):
                    reserved = self._reserve_quota(ctx, len(batch), allow_partial=bool(results))
                if reserved <= 0:
                    quota_truncated = True
                    break
                batch = batch[:reserved]
                attempted += len(batch)
                if reader is None and cover_settings.enabled and not results:
                    reader = self._start_cover_reader(ctx.request_id, cover_settings)
                    if reader is not None:
                        reader.submit(cover_top, "top", all_zero=all_zero)
                prefetch = {m.rank: self._prefetch_for(reader, m) for m in batch}
                workers = max(1, min(self._max_workers, len(batch)))
                with (
                    _stage("analyze_wave", ctx.request_id),
                    ThreadPoolExecutor(max_workers=workers) as ex,
                ):
                    results.extend(
                        ex.map(
                            lambda m, pre=prefetch: self._analyze_one(
                                m,
                                query=input.query,
                                client_name=input.client_name,
                                system=system,
                                request_id=ctx.request_id,
                                downloader=call_downloader,
                                user_email=str(ctx.metadata.get("user_email") or ""),
                                apify_budget=apify_budget,
                                # 構成分解のコマは場面ごと（最初と最後の場面を含む・最大 12 枚）。
                                # pick_timecodes の 6 枚は前半に偏り、本編と締めが無かった（M28）。
                                scene_frames=True,
                                frame_width=320,
                                opening_frame=True,
                                prefetched=pre[m.rank],
                            ),
                            batch,
                        )
                    )
                if reader is not None and cover_settings.board and not board_submitted:
                    # 6〜30 位の表紙は、1 波目の動画の job を出し終えてから投入する（dispatcher と
                    # Fargate の同時数を動画と取り合わないため）。
                    board_submitted = True
                    reader.submit(
                        [m for m in pool if m.rank not in cover_top_ranks],
                        "rest",
                        all_zero=all_zero,
                    )
        except BaseException:
            # 動画の波が例外で止まったら、表紙の読み取りも待たずに片付ける（裏で課金を続けない）。
            if reader is not None:
                reader.join(0.0)
            raise
        # 成功が target に達したら失敗カードは捨てる（バックフィル済み）。
        # 足りなければ失敗も見せて正直に（候補枯渇・全滅を隠さない）。
        ok = [v for v in results if v.analysis]
        if len(ok) >= target:
            analyzed = ok[:target]
        else:
            analyzed = (ok + [v for v in results if not v.analysis])[:target]
        analyzed.sort(key=lambda v: v.meta.rank)
        backfilled = sum(1 for v in analyzed if v.analysis and v.meta.rank > target)

        cover_cost = 0.0
        if reader is not None:
            with _stage("cover_join", ctx.request_id):
                reads, cover_cost = reader.join(reader.remaining_budget_s(run_started))
            # 置き場所は上位ボードの各行だけ（唯一の正）。videos[].meta は同じ物を指すので
            # 写しにする。
            for m in pool:
                if m.rank in reads:
                    m.cover_read = reads[m.rank]
            for v in results:
                if v.meta.cover_read is not None:
                    v.meta = v.meta.model_copy(update={"cover_read": None})

        roster = Roster.of(input.client_name, input.competitors)
        with _stage("cross", ctx.request_id):
            cross = cross_analyze(analyzed, input.query, board=pool, roster=roster)
        # 全試行の課金を計上（表紙の読み取りは、返ってきた分だけ・float のときだけ足してある）
        total_cost = round(sum(v.cost_usd for v in results) + cover_cost, 6)
        # 横断シンセシス（Gemini 2nd pass・概念の関連性）。≥2本でのみ実行
        if sum(1 for v in analyzed if v.analysis) >= 2:
            from teamagent.skills.video_algorithm.synthesis import synthesize

            if assert_lease_owned is not None:
                assert_lease_owned()
            with _stage("synthesis", ctx.request_id):
                syn, syn_cost = synthesize(
                    self._client(),
                    analyzed,
                    input.query,
                    request_id=ctx.request_id,
                    prompt_version=self._synthesis_version,
                    stats=cross.stats,
                    extra_context=self._kw_context(input),
                    roster=roster,
                    board=pool,
                    avoid_terms=input.avoid_terms,
                )
            cross.synthesis = syn
            total_cost = round(total_cost + syn_cost, 6)
        model_id = next((v.model_id for v in analyzed if v.model_id), None)

        out = VideoAlgorithmOutput(
            query=input.query,
            videos=analyzed,
            board=pool,  # 取得した全メタ（上位ボード board_size 本・深掘りは上位 target 本のみ）
            cross=cross,
            total_cost_usd=total_cost,
            model_id=model_id,
            search_volume=input.search_volume,
            kw_set=list(input.kw_set or []),
            **_echo_fields(input),
            generated_at=generated_at,
            # off＝止めている設定・top／board＝読む設定（読み取りを始められなかったときも同じ値で、
            # 描画は上位ボードに読み取りが無いことから「読めず」と出す）。
            cover_read_mode=cover_settings.mode,
            quota_note=(
                f"今月の残り本数の都合で{len(ok)}本までで止めました"
                f"（ご依頼は{target}本）。リセットは来月1日（JST）です。"
                if quota_truncated
                else None
            ),
        )
        if result_cache is not None and cache_key is not None and lease is not None:
            # Gemini/横断 synthesis の課金済み core を成果物生成より先に commit する。
            # report/slides/pptx が失敗しても retry はこの core から再生成し、再分析しない。
            self._put_cached_result(
                result_cache,
                cache_key,
                output=out.model_dump(mode="json"),
                stage="paid_core",
                lease=lease,
                request_id=ctx.request_id,
                assert_lease_owned=assert_lease_owned,
                failure_message=(
                    "VIDEO_ALGORITHM_CACHE_COMMIT_FAILED: 課金済み分析結果を保存できないため、"
                    "成果物生成を中止しました"
                ),
            )
        report_dir = self._report_dir or _request_report_dir(ctx.request_id)
        # TEAMAGENT_VSEO_REPORT_DIR is a base directory, not a persistence opt-out:
        # _request_report_dir() still created a uniquely owned child beneath it.
        owns_request_dir = self._report_dir is None
        handed_off = False
        try:
            with _stage("report", ctx.request_id):
                out.report_html_path = self._write_report(out, ctx.request_id, report_dir)
                if out.report_html_path:
                    # §M: 金庫外の OpenClaw 等が読めるよう、非公開S3へ発行して
                    # 署名URLを出力に載せる。
                    out.report_url = self._publish(
                        out.report_html_path, ctx.request_id, input.query
                    )
            if result_cache is not None and cache_key is not None and lease is not None:
                # 後続slides/pptxだけが失敗しても、発行済みの高品質report URLは
                # coreへcheckpointし、sanitized結果から再生成しない。
                self._put_cached_result(
                    result_cache,
                    cache_key,
                    output=out.model_dump(mode="json"),
                    stage="paid_core",
                    lease=lease,
                    request_id=ctx.request_id,
                    assert_lease_owned=assert_lease_owned,
                    failure_message=(
                        "VIDEO_ALGORITHM_CACHE_COMMIT_FAILED: "
                        "report checkpointを保存できませんでした"
                    ),
                )
            if "slides" in input.outputs or "pptx" in input.outputs:
                with _stage("slides", ctx.request_id):
                    self._build_proposal_outputs(out, input, ctx.request_id, report_dir)
            out.slack_summary = self._slack_summary(out, backfilled)
            if out.report_html_path is None and owns_request_dir and os.path.exists(report_dir):
                # report生成失敗後にslidesだけが残っても、配送済みならここで回収する。
                shutil.rmtree(report_dir)
            log.info(
                "video_algorithm_done",
                requested=input.max_videos,
                board_size=input.board_size,
                scraped=len(pool),
                attempted=attempted,
                analyzed=sum(1 for v in analyzed if v.analysis),
                backfilled=backfilled,
                failed=len(results) - len(ok),
                cost_usd=total_cost,
                report=out.report_html_path,
            )
            if result_cache is not None and cache_key is not None and lease is not None:
                self._put_cached_result(
                    result_cache,
                    cache_key,
                    output=out.model_dump(mode="json"),
                    stage="complete",
                    lease=lease,
                    request_id=ctx.request_id,
                    assert_lease_owned=assert_lease_owned,
                    failure_message=(
                        "VIDEO_ALGORITHM_CACHE_COMMIT_FAILED: 完了結果を保存できませんでした"
                    ),
                )
            handed_off = True
            return out
        finally:
            # Successful output is retained only until the runtime serializes or
            # uploads it and invokes cleanup_output().  Any exception before
            # handoff removes the complete request directory here.
            if owns_request_dir and not handed_off and os.path.exists(report_dir):
                shutil.rmtree(report_dir)

    def _finalize_cached_output(
        self,
        out: VideoAlgorithmOutput,
        *,
        input: VideoAlgorithmInput,
        ctx: SkillContext,
        result_cache: VideoAlgorithmResultCache,
        cache_key: str,
        lease: VideoAlgorithmCacheLease,
        assert_lease_owned: Callable[[], None] | None,
    ) -> VideoAlgorithmOutput:
        """課金済み core から成果物だけを再生成する（Gemini/quota は呼ばない）。"""

        backfilled = sum(
            1 for video in out.videos if video.analysis and video.meta.rank > input.max_videos
        )
        original_cost = out.total_cost_usd
        report_dir = self._report_dir or _request_report_dir(ctx.request_id)
        owns_request_dir = self._report_dir is None
        handed_off = False
        try:
            if out.report_url is None:
                out.report_html_path = self._write_report(out, ctx.request_id, report_dir)
                if out.report_html_path:
                    out.report_url = self._publish(
                        out.report_html_path,
                        ctx.request_id,
                        input.query,
                    )
            else:
                # paid_core checkpoint済みの元レポートを優先し、sanitized coreから劣化再生成しない。
                out.report_html_path = None
            if "slides" in input.outputs or "pptx" in input.outputs:
                self._build_proposal_outputs(out, input, ctx.request_id, report_dir)
            out.slack_summary = self._slack_summary(out, backfilled)
            if out.report_html_path is None and owns_request_dir and os.path.exists(report_dir):
                shutil.rmtree(report_dir)
            # 保存する cost は元分析の実績。返却値だけを 0 にし、今回の再利用が無課金と示す。
            out.total_cost_usd = original_cost
            self._put_cached_result(
                result_cache,
                cache_key,
                output=out.model_dump(mode="json"),
                stage="complete",
                lease=lease,
                request_id=ctx.request_id,
                assert_lease_owned=assert_lease_owned,
                failure_message=(
                    "VIDEO_ALGORITHM_CACHE_COMMIT_FAILED: 再生成した成果物を保存できませんでした"
                ),
            )
            out.total_cost_usd = 0.0
            out.slack_summary = self._slack_summary(out, backfilled)
            ctx.bind_logger(self.name).info(
                "video_algorithm_cache_artifacts_regenerated",
                requested=input.max_videos,
                board_size=input.board_size,
            )
            handed_off = True
            return out
        finally:
            if owns_request_dir and not handed_off and os.path.exists(report_dir):
                shutil.rmtree(report_dir)

    def _publish(self, path: str, request_id: str, query: str) -> str | None:
        """ローカル HTML レポートを非公開S3へ発行し署名URL(7日)を返す（失敗は None＝graceful）。

        注入 publisher があればそれを使う（テスト差し替え）。無ければ VSEO_REPORT_BUCKET 設定時のみ
        既定実装を遅延使用＝ローカル/テスト（bucket未設定）では S3 を叩かず None。
        """
        if self._publisher is not None:
            return self._publisher(path, request_id=request_id, query=query)
        if not os.environ.get("VSEO_REPORT_BUCKET"):
            return None
        from teamagent.adapters.report_publish import publish_html_file_result
        from teamagent.skills._shared.report_delivery import delivery_url

        result = publish_html_file_result(path, request_id=request_id, query=query)
        if result is None:
            return None
        # 配信URLの判断は全 HTML レポート共通のチョークポイントへ（openclaw が presigned の
        # クエリを落として壊す事象は本 skill のレポートでも同じく起きる）。
        return delivery_url(result, request_id=request_id)

    def _build_proposal_outputs(
        self,
        out: VideoAlgorithmOutput,
        input: VideoAlgorithmInput,
        request_id: str,
        report_dir: str | None = None,
    ) -> None:
        """提案資料向け slides(HTML)/pptx を生成しS3署名URLを out に載せる（全工程graceful）。

        - slides: render_slides(out) を S3 へ（HTML・編集可・営業がブラウザで直す）。
        - pptx:   slides を playwright で要素スクショ→python-pptx→S3（拡張版イメージの chromium）。
        どの段が失敗しても本体分析(out)は壊さない＝報告だけ残して None のまま進む。
        """
        resolved_report_dir = report_dir or self._report_dir or _request_report_dir(request_id)
        safe = re.sub(r"[^\w]+", "_", out.query).strip("_")[:40] or "kw"
        if "slides" in input.outputs or "pptx" in input.outputs:
            try:
                from teamagent.skills.video_algorithm.slides import render_slides

                os.makedirs(resolved_report_dir, exist_ok=True)
                spath = os.path.join(
                    resolved_report_dir,
                    f"vseo_slides_{safe}_{uuid.uuid4().hex[:8]}.html",
                )
                with open(spath, "w", encoding="utf-8") as f:
                    f.write(render_slides(out, generated_at=out.generated_at or ""))
                if "slides" in input.outputs:
                    out.slides_url = self._publish_artifact(
                        spath, request_id, out.query, kind="slides"
                    )
                if "pptx" in input.outputs:
                    out.pptx_url = self._build_pptx(
                        out,
                        resolved_report_dir,
                        safe,
                        request_id,
                    )
            except Exception:
                logger.warning("vseo_proposal_outputs_failed", request_id=request_id)
                if "pptx" in input.outputs:
                    raise

    def _build_pptx(
        self, out: VideoAlgorithmOutput, report_dir: str, safe: str, request_id: str
    ) -> str | None:
        try:
            ppath = os.path.join(report_dir, f"vseo_proposal_{safe}_{uuid.uuid4().hex[:8]}.pptx")
            from teamagent.adapters.media_job import MediaJobClient

            if MediaJobClient.is_configured():
                from teamagent.skills.video_algorithm.slides import render_slides

                # 従来挙動維持: 1280x720 の HTML を device_scale_factor=2 で撮影する
                # （slides_to_pptx の既定 scale が 1 に変わったため明示する）。
                pptx = MediaJobClient().slides_to_pptx(
                    render_slides(out, generated_at=out.generated_at or ""),
                    request_fingerprint=f"{request_id}:slides-pptx",
                    width=1280,
                    height=720,
                    device_scale_factor=2,
                )
                with open(ppath, "wb") as file:
                    file.write(pptx)
            elif MediaJobClient.local_runtime_enabled():
                from teamagent.skills.video_algorithm.pptx_export import render_pptx

                if render_pptx(out, ppath, generated_at=out.generated_at or "") is None:
                    return None
            else:
                MediaJobClient.require_configured()
            return self._publish_artifact(ppath, request_id, out.query, kind="pptx")
        except Exception:
            logger.warning("vseo_pptx_build_failed", request_id=request_id)
            raise

    def _publish_artifact(self, path: str, request_id: str, query: str, *, kind: str) -> str | None:
        """slides/pptx を非公開S3へ発行（publisher 注入優先・bucket未設定なら None）。

        2026-08-31 から /r 短縮URLを優先する。生の署名付きURLは ECS タスクロールの
        一時 credential で署名され**最大1時間で失効**する（宣言 7 日は名目）ため、
        「7日有効」と案内して渡す成果物は必ず delivery_url() を通す。
        条件未充足時は従来どおり生 presigned へフォールバック（fail-open・配信は止めない）。
        """
        if self._publisher is not None:
            return self._publisher(path, request_id=request_id, query=query)
        if not os.environ.get("VSEO_REPORT_BUCKET"):
            return None
        from teamagent.adapters.report_publish import publish_artifact_result
        from teamagent.skills._shared.report_delivery import delivery_url

        result = publish_artifact_result(
            path,
            "pptx" if kind == "pptx" else "slides_html",
            request_id=request_id,
            query=query,
        )
        if result is None:
            return None
        return delivery_url(result, request_id=request_id)

    def _write_report(
        self,
        out: VideoAlgorithmOutput,
        request_id: str,
        report_dir: str | None = None,
    ) -> str | None:
        try:
            resolved_report_dir = report_dir or self._report_dir or _request_report_dir(request_id)
            os.makedirs(resolved_report_dir, exist_ok=True)
            safe = re.sub(r"[^\w]+", "_", out.query).strip("_")[:40] or "kw"
            path = os.path.join(
                resolved_report_dir,
                f"vseo_{safe}_{uuid.uuid4().hex[:8]}.html",
            )
            html = render_report(out, generated_at=out.generated_at or "")
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
            return path
        except Exception:
            logger.warning("video_algorithm_report_write_failed", request_id=request_id)
            return None

    def cleanup_output(self, out: VideoAlgorithmOutput) -> None:
        """配送/JSON化後にreport・slides・PPTXを含むrequest dirを削除する。"""

        if self._report_dir is not None:
            return
        if not out.report_html_path:
            return
        request_dir = os.path.dirname(os.path.abspath(out.report_html_path))
        root = os.path.abspath(_default_report_dir())
        if request_dir == root or not os.path.commonpath((root, request_dir)) == root:
            logger.warning("video_algorithm_cleanup_scope_rejected", path=request_dir)
            return
        try:
            shutil.rmtree(request_dir)
        except FileNotFoundError:
            return

    @staticmethod
    def _kw_context(input: VideoAlgorithmInput) -> str:
        """カタログ⑥: 兄弟KW群・月間検索量を synthesis の追加文脈にする（無ければ空）。"""
        parts: list[str] = []
        if input.kw_set:
            parts.append(
                f"このKW「{input.query}」は兄弟KW群（{'・'.join(input.kw_set)}）の比較分析の一部。"
                "summary の中で、この群の中での本KWの位置づけ（面の空き/激戦度）に1文言及すること。"
            )
        if input.search_volume is not None:
            parts.append(
                f"本KWの月間検索量（ラッコキーワード手動実測）: {input.search_volume:,}。"
                "検索量と面の状況を掛け合わせたKW優先度の示唆があれば含めること。"
            )
        return "\n".join(parts)

    def _slack_summary(self, out: VideoAlgorithmOutput, backfilled: int = 0) -> str:
        """Slack は『通知』だけ（詳細は添付 HTML レポートに全て埋め込む）。

        共通点は「勝ち筋」と呼ばない。段階の名前（必須条件／多数派／事例）はコードが本数から
        付けたもの（facts / evidence.tier）。URL は必ず行末に置く（#463）。クライアント名が
        無ければ、区分と提案文を入れた版に作り直せることを最後の 1 行で案内する。
        """
        c = out.cross
        ok = sum(1 for v in out.videos if v.analysis)
        bf = f"／下位繰上げ{backfilled}本" if backfilled else ""
        points = _tier_points(out)
        top = f"\n共通点（コードの集計）: {points}" if points else ""
        if c.cover_line:
            top += f"\n{c.cover_line}"
        proposal_lines = ""
        if out.pptx_url:
            proposal_lines += f"\n📊 画像のパワポ（文字の修正はHTML版で・7日有効）: {out.pptx_url}"
        if out.slides_url:
            proposal_lines += f"\n✏️ 編集用スライド（ブラウザで直接編集）: {out.slides_url}"
        # URL は必ず行末に置く。直後に全角の文字が続くと、Slack がその文字まで URL に含めて
        # リンクが 404 になる（09-28 本番「（タイムライン/…）」で発生）。
        report_line = (
            f"📄 詳細レポート（構成/テロップ/ブランド検出/タイムライン・7日有効）: {out.report_url}"
            if out.report_url
            else "📄 詳細は添付の HTML レポートをご覧ください"
        )
        volume_line = (
            f"📈 月間検索量(手動実測): {out.search_volume:,}\n" if out.search_volume else ""
        )
        quota_line = f"ℹ️ {out.quota_note}\n" if out.quota_note else ""
        client_line = "" if (out.client_name or "").strip() else f"\n{CLIENT_MISSING_NOTE}"
        return (
            f"🔎 **VSEO動画アルゴリズム分析** 完了「{out.query}」"
            f"（上位{len(out.videos)}本／分析成功{ok}本{bf}）\n"
            f"{c.summary}{top}\n{quota_line}{volume_line}"
            f"{report_line}{proposal_lines}\n"
            f"_概算 ${out.total_cost_usd:.4f}・n={c.video_count} の観測仮説（相関≠因果）_"
            f"{client_line}"
        )


@register
class VideoAlgorithmStatusSkill(BaseSkill[VideoAlgorithmStatusInput, VideoAlgorithmStatusOutput]):
    name: ClassVar[str] = "video_algorithm_status"
    description: ClassVar[str] = "動画分析の実際の状態を返す。番号省略時は本人の直近の分析。"
    input_schema: ClassVar[type[BaseModel]] = VideoAlgorithmStatusInput
    output_schema: ClassVar[type[BaseModel]] = VideoAlgorithmStatusOutput

    def run(
        self, input: VideoAlgorithmStatusInput, ctx: SkillContext
    ) -> VideoAlgorithmStatusOutput:
        from teamagent.adapters.proposal_job_store import ProposalJobStore
        from teamagent.skills._shared.long_jobs import latest_job, owner_key

        job_id = input.job_id or latest_job(ctx, "video_algorithm")
        owner = owner_key(ctx, "video_algorithm")
        row = ProposalJobStore().get_job(job_id) if job_id.startswith("va_") and owner else None
        try:
            summary = json.loads(row.get("request_summary") or "{}") if row else {}
        except (ValueError, TypeError):
            summary = {}
        if row is None or not isinstance(summary, dict) or summary.get("owner") != owner:
            return VideoAlgorithmStatusOutput(
                job_id=job_id, status="unknown", message="確認できる動画分析がありません。"
            )
        state = str(row.get("status") or "unknown")
        if state in {"queued", "running"}:
            from datetime import UTC, datetime

            try:
                updated = datetime.fromisoformat(
                    str(row.get("updated_at") or "").replace("Z", "+00:00")
                )
                stale = (datetime.now(UTC) - updated).total_seconds() > 180
            except (ValueError, TypeError):
                stale = True
            from teamagent.skills._shared.long_jobs import job_is_alive

            # 確かめられない（None）ときは止まったと断定しない（False のときだけ失敗にする）。
            if stale and job_is_alive(job_id.removeprefix("va_")) is False:
                store = ProposalJobStore()
                changed = store.mark_failed(
                    job_id,
                    "MCP_RESTARTED",
                    expected_statuses=(state,),
                    expected_updated_at=row.get("updated_at"),
                    error_summary="動画分析の実行継続を確認できなくなりました。",
                )
                row = store.get_job(job_id) or row
                state = "failed" if changed else str(row.get("status") or "unknown")
        message = {
            "queued": "動画分析は順番待ちです。",
            "running": "動画分析は実行中です。",
            "failed": str(row.get("error_summary") or "動画分析の途中で問題が起きました。"),
            "unknown": "動画分析の状態を確認できません。",
        }.get(state, "動画分析の状態を確認できません。")
        if state == "done":
            try:
                result = json.loads(row.get("result_json") or "{}")
                message = str(result.get("message") or "") if isinstance(result, dict) else ""
            except (ValueError, TypeError):
                message = ""
            if not message:
                state, message = "unknown", "動画分析の結果を確認できません。"
        return VideoAlgorithmStatusOutput(job_id=job_id, status=state, message=message)
