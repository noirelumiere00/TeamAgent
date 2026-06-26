"""VideoAlgorithm Skill 本体（VSEO 動画アルゴリズム読み解き）。

検索KW → 上位N本（tiktok_search）→ 各動画 download→proxy→Gemini構造分析 → 5本横断
→ HTML タイムラインレポート + Slack 要約。

3層分離: Skill 層。検索/取得/圧縮/Gemini は adapters。重い I/O は ThreadPool で並列化。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel, ValidationError

from teamagent.adapters.gemini_client import GeminiClient
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

    # --- 依存の遅延解決（テスト差し替え可） ---
    def _client(self) -> GeminiClient:
        if self._gemini is None:
            self._gemini = GeminiClient.from_env()
        return self._gemini

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
                    engagement_rate=float(p.get("eg_rate", 0.0) or 0.0) / 100.0,
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
                    engagement_rate=float(getattr(v, "engagement_rate", 0.0) or 0.0),
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
        # 3層DLチェーン（ブラウザ内DL→yt-dlp→…）。全滅時は _analyze_one が cover-only へ縮退。
        from teamagent.adapters.video_download import download_video_chained

        return download_video_chained(url, request_id=request_id)

    def _shrink(self, data: bytes, mime: str, request_id: str) -> tuple[bytes, str]:
        if self._proxy is not None:
            return self._proxy(data, mime)
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
                shots = extract_frames(
                    data, mime, [s for s, _ in tcs], width=320, request_id=request_id
                )
                frames = [
                    FrameShot(sec=s, caption=cap_by_sec.get(round(s, 1), f"{s:.0f}s"), data_uri=uri)
                    for s, uri in shots
                ]
        # サムネ色（検索一覧タイル）: cover_url を取得、失敗時は先頭フレームを流用
        cover_uri, thumb = self._build_thumb(meta.cover_url, frames, request_id)
        # タイムラインで実再生する軽量Webプレビュー動画（~480p・graceful。失敗時は静止フレーム）
        video_uri = ""
        if analysis is not None:
            from teamagent.adapters.video_proxy import make_web_preview

            video_uri = make_web_preview(data, mime, request_id=request_id)
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
        from teamagent.skills.video_algorithm.thumbnails import fetch_cover

        cover = fetch_cover(meta.cover_url, request_id=request_id)
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
        from teamagent.skills.video_algorithm.thumbnails import analyze_cover, build_thumb

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

        target = input.max_videos  # 深掘り分析（DL+Gemini）する本数。重い。
        # 取得（スクレイプ）= 上位ボード board_size 本。メタのみ＝軽い。
        # 深掘り分析の予備候補も兼ねる（DL/分析失敗を後続候補でバックフィル）。天井は _MAX_POOL。
        board_target = min(max(input.board_size, target + self._overfetch_buffer), _MAX_POOL)

        # 取得段の委譲: acquire_s3_prefix があれば tiktok_acquire 成果物(S3)から読む(スクレイプ無)。
        # per-call override はローカルで組み立て self へ保存しない(共有インスタンス安全)。
        call_searcher: Searcher | None = None
        call_downloader: Downloader | None = None
        if input.acquire_s3_prefix:
            from teamagent.adapters.tiktok_s3_source import TikTokS3Source

            _src = TikTokS3Source(input.acquire_s3_prefix)

            def _s3_search(q: str, n: int, rid: str) -> list[VideoMeta]:
                return self._posts_to_metas(_src.posts(n))

            call_searcher = _s3_search
            call_downloader = _src.download
            log.info("video_algorithm_s3_source", prefix=input.acquire_s3_prefix[:60])

        pool = self._search(input.query, board_target, ctx.request_id, searcher=call_searcher)
        if not pool:
            return VideoAlgorithmOutput(
                query=input.query,
                slack_summary=f"🔎 「{input.query}」の検索結果を取得できませんでした。",
            )

        system = load_prompt("video_algorithm", self._prompt_version, "system")
        # 上位から波状に分析し、成功が target 本に達するか候補が尽きるまで（再検索はしない）
        results: list[AnalyzedVideo] = []
        attempted = 0
        while sum(1 for v in results if v.analysis) < target and attempted < len(pool):
            need = target - sum(1 for v in results if v.analysis)
            batch = pool[attempted : attempted + need]
            attempted += len(batch)
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

        cross = cross_analyze(analyzed, input.query)
        total_cost = round(sum(v.cost_usd for v in results), 6)  # 全試行の課金を計上
        # 横断シンセシス（Gemini 2nd pass・概念の関連性）。≥2本でのみ実行
        if sum(1 for v in analyzed if v.analysis) >= 2:
            from teamagent.skills.video_algorithm.synthesis import synthesize

            syn, syn_cost = synthesize(
                self._client(),
                analyzed,
                input.query,
                request_id=ctx.request_id,
                stats=cross.stats,
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
        )
        out.report_html_path = self._write_report(out, ctx.request_id)
        if out.report_html_path:
            # §M: 金庫外の OpenClaw 等が読めるよう、非公開S3へ発行して署名URLを出力に載せる。
            out.report_url = self._publish(out.report_html_path, ctx.request_id, input.query)
        # §Q-HTML→PPTX: 提案資料組み込み用の追加成果物（要求時のみ・graceful＝本体分析は壊さない）。
        if "slides" in input.outputs or "pptx" in input.outputs:
            self._build_proposal_outputs(out, input, ctx.request_id)
        out.slack_summary = self._slack_summary(out, backfilled)
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
        return out

    def _publish(self, path: str, request_id: str, query: str) -> str | None:
        """ローカル HTML レポートを非公開S3へ発行し署名URL(7日)を返す（失敗は None＝graceful）。

        注入 publisher があればそれを使う（テスト差し替え）。無ければ VSEO_REPORT_BUCKET 設定時のみ
        既定実装を遅延使用＝ローカル/テスト（bucket未設定）では S3 を叩かず None。
        """
        if self._publisher is not None:
            return self._publisher(path, request_id=request_id, query=query)
        if not os.environ.get("VSEO_REPORT_BUCKET"):
            return None
        from teamagent.adapters.report_publish import publish_html_file

        return publish_html_file(path, request_id=request_id, query=query)

    def _build_proposal_outputs(
        self, out: VideoAlgorithmOutput, input: VideoAlgorithmInput, request_id: str
    ) -> None:
        """提案資料向け slides(HTML)/pptx を生成しS3署名URLを out に載せる（全工程graceful）。

        - slides: render_slides(out) を S3 へ（HTML・編集可・営業がブラウザで直す）。
        - pptx:   slides を playwright で要素スクショ→python-pptx→S3（拡張版イメージの chromium）。
        どの段が失敗しても本体分析(out)は壊さない＝報告だけ残して None のまま進む。
        """
        report_dir = self._report_dir or os.path.join(os.getcwd(), ".local_out", "vseo_reports")
        safe = re.sub(r"[^\w]+", "_", out.query).strip("_")[:40] or "kw"
        if "slides" in input.outputs or "pptx" in input.outputs:
            try:
                from teamagent.skills.video_algorithm.slides import render_slides

                os.makedirs(report_dir, exist_ok=True)
                spath = os.path.join(report_dir, f"vseo_slides_{safe}_{uuid.uuid4().hex[:8]}.html")
                with open(spath, "w", encoding="utf-8") as f:
                    f.write(render_slides(out))
                if "slides" in input.outputs:
                    out.slides_url = self._publish_artifact(
                        spath, request_id, out.query, kind="slides"
                    )
                if "pptx" in input.outputs:
                    out.pptx_url = self._build_pptx(out, report_dir, safe, request_id)
            except Exception:
                logger.warning("vseo_proposal_outputs_failed", request_id=request_id)

    def _build_pptx(
        self, out: VideoAlgorithmOutput, report_dir: str, safe: str, request_id: str
    ) -> str | None:
        try:
            from teamagent.skills.video_algorithm.pptx_export import render_pptx

            ppath = os.path.join(report_dir, f"vseo_proposal_{safe}_{uuid.uuid4().hex[:8]}.pptx")
            if render_pptx(out, ppath) is None:
                return None
            return self._publish_artifact(ppath, request_id, out.query, kind="pptx")
        except Exception:
            logger.warning("vseo_pptx_build_failed", request_id=request_id)
            return None

    def _publish_artifact(self, path: str, request_id: str, query: str, *, kind: str) -> str | None:
        """slides/pptx を非公開S3へ発行（publisher 注入優先・bucket未設定なら None）。"""
        if self._publisher is not None:
            return self._publisher(path, request_id=request_id, query=query)
        if not os.environ.get("VSEO_REPORT_BUCKET"):
            return None
        from teamagent.adapters.report_publish import publish_html_file, publish_pptx_file

        fn = publish_pptx_file if kind == "pptx" else publish_html_file
        return fn(path, request_id=request_id, query=query)

    def _write_report(self, out: VideoAlgorithmOutput, request_id: str) -> str | None:
        try:
            report_dir = self._report_dir or os.path.join(os.getcwd(), ".local_out", "vseo_reports")
            os.makedirs(report_dir, exist_ok=True)
            safe = re.sub(r"[^\w]+", "_", out.query).strip("_")[:40] or "kw"
            path = os.path.join(report_dir, f"vseo_{safe}_{uuid.uuid4().hex[:8]}.html")
            html = render_report(out)
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
            return path
        except Exception:
            logger.warning("video_algorithm_report_write_failed", request_id=request_id)
            return None

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
        return (
            f"🔎 *VSEO動画アルゴリズム分析* 完了「{out.query}」"
            f"（上位{len(out.videos)}本／分析成功{ok}本{bf}）\n"
            f"{c.summary}{top}\n"
            f"{report_line}（タイムライン/テロップ位置/ブランド検出/勝ち筋）。{proposal_lines}\n"
            f"_概算 ${out.total_cost_usd:.4f}・n={c.video_count} の観測仮説（相関≠因果）_"
        )
