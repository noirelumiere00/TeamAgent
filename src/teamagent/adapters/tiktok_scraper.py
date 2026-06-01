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


def search_tiktok(
    query: str,
    *,
    search_type: str = "keyword",
    max_videos: int = 10,
    request_id: str | None = None,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
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
    if not _SCRAPER_SCRIPT.exists():
        raise TikTokScrapeError(
            f"TIKTOK_SCRAPER_MISSING: {_SCRAPER_SCRIPT} がありません。"
            "tools/tiktok_scraper で npm install を実行してください"
        )

    node = _node_bin()
    cmd = [
        node,
        str(_SCRAPER_SCRIPT),
        "--query",
        query,
        "--type",
        "hashtag" if search_type == "hashtag" else "keyword",
        "--max",
        str(max_videos),
    ]
    logger.info(
        "tiktok_search_start",
        request_id=request_id,
        search_type=search_type,
        max_videos=max_videos,
        query_len=len(query),
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
        logger.info("tiktok_search_empty", request_id=request_id, error=err)
        raise TikTokScrapeError(f"TIKTOK_EMPTY_RESULT: {err}")

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
    if not video_url.strip() or "tiktok.com" not in video_url:
        raise TikTokScrapeError("TIKTOK_INVALID_URL: 有効な TikTok 動画 URL ではありません")
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
