"""VSEO データ準備パイプライン (オーケストレーション層)。

複数KW → tiktok_search で各KW検索 → VSEO スキルが食う JSON 群を生成 →
プロジェクトディレクトリに書き出す。これにより、人手の「ラッコ検索→上位30本を
個別取得→Excel化」を完全自動化する。

出力ディレクトリ構成 (VSEO スキル build_proposal.js が読む形):
    <project_dir>/
      top10_with_urls.json
      multi_kw_videos.json
      kw_stats.json
      covers/<kw>/rankNN.jpeg   (任意・画像DL成功分)
      _meta.json               (生成メタ: KW・取得本数・生成時刻)

注意: kw50_categorized.json (ラッコ検索量) は TikTok スクレイパの管轄外なので
ここでは生成しない (VSEO スキル側でラッコから取得する既存フロー)。

3 層分離: Skill 層。検索は adapters/tiktok_scraper、変換は dataprep、画像は covers。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from teamagent.adapters.tiktok_scraper import TikTokScrapeError, search_tiktok
from teamagent.skills.vseo import dataprep
from teamagent.skills.vseo.covers import download_covers

logger = structlog.get_logger(__name__)

# search_tiktok の型 (テストで差し替え可能にする)
Searcher = Callable[..., Any]


@dataclass(frozen=True)
class VseoPrepResult:
    """VSEO データ準備の結果サマリ。"""

    project_dir: str
    keywords: list[str]
    counts: dict[str, int]  # KW → 取得本数
    multi_kw_count: int  # マルチKW入賞動画数
    covers_saved: int
    failed_keywords: list[str]  # 検索失敗した KW


def prepare_vseo_data(
    keywords: list[str],
    project_dir: Path,
    *,
    max_videos: int = 30,
    now_ts: int,
    download_thumbnails: bool = True,
    request_id: str | None = None,
    searcher: Searcher | None = None,
) -> VseoPrepResult:
    """複数KWを検索して VSEO スキル用 JSON 群を project_dir に書き出す。

    Args:
        keywords: 検索KWリスト (通常5個)。
        project_dir: 出力先 (無ければ作成)。
        max_videos: 各KWで取得する最大本数 (VSEO は上位30本前提)。
        now_ts: 統計の直近判定基準時刻 (Unix秒)。
        download_thumbnails: サムネ画像をDLするか。
        request_id: トレース ID。
        searcher: search_tiktok の差し替え (テスト用)。

    Returns:
        VseoPrepResult。検索が一部失敗しても、成功分で JSON を生成する
        (全滅時のみ TikTokScrapeError を上げる)。
    """
    search = searcher or search_tiktok
    project_dir.mkdir(parents=True, exist_ok=True)

    results: list[dataprep.KwResult] = []
    failed: list[str] = []
    for kw in keywords:
        try:
            res = search(kw, search_type="keyword", max_videos=max_videos, request_id=request_id)
            results.append(dataprep.KwResult(keyword=kw, videos=list(res.videos)))
            logger.info("vseo_kw_searched", request_id=request_id, kw_len=len(kw), n=res.count)
        except TikTokScrapeError:
            failed.append(kw)
            logger.warning("vseo_kw_failed", request_id=request_id, kw_len=len(kw))

    if not results:
        raise TikTokScrapeError("TIKTOK_EMPTY_RESULT: 全KWの検索が失敗しました")

    # JSON 群を生成
    top10 = dataprep.build_top10(results, top_n=10)
    multi = dataprep.build_multi_kw(results, top_n=max_videos)
    stats = dataprep.stats_to_dict(dataprep.compute_stats(results, now_ts=now_ts))

    _write_json(project_dir / "top10_with_urls.json", top10)
    _write_json(project_dir / "multi_kw_videos.json", multi)
    _write_json(project_dir / "kw_stats.json", stats)

    # サムネ画像 DL (任意)
    covers_saved = 0
    if download_thumbnails:
        covers_root = project_dir / "covers"
        for kw, entries in top10.items():
            # KW 名をディレクトリ名に使う (スラッシュ等は置換)
            safe = kw.replace("/", "_").replace(" ", "_")
            saved = download_covers(entries, covers_root / safe, request_id=request_id)
            covers_saved += len(saved)

    counts = {r.keyword: len(r.videos) for r in results}
    meta = {
        "keywords": keywords,
        "counts": counts,
        "multi_kw_count": len(multi),
        "covers_saved": covers_saved,
        "failed_keywords": failed,
        "now_ts": now_ts,
        "max_videos": max_videos,
    }
    _write_json(project_dir / "_meta.json", meta)

    logger.info(
        "vseo_prepare_done",
        request_id=request_id,
        kw_count=len(keywords),
        success=len(results),
        multi_kw=len(multi),
        covers=covers_saved,
    )
    return VseoPrepResult(
        project_dir=str(project_dir),
        keywords=keywords,
        counts=counts,
        multi_kw_count=len(multi),
        covers_saved=covers_saved,
        failed_keywords=failed,
    )


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
