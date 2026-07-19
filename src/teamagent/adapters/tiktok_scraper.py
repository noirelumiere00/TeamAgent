"""TikTok 検索スクレイパ (Puppeteer 実ブラウザ + 内部 API 傍受)。

CLAUDE.md 6-bis Adapter 層。Skill からはこのモジュールの search_tiktok() のみ使う。

実体は Node.js (puppeteer-core) スクリプト `tools/tiktok_scraper/search.mjs`。
Apify 等の課金 SaaS を使わず、ローカルの Chrome を headless で起動して
TikTok の検索ページに遷移 → スクロール → 内部 API レスポンスを傍受して
動画メタ (再生数/いいね/作者/ハッシュタグ等) を取得する。

なぜ Node を subprocess で呼ぶか:
- 元々 EC2 で実証済みの TypeScript 実装 (vseo-analytics-web) を移植したもので、
  Stealth・スクロール・IntersectionObserver 誘発の繊細な挙動が作り込まれている。
  Python へフル移植すると bot 検出回避が壊れるリスクが高いため、動く実装を
  そのまま活かし、Python からは JSON I/F で薄く叩く。

前提:
- `node` が PATH にあること (TIKTOK_NODE_BIN で上書き可)。
- `tools/tiktok_scraper/` で `npm install` 済み (puppeteer-core)。
- Chrome/Chromium が存在すること (search.mjs が自動検出、CHROMIUM_PATH で上書き可)。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# このファイル: src/teamagent/adapters/tiktok_scraper.py
# tools/tiktok_scraper/search.mjs はリポジトリルート直下の tools/ にある。
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRAPER_DIR = _REPO_ROOT / "tools" / "tiktok_scraper"
_SCRAPER_SCRIPT = _SCRAPER_DIR / "search.mjs"

# 1 回の検索で待つ最大秒数 (ブラウザ起動 + スクロール数回で通常 10〜60s)
_DEFAULT_TIMEOUT_S = 120


class TikTokScrapeError(RuntimeError):
    """TikTok スクレイピング失敗。呼び出し側がユーザー向け案内に変換する。"""


@dataclass(frozen=True)
class TikTokAuthor:
    unique_id: str
    nickname: str
    follower_count: int


@dataclass(frozen=True)
class TikTokVideo:
    """検索で取れた 1 本の動画メタ。"""

    id: str
    url: str
    desc: str
    create_time: int
    duration: int
    cover_url: str
    author: TikTokAuthor
    play_count: int
    digg_count: int  # いいね
    comment_count: int
    share_count: int
    collect_count: int  # 保存
    hashtags: tuple[str, ...]
    music_title: str

    @property
    def engagement_rate(self) -> float:
        """エンゲージ率 (いいね+コメント+シェア+保存)/再生。再生 0 は 0。"""
        if self.play_count <= 0:
            return 0.0
        eng = self.digg_count + self.comment_count + self.share_count + self.collect_count
        return round(eng / self.play_count, 4)


@dataclass(frozen=True)
class TikTokComment:
    """動画 1 件のコメント。"""

    text: str
    likes: int
    author: str


@dataclass(frozen=True)
class TikTokCommentResult:
    """get_tiktok_comments の返り値。"""

    video_url: str
    comments: tuple[TikTokComment, ...]

    @property
    def count(self) -> int:
        return len(self.comments)


@dataclass(frozen=True)
class TikTokSearchResult:
    query: str
    search_type: str  # keyword | hashtag | keyword(fallback)
    videos: tuple[TikTokVideo, ...]

    @property
    def count(self) -> int:
        return len(self.videos)


def _node_bin() -> str:
    """node 実行ファイルを解決する。TIKTOK_NODE_BIN > PATH の node。"""
    explicit = os.environ.get("TIKTOK_NODE_BIN")
    if explicit:
        return explicit
    found = shutil.which("node")
    if not found:
        raise TikTokScrapeError(
            "TIKTOK_NODE_UNAVAILABLE: node が見つかりません。Node.js をインストールするか "
            "TIKTOK_NODE_BIN を設定してください"
        )
    return found


def _parse_video(d: dict[str, Any]) -> TikTokVideo:
    a = d.get("author") or {}
    s = d.get("stats") or {}
    music = d.get("music") or {}
    return TikTokVideo(
        id=str(d.get("id", "")),
        url=d.get("url", ""),
        desc=d.get("desc", ""),
        create_time=int(d.get("createTime", 0) or 0),
        duration=int(d.get("duration", 0) or 0),
        cover_url=d.get("coverUrl", ""),
        author=TikTokAuthor(
            unique_id=a.get("uniqueId", ""),
            nickname=a.get("nickname", ""),
            follower_count=int(a.get("followerCount", 0) or 0),
        ),
        play_count=int(s.get("playCount", 0) or 0),
        digg_count=int(s.get("diggCount", 0) or 0),
        comment_count=int(s.get("commentCount", 0) or 0),
        share_count=int(s.get("shareCount", 0) or 0),
        collect_count=int(s.get("collectCount", 0) or 0),
        hashtags=tuple(d.get("hashtags", []) or []),
        music_title=(music or {}).get("title", "") if isinstance(music, dict) else "",
    )


def _parse_media_post(post: dict[str, Any]) -> TikTokVideo:
    """generic media workerの正規化postを既存public schemaへ写像する。"""

    return TikTokVideo(
        id=str(post.get("pid") or ""),
        url=str(post.get("url") or ""),
        desc=str(post.get("title") or ""),
        create_time=int(post.get("create_time") or 0),
        duration=max(0, int(post.get("duration") or 0)),
        cover_url=str(post.get("cover_url") or ""),
        author=TikTokAuthor(
            unique_id=str(post.get("account_id") or ""),
            nickname=str(post.get("account_name") or ""),
            follower_count=int(post.get("followers") or 0),
        ),
        play_count=int(post.get("plays") or 0),
        digg_count=int(post.get("likes") or 0),
        comment_count=int(post.get("comments") or 0),
        share_count=int(post.get("shares") or 0),
        collect_count=int(post.get("saves") or 0),
        hashtags=tuple(
            str(value) for value in (post.get("hashtags") or ()) if isinstance(value, str)
        ),
        music_title=str(post.get("music_title") or ""),
    )


def search_tiktok(
    query: str,
    *,
    search_type: str = "keyword",
    max_videos: int = 10,
    request_id: str | None = None,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
    sessions: int | None = None,
) -> TikTokSearchResult:
    """TikTok をキーワード/ハッシュタグで検索し、上位動画のメタを返す。

    Args:
        query: 検索語 (例: "新宿 ランチ")。hashtag のときは "#" 抜きの語 (例: "新宿")。
        search_type: "keyword" か "hashtag"。hashtag が空振りしたら内部で keyword に
            フォールバックする (search.mjs 側)。
        max_videos: 取得する最大本数 (上位から)。
        request_id: トレース ID。
        timeout_s: subprocess の最大待ち秒。

    Raises:
        TikTokScrapeError: node 不在 / スクリプト不在 / タイムアウト / 0 件 等。
    """
    if not query.strip():
        raise TikTokScrapeError("TIKTOK_EMPTY_QUERY: 検索語が空です")
    from teamagent.adapters.media_job import MediaJobClient

    if MediaJobClient.is_configured():
        import hashlib

        normalized_query = query.strip()
        fingerprint = hashlib.sha256(
            (f"{request_id or 'no-request'}:{search_type}:{normalized_query}:{max_videos}").encode()
        ).hexdigest()
        try:
            posts = MediaJobClient().search_tiktok(
                normalized_query,
                request_fingerprint=f"tiktok-search:{fingerprint}",
                search_type=search_type,
                max_videos=max_videos,
                timeout_s=min(timeout_s, 15 * 60),
            )
        except Exception as exc:
            raise TikTokScrapeError(f"TIKTOK_MEDIA_JOB_FAILED: {type(exc).__name__}") from exc
        videos = tuple(_parse_media_post(post) for post in posts[:max_videos])
        if not videos:
            raise TikTokScrapeError("TIKTOK_EMPTY_RESULT: media worker returned no posts")
        return TikTokSearchResult(
            query=normalized_query,
            search_type=str(posts[0].get("search_type") or search_type),
            videos=videos,
        )
    if not MediaJobClient.local_runtime_enabled():
        raise TikTokScrapeError("TIKTOK_MEDIA_JOB_NOT_CONFIGURED")
    if not _SCRAPER_SCRIPT.exists():
        raise TikTokScrapeError(
            f"TIKTOK_SCRAPER_MISSING: {_SCRAPER_SCRIPT} がありません。"
            "tools/tiktok_scraper で npm install を実行してください"
        )

    node = _node_bin()
    if sessions is None:
        sessions = max(1, int(os.environ.get("TIKTOK_SESSIONS", "1") or "1"))
    # 複数セッションは1回あたり~60-70sかかるため timeout を比例拡大 (既定 sessions=1 では不変)
    if sessions > 1:
        timeout_s = max(timeout_s, sessions * 75)
    cmd = [
        node,
        str(_SCRAPER_SCRIPT),
        "--query",
        query,
        "--type",
        "hashtag" if search_type == "hashtag" else "keyword",
        "--max",
        str(max_videos),
        "--sessions",
        str(sessions),
    ]
    logger.info(
        "tiktok_search_start",
        request_id=request_id,
        search_type=search_type,
        max_videos=max_videos,
        query_len=len(query),
        sessions=sessions,
    )

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(_SCRAPER_DIR),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        logger.warning("tiktok_search_timeout", request_id=request_id, timeout_s=timeout_s)
        raise TikTokScrapeError(
            f"TIKTOK_TIMEOUT: 検索が {timeout_s}s 以内に終わりませんでした"
        ) from e

    # stdout は JSON のみ (ブラウザログは stderr)。空なら異常。
    stdout = (proc.stdout or "").strip()
    if not stdout:
        logger.warning(
            "tiktok_search_no_output",
            request_id=request_id,
            returncode=proc.returncode,
            stderr_tail=(proc.stderr or "")[-300:],
        )
        raise TikTokScrapeError(
            "TIKTOK_NO_OUTPUT: スクレイパが結果を返しませんでした "
            f"(exit={proc.returncode})。Chrome 未インストール等の可能性"
        )

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        # stdout の先頭だけログ (生データを丸ごと残さない)
        logger.warning("tiktok_search_bad_json", request_id=request_id, head=stdout[:200])
        raise TikTokScrapeError("TIKTOK_BAD_JSON: スクレイパ出力を解析できませんでした") from e

    if not payload.get("ok"):
        err = payload.get("error") or "不明なエラー"
        error_code = payload.get("errorCode") or "TIKTOK_EMPTY_RESULT"
        diag = payload.get("diag") or {}
        # 空振りの原因切り分け用にブラウザ層 stderr と診断を必ず残す
        # (captcha壁 / 真の0件 / API形状変更 を推測でなくログで判別できるようにする)
        logger.warning(
            "tiktok_search_empty",
            request_id=request_id,
            error=err,
            error_code=error_code,
            diag=diag,
            stderr_tail=(proc.stderr or "")[-600:],
        )
        msg = err if err.startswith(error_code) else f"{error_code}: {err}"
        raise TikTokScrapeError(msg)

    videos = tuple(_parse_video(v) for v in payload.get("videos", []))
    result = TikTokSearchResult(
        query=query,
        search_type=str(payload.get("type", search_type)),
        videos=videos,
    )
    logger.info(
        "tiktok_search_done",
        request_id=request_id,
        count=result.count,
        search_type=result.search_type,
        diag=payload.get("diag") or {},
    )
    return result


def get_tiktok_comments(
    video_url: str,
    *,
    max_comments: int = 50,
    request_id: str | None = None,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
) -> TikTokCommentResult:
    """TikTok 動画 URL からコメントを取得する (コメント API を傍受)。

    Args:
        video_url: TikTok 動画 URL (例: https://www.tiktok.com/@user/video/123)
        max_comments: 取得する最大コメント数
        request_id: トレース ID
        timeout_s: subprocess の最大待ち秒

    Raises:
        TikTokScrapeError: URL 不正 / node 不在 / タイムアウト / 0 件 等。
    """
    # §N: 部分文字列チェック（`?x=tiktok.com` で突破可）を SSRF allowlist 検証に置換。
    from teamagent.adapters.url_guard import UrlGuardError, validate_scrape_url

    try:
        video_url = validate_scrape_url(video_url, request_id=request_id)
    except UrlGuardError as e:
        raise TikTokScrapeError(f"TIKTOK_INVALID_URL: {e}") from e
    from teamagent.adapters.media_job import MediaJobClient

    if not MediaJobClient.local_runtime_enabled():
        # The production comment-mining skill catches this explicit boundary
        # and uses its existing bounded Apify route.  Never fall through to a
        # Node/Chromium binary that is intentionally absent from core.
        raise TikTokScrapeError("TIKTOK_COMMENTS_LOCAL_RUNTIME_DISABLED")
    if not _SCRAPER_SCRIPT.exists():
        raise TikTokScrapeError(f"TIKTOK_SCRAPER_MISSING: {_SCRAPER_SCRIPT} がありません")

    node = _node_bin()
    cmd = [
        node,
        str(_SCRAPER_SCRIPT),
        "--mode",
        "comments",
        "--url",
        video_url,
        "--max-comments",
        str(max_comments),
    ]
    logger.info(
        "tiktok_comments_start",
        request_id=request_id,
        max_comments=max_comments,
        url_len=len(video_url),
    )

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(_SCRAPER_DIR),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        logger.warning("tiktok_comments_timeout", request_id=request_id, timeout_s=timeout_s)
        raise TikTokScrapeError(
            f"TIKTOK_TIMEOUT: コメント取得が {timeout_s}s 以内に終わりませんでした"
        ) from e

    stdout = (proc.stdout or "").strip()
    if not stdout:
        logger.warning(
            "tiktok_comments_no_output",
            request_id=request_id,
            returncode=proc.returncode,
            stderr_tail=(proc.stderr or "")[-300:],
        )
        raise TikTokScrapeError(
            f"TIKTOK_NO_OUTPUT: スクレイパが結果を返しませんでした (exit={proc.returncode})"
        )

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as e:
        logger.warning("tiktok_comments_bad_json", request_id=request_id, head=stdout[:200])
        raise TikTokScrapeError("TIKTOK_BAD_JSON: スクレイパ出力を解析できませんでした") from e

    if not payload.get("ok"):
        err = payload.get("error") or "不明なエラー"
        logger.info("tiktok_comments_empty", request_id=request_id, error=err)
        raise TikTokScrapeError(f"TIKTOK_EMPTY_RESULT: {err}")

    comments = tuple(
        TikTokComment(
            text=c.get("text", ""),
            likes=int(c.get("likes", 0) or 0),
            author=c.get("author", ""),
        )
        for c in payload.get("comments", [])
    )
    result = TikTokCommentResult(video_url=video_url, comments=comments)
    logger.info("tiktok_comments_done", request_id=request_id, count=result.count)
    return result


def download_tiktok_video(
    video_url: str,
    *,
    request_id: str | None = None,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
    _skip_url_guard: bool = False,
) -> tuple[bytes, str]:
    """search.mjs --mode download でブラウザ内DLし (動画bytes, mime) を返す。

    検索と同一の Puppeteer 経路で playAddr を確定し、ブラウザ自身（署名/Cookie/UA/proxy を
    自前管理）にバイトを取らせる。会社プロキシ下で yt-dlp が SSL/Unable to extract で落ちる
    動画の primary 経路。取得した動画は一時ディレクトリに保存し、bytes を読んだら即破棄する
    （ToS 配慮・ディスクに残さない / video_download.py と同方針）。

    `_skip_url_guard=True` は download_video_chained が外側で SSRF 検証済みのときに使う
    （二重 DNS 解決の回避）。直接呼ぶ場合は False のまま＝必ず SSRF 関門を通す。

    Raises:
        TikTokScrapeError: URL不正(SSRF) / node不在 / スクリプト不在 / タイムアウト / 取得失敗。
    """
    from teamagent.adapters.media_job import MediaJobClient

    if MediaJobClient.is_configured():
        import hashlib

        fingerprint = hashlib.sha256(video_url.encode("utf-8")).hexdigest()
        try:
            return MediaJobClient().acquire_video(
                video_url,
                request_fingerprint=f"{request_id or 'no-request'}:download:{fingerprint}",
                timeout_s=min(timeout_s, 15 * 60),
            )
        except Exception as exc:
            raise TikTokScrapeError(f"TIKTOK_MEDIA_JOB_FAILED: {type(exc).__name__}") from exc
    if not MediaJobClient.local_runtime_enabled():
        raise TikTokScrapeError("TIKTOK_MEDIA_JOB_NOT_CONFIGURED")
    if not _skip_url_guard:
        from teamagent.adapters.url_guard import UrlGuardError, validate_scrape_url

        try:
            video_url = validate_scrape_url(video_url, request_id=request_id)
        except UrlGuardError as e:
            raise TikTokScrapeError(f"TIKTOK_INVALID_URL: {e}") from e
    if not _SCRAPER_SCRIPT.exists():
        raise TikTokScrapeError(f"TIKTOK_SCRAPER_MISSING: {_SCRAPER_SCRIPT} がありません")

    node = _node_bin()
    with tempfile.TemporaryDirectory(prefix="teamagent_btdl_") as tmpdir:
        out_path = os.path.join(tmpdir, "v.mp4")
        cmd = [
            node,
            str(_SCRAPER_SCRIPT),
            "--mode",
            "download",
            "--url",
            video_url,
            "--out",
            out_path,
        ]
        logger.info("tiktok_download_start", request_id=request_id, url_len=len(video_url))

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(_SCRAPER_DIR),
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            logger.warning("tiktok_download_timeout", request_id=request_id, timeout_s=timeout_s)
            raise TikTokScrapeError(
                f"TIKTOK_TIMEOUT: 動画取得が {timeout_s}s 以内に終わりませんでした"
            ) from e

        stdout = (proc.stdout or "").strip()
        if not stdout:
            logger.warning(
                "tiktok_download_no_output",
                request_id=request_id,
                returncode=proc.returncode,
                stderr_tail=(proc.stderr or "")[-300:],
            )
            raise TikTokScrapeError(
                f"TIKTOK_NO_OUTPUT: スクレイパが結果を返しませんでした (exit={proc.returncode})"
            )

        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as e:
            logger.warning("tiktok_download_bad_json", request_id=request_id, head=stdout[:200])
            raise TikTokScrapeError("TIKTOK_BAD_JSON: スクレイパ出力を解析できませんでした") from e

        if not payload.get("ok"):
            err = payload.get("error") or "不明なエラー"
            logger.info("tiktok_download_failed", request_id=request_id, error=err)
            raise TikTokScrapeError(f"TIKTOK_DL_FAILED: {err}")

        saved = payload.get("savedTo") or out_path
        try:
            with open(saved, "rb") as f:
                data = f.read()
        except OSError as e:
            raise TikTokScrapeError(
                f"TIKTOK_DL_FAILED: 保存ファイルを読めません ({type(e).__name__})"
            ) from e
        if not data:
            raise TikTokScrapeError("TIKTOK_DL_FAILED: 取得した動画が空でした")
        mime = str(payload.get("mime") or "video/mp4").split(";")[0].strip()
        if not mime.startswith("video/"):
            mime = "video/mp4"

    logger.info(
        "tiktok_download_done",
        request_id=request_id,
        size_mb=round(len(data) / 1024 / 1024, 2),
        mime=mime,
    )
    return data, mime
