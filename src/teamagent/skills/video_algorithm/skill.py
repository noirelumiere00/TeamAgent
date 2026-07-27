"""VideoAlgorithm Skill 本体（VSEO 動画アルゴリズム読み解き）。

検索KW → 上位N本（tiktok_search）→ 各動画 download→proxy→Gemini構造分析 → 5本横断
→ HTML タイムラインレポート + Slack 要約。

3層分離: Skill 層。検索/取得/圧縮/Gemini は adapters。重い I/O は ThreadPool で並列化。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, Thread
from typing import Any, ClassVar, Literal

import structlog
from pydantic import BaseModel, ValidationError

from teamagent.adapters.gemini_client import GeminiClient
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
from teamagent.skills.video_algorithm.report import render_report
from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    FrameShot,
    ThumbColor,
    VideoAlgorithmInput,
    VideoAlgorithmOutput,
    VideoMeta,
    VideoVSEOAnalysis,
)

logger = structlog.get_logger(__name__)

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)

Searcher = Callable[[str, int, str], list[VideoMeta]]
Downloader = Callable[[str], tuple[bytes, str]]
Proxy = Callable[[bytes, str], tuple[bytes, str]]

_MAX_FIELD_RESETS = 8  # 寛容パース: 最大何フィールドまで default に戻して動画を救済するか
_OVERFETCH_BUFFER = 4  # over-fetch: 目標+この本数を検索し DL/分析失敗を後続候補でバックフィル
_MAX_POOL = 30  # 検索の絶対上限（スクレイパ実証済み。レート制限/遮断を踏み抜かない）


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
        "フック/CTA）→ 5本横断で勝ち筋を読み解き、HTMLタイムラインレポートを生成"
    )
    input_schema: ClassVar[type[BaseModel]] = VideoAlgorithmInput
    output_schema: ClassVar[type[BaseModel]] = VideoAlgorithmOutput

    def __init__(
        self,
        gemini: GeminiClient | None = None,
        *,
        prompt_version: str = "v1",
        searcher: Searcher | None = None,
        downloader: Downloader | None = None,
        proxy: Proxy | None = None,
        report_dir: str | None = None,
        max_workers: int = 3,
        overfetch_buffer: int = _OVERFETCH_BUFFER,
        publisher: Callable[..., str | None] | None = None,
        result_cache: VideoAlgorithmResultCache | None = None,
    ) -> None:
        self._gemini = gemini
        self._prompt_version = prompt_version
        self._searcher = searcher
        self._downloader = downloader
        self._proxy = proxy
        self._report_dir = report_dir
        self._max_workers = max_workers
        self._overfetch_buffer = overfetch_buffer
        self._publisher = publisher
        self._result_cache = result_cache

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
        return os.environ.get("GEMINI_MODEL_ID", "gemini-2.5-flash").strip() or "gemini-2.5-flash"

    @staticmethod
    def _consume_quota_or_raise(ctx: SkillContext, count: int) -> None:
        """Gemini 分析を開始する batch 本数を事前消費する（既定OFFなら完全 no-op）。

        事後消費では並行リクエストが上限をすり抜けるため、失敗試行も含めて開始前に確保する。
        取得失敗後の cover-only 縮退も同じ「分析試行」1本として数える。
        """

        from teamagent.adapters.quota_store import VideoQuotaStore

        if count <= 0 or not VideoQuotaStore.enabled():
            return
        email = str(ctx.metadata.get("user_email", "") or "").strip().lower()
        if not email:
            # quota ON なのに主体不明を allowed no-op にするとコスト上限を迂回できる。
            # そのため、quota が有効な場合だけ fail-closed にする。
            raise RuntimeError(
                "VIDEO_QUOTA_IDENTITY_REQUIRED: 動画分析クォータの利用者メールを解決できません"
            )
        result = VideoQuotaStore().try_consume(email, count, request_id=ctx.request_id)
        if not result.allowed:
            raise RuntimeError(
                f"VIDEO_QUOTA_EXCEEDED: 今月の動画分析上限（{result.limit}本）に達しました"
                f"（使用 {result.used}本）。リセットは来月1日（JST）です。"
                "お急ぎの場合は管理者に上限引き上げを依頼してください。"
            )

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
    ) -> AnalyzedVideo:
        try:
            data, mime = self._download(meta.url, request_id, downloader=downloader)
            data, mime = self._shrink(data, mime, request_id)
        except Exception as e:  # 取得/圧縮失敗 → サムネのみの軽量分析へ縮退（全滅回避）
            logger.warning("video_algorithm_fetch_failed", rank=meta.rank, error=type(e).__name__)
            return self._cover_only_analysis(
                meta, query=query, system=system, request_id=request_id, cause=type(e).__name__
            )

        user_prompt = (
            f"# 検索KW: {query}\n"
            f"# この動画の表示順位: {meta.rank}位\n"
            + (f"# クライアント名（competitor判定用）: {client_name}\n" if client_name else "")
            + f"# キャプション本文: {meta.desc}\n\n"
            "この動画を実際に視聴し、システム指示のJSON形式で VSEO 構造分析を出力してください。"
        )
        try:
            resp = self._client().analyze_video_bytes(
                data=data, mime_type=mime, prompt=user_prompt, request_id=request_id, system=system
            )
        except Exception as e:
            logger.warning("video_algorithm_gemini_failed", rank=meta.rank, error=type(e).__name__)
            return AnalyzedVideo(meta=meta, error=f"分析失敗: {type(e).__name__}")

        analysis = parse_analysis(resp.text)
        frames: list[FrameShot] = []
        if analysis is not None:
            # proxy 後の検証済み bytes を使い回して実フレームを抽出（graceful）
            from teamagent.skills.video_algorithm.frames import extract_frames, pick_timecodes

            tcs = pick_timecodes(analysis, max_frames=6)
            if tcs:
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
                            width=320,
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
                            rank=meta.rank,
                            error=type(exc).__name__,
                        )
                        raise RuntimeError("MEDIA_FRAME_JOB_FAILED") from exc
                elif MediaJobClient.local_runtime_enabled():
                    shots = extract_frames(
                        data, mime, [s for s, _ in tcs], width=320, request_id=request_id
                    )
                else:
                    MediaJobClient.require_configured()
                    raise AssertionError("unreachable")
                frames = [
                    FrameShot(sec=s, caption=cap_by_sec.get(round(s, 1), f"{s:.0f}s"), data_uri=uri)
                    for s, uri in shots
                ]
        # サムネ色（検索一覧タイル）: cover_url を取得、失敗時は先頭フレームを流用
        cover_uri, thumb = self._build_thumb(meta.cover_url, frames, request_id)
        # タイムラインで実再生する軽量Webプレビュー動画（~480p・graceful。失敗時は静止フレーム）
        video_uri = ""
        if analysis is not None:
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
                    video_uri = "data:video/mp4;base64," + base64.b64encode(preview).decode("ascii")
                except Exception as exc:
                    logger.warning(
                        "video_algorithm_preview_failed",
                        rank=meta.rank,
                        error=type(exc).__name__,
                    )
                    raise RuntimeError("MEDIA_PREVIEW_JOB_FAILED") from exc
            elif MediaJobClient.local_runtime_enabled():
                from teamagent.adapters.video_proxy import make_web_preview

                video_uri = make_web_preview(data, mime, request_id=request_id)
            else:
                MediaJobClient.require_configured()
                raise AssertionError("unreachable")
        return AnalyzedVideo(
            meta=meta,
            analysis=analysis,
            frames=frames,
            video_data_uri=video_uri,
            cover_data_uri=cover_uri,
            thumb=thumb,
            error=None if analysis else "JSONパース失敗",
            cost_usd=resp.cost_usd,
            model_id=getattr(resp, "model_id", None),
        )

    def _cover_only_analysis(
        self, meta: VideoMeta, *, query: str, system: str, request_id: str, cause: str
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
        cover_uri, thumb = self._build_thumb(meta.cover_url, [], request_id)
        return AnalyzedVideo(
            meta=meta,
            analysis=analysis,
            cover_data_uri=cover_uri,
            thumb=thumb,
            error="動画取得失敗・サムネのみ軽量分析" if analysis else f"取得失敗: {cause}",
            cost_usd=resp.cost_usd,
            model_id=getattr(resp, "model_id", None),
        )

    def _build_thumb(
        self, cover_url: str | None, frames: list[FrameShot], request_id: str
    ) -> tuple[str, ThumbColor | None]:
        """サムネ色を算出。cover_url 取得失敗時は抽出済みフレーム先頭で代替（graceful）。"""
        from teamagent.adapters.media_job import MediaJobClient
        from teamagent.skills.video_algorithm.thumbnails import (
            analyze_cover,
            build_thumb,
        )

        if MediaJobClient.is_configured():
            import base64
            import hashlib

            source: bytes | None = None
            if frames:
                head = frames[0].data_uri
                if head.startswith("data:image/jpeg;base64,"):
                    try:
                        source = base64.b64decode(head.split(",", 1)[1], validate=True)
                    except Exception:
                        source = None
            try:
                if source is not None:
                    fingerprint = hashlib.sha256(source).hexdigest()
                    image, metadata = MediaJobClient().make_thumbnail(
                        source,
                        _sniff_image_mime(source),
                        request_fingerprint=f"{request_id}:thumbnail:{fingerprint}",
                        width=240,
                    )
                elif cover_url:
                    fingerprint = hashlib.sha256(cover_url.encode("utf-8")).hexdigest()
                    image, metadata = MediaJobClient().make_thumbnail_from_url(
                        cover_url,
                        request_fingerprint=f"{request_id}:thumbnail-url:{fingerprint}",
                        width=240,
                    )
                else:
                    return "", None
            except Exception as exc:
                logger.warning(
                    "video_algorithm_thumbnail_failed",
                    error=type(exc).__name__,
                )
                raise RuntimeError("MEDIA_THUMBNAIL_JOB_FAILED") from exc
            return (
                "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii"),
                ThumbColor.model_validate(metadata),
            )

        if not MediaJobClient.local_runtime_enabled():
            MediaJobClient.require_configured()
            raise AssertionError("unreachable")
        res = build_thumb(cover_url, request_id=request_id)
        if res is None and frames:
            head = frames[0].data_uri
            if head.startswith("data:image/jpeg;base64,"):
                import base64

                try:
                    res = analyze_cover(
                        base64.b64decode(head.split(",", 1)[1]), request_id=request_id
                    )
                except Exception:
                    res = None
        return res if res is not None else ("", None)

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
            )
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

        target = input.max_videos  # 深掘り分析（DL+Gemini）する本数。重い。
        # 取得（スクレイプ）= 上位ボード board_size 本。メタのみ＝軽い。
        # 深掘り分析の予備候補も兼ねる（DL/分析失敗を後続候補でバックフィル）。天井は _MAX_POOL。
        board_target = min(max(input.board_size, target + self._overfetch_buffer), _MAX_POOL)

        # 取得段の委譲: caller-owned job_id から immutable 成果物を読む(スクレイプ無)。
        # per-call override はローカルで組み立て self へ保存しない(共有インスタンス安全)。
        call_searcher: Searcher | None = None
        call_downloader: Downloader | None = None
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
                return self._posts_to_metas(_src.posts(n))

            call_searcher = _s3_search
            call_downloader = _src.download
            log.info("video_algorithm_s3_source", job_id=input.acquire_job_id)

        pool = self._search(input.query, board_target, ctx.request_id, searcher=call_searcher)
        if not pool:
            empty = VideoAlgorithmOutput(
                query=input.query,
                slack_summary=f"🔎 「{input.query}」の検索結果を取得できませんでした。",
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
        # 上位から波状に分析し、成功が target 本に達するか候補が尽きるまで（再検索はしない）
        results: list[AnalyzedVideo] = []
        attempted = 0
        while sum(1 for v in results if v.analysis) < target and attempted < len(pool):
            need = target - sum(1 for v in results if v.analysis)
            batch = pool[attempted : attempted + need]
            attempted += len(batch)
            # 事前 consume: DL/Gemini/parse の失敗もコスト試行として数え、上限の並行すり抜けを防ぐ。
            # バックフィル batch もここを通るため、実際に開始した分析本数が台帳へ乗る。
            if assert_lease_owned is not None:
                assert_lease_owned()
            self._consume_quota_or_raise(ctx, len(batch))
            workers = max(1, min(self._max_workers, len(batch)))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                results.extend(
                    ex.map(
                        lambda m: self._analyze_one(
                            m,
                            query=input.query,
                            client_name=input.client_name,
                            system=system,
                            request_id=ctx.request_id,
                            downloader=call_downloader,
                        ),
                        batch,
                    )
                )
        # 成功が target に達したら失敗カードは捨てる（バックフィル済み）。
        # 足りなければ失敗も見せて正直に（候補枯渇・全滅を隠さない）。
        ok = [v for v in results if v.analysis]
        if len(ok) >= target:
            analyzed = ok[:target]
        else:
            analyzed = (ok + [v for v in results if not v.analysis])[:target]
        analyzed.sort(key=lambda v: v.meta.rank)
        backfilled = sum(1 for v in analyzed if v.analysis and v.meta.rank > target)

        cross = cross_analyze(analyzed, input.query, board=pool)
        total_cost = round(sum(v.cost_usd for v in results), 6)  # 全試行の課金を計上
        # 横断シンセシス（Gemini 2nd pass・概念の関連性）。≥2本でのみ実行
        if sum(1 for v in analyzed if v.analysis) >= 2:
            from teamagent.skills.video_algorithm.synthesis import synthesize

            if assert_lease_owned is not None:
                assert_lease_owned()
            syn, syn_cost = synthesize(
                self._client(),
                analyzed,
                input.query,
                request_id=ctx.request_id,
                stats=cross.stats,
                extra_context=self._kw_context(input),
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
            out.report_html_path = self._write_report(out, ctx.request_id, report_dir)
            if out.report_html_path:
                # §M: 金庫外の OpenClaw 等が読めるよう、非公開S3へ発行して署名URLを出力に載せる。
                out.report_url = self._publish(out.report_html_path, ctx.request_id, input.query)
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
                    f.write(render_slides(out))
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

                pptx = MediaJobClient().slides_to_pptx(
                    render_slides(out),
                    request_fingerprint=f"{request_id}:slides-pptx",
                )
                with open(ppath, "wb") as file:
                    file.write(pptx)
            elif MediaJobClient.local_runtime_enabled():
                from teamagent.skills.video_algorithm.pptx_export import render_pptx

                if render_pptx(out, ppath) is None:
                    return None
            else:
                MediaJobClient.require_configured()
            return self._publish_artifact(ppath, request_id, out.query, kind="pptx")
        except Exception:
            logger.warning("vseo_pptx_build_failed", request_id=request_id)
            raise

    def _publish_artifact(self, path: str, request_id: str, query: str, *, kind: str) -> str | None:
        """slides/pptx を非公開S3へ発行（publisher 注入優先・bucket未設定なら None）。"""
        if self._publisher is not None:
            return self._publisher(path, request_id=request_id, query=query)
        if not os.environ.get("VSEO_REPORT_BUCKET"):
            return None
        from teamagent.adapters.report_publish import publish_html_file, publish_pptx_file

        fn = publish_pptx_file if kind == "pptx" else publish_html_file
        return fn(path, request_id=request_id, query=query)

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
            html = render_report(out)
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
        """Slack は『通知』だけ（詳細は添付 HTML レポートに全て埋め込む）。"""
        c = out.cross
        ok = sum(1 for v in out.videos if v.analysis)
        bf = f"／下位繰上げ{backfilled}本" if backfilled else ""
        top = f"　最有力の勝ち筋: 『{c.win_factors[0].factor}』" if c.win_factors else ""
        proposal_lines = ""
        if out.pptx_url:
            proposal_lines += f"\n📊 提案用パワポ（7日有効・そのまま提案資料へ）: {out.pptx_url}"
        if out.slides_url:
            proposal_lines += f"\n✏️ 編集用スライド（ブラウザで直接編集）: {out.slides_url}"
        report_line = (
            f"📄 詳細レポート（7日有効）: {out.report_url}"
            if out.report_url
            else "📄 詳細は添付の HTML レポートをご覧ください"
        )
        volume_line = (
            f"📈 月間検索量(手動実測): {out.search_volume:,}\n" if out.search_volume else ""
        )
        return (
            f"🔎 *VSEO動画アルゴリズム分析* 完了「{out.query}」"
            f"（上位{len(out.videos)}本／分析成功{ok}本{bf}）\n"
            f"{c.summary}{top}\n{volume_line}"
            f"{report_line}（タイムライン/テロップ位置/ブランド検出/勝ち筋）。{proposal_lines}\n"
            f"_概算 ${out.total_cost_usd:.4f}・n={c.video_count} の観測仮説（相関≠因果）_"
        )
