"""VSEO 提案書スキルのデータ準備を自動化する。

VSEO スキル (~/.claude/skills/tiktok-vseo-proposal/) は本来、人が手作業で
「5KW それぞれの TikTok 上位30動画を個別取得 → Excel 化」してから分析する。
このモジュールは、既存の tiktok_search (Puppeteer 実ブラウザスクレイパ) の
結果から、VSEO スキルが食う JSON を**直接生成**して Excel 工程をバイパスする。

生成する JSON (VSEO スキル references/data_schemas.md 準拠):
- top10_with_urls.json: 各KWの上位10動画 (build_proposal.js p7-p11)
- multi_kw_videos.json: 複数KWで上位入りした動画 (build_proposal.js p12/p13)
- kw_stats.json: 各KWの統計 (analyze_top_videos.py 相当: avg/median/breakout/花王仮説)

3 層分離: Skill 層。検索は adapters/tiktok_scraper.py の search_tiktok() を使う。
サムネ画像 DL は dl_covers() で別途 (cover_url は署名付き CDN URL のため取得直後に DL)。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from teamagent.adapters.tiktok_scraper import TikTokVideo


@dataclass
class KwResult:
    """1 KW 分の検索結果 (KW 名 + 取得動画)。"""

    keyword: str
    videos: list[TikTokVideo]


def _video_to_top10_entry(v: TikTokVideo, rank: int) -> dict[str, Any]:
    """TikTokVideo → top10_with_urls.json の 1 エントリ。"""
    return {
        "rank": rank,
        "video_url": v.url,
        "account_name": v.author.nickname,
        "account_id": v.author.unique_id,
        "account_url": f"https://www.tiktok.com/@{v.author.unique_id}",
        "plays": v.play_count,
        "followers": v.author.follower_count,
        # eng_rate は % 表記 (VSEO スキルの慣習。engagement_rate は比率なので ×100)
        "eng_rate": round(v.engagement_rate * 100, 2),
        "title_short": (v.desc or "").replace("\n", " ")[:50],
        "cover_url": v.cover_url,  # 画像 DL 用 (VSEO スキーマには無いが後工程で使う)
    }


def build_top10(results: list[KwResult], *, top_n: int = 10) -> dict[str, list[dict[str, Any]]]:
    """各KWの上位 top_n を top10_with_urls.json 形式で返す。

    KW 名をキーに、上位動画のリストを値とする dict。
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        entries = [_video_to_top10_entry(v, i + 1) for i, v in enumerate(r.videos[:top_n])]
        out[r.keyword] = entries
    return out


def build_multi_kw(results: list[KwResult], *, top_n: int = 30) -> list[dict[str, Any]]:
    """複数KWで上位 top_n 入りした動画を multi_kw_videos.json 形式で返す。

    identify_multi_kw.py のロジックを Excel ではなく TikTokVideo リスト入力に移植。
    URL をキーに、各KWでの (KW名, rank) を集約。2KW 以上を対象、出現数→再生数で降順。
    """
    url_kws: dict[str, list[list[Any]]] = defaultdict(list)
    url_db: dict[str, dict[str, Any]] = {}

    for r in results:
        for rank, v in enumerate(r.videos[:top_n], 1):
            if not v.url:
                continue
            url_kws[v.url].append([r.keyword, rank])
            if v.url not in url_db:
                url_db[v.url] = {
                    "title": (v.desc or "").replace("\n", " ")[:50],
                    "account_name": v.author.nickname,
                    "account_id": v.author.unique_id,
                    "followers": v.author.follower_count,
                    "plays": v.play_count,
                }

    multi: list[dict[str, Any]] = []
    for url, kws in url_kws.items():
        if len(kws) >= 2:
            meta = url_db[url]
            multi.append(
                {
                    "url": url,
                    "kws": kws,
                    "n_kws": len(kws),
                    "title": meta["title"],
                    "account_name": meta["account_name"],
                    "account_id": meta["account_id"],
                    "account_url": f"https://www.tiktok.com/@{meta['account_id']}",
                    "followers": meta["followers"],
                    "plays": meta["plays"],
                }
            )

    multi.sort(key=lambda x: (-x["n_kws"], -x["plays"]))
    return multi


@dataclass
class KwStats:
    """1 KW の統計 (analyze_top_videos.py 相当)。"""

    keyword: str
    n: int
    avg_plays_all: int
    avg_plays_clean: int  # 50万再生超の外れ値を除外
    median_plays: int
    median_eng: float
    recent_count: int  # 直近60日公開数
    breakouts: list[dict[str, Any]] = field(default_factory=list)  # フォロワー<5万 & 再生>=10万
    kao_hypothesis_hits: int = 0  # 10万再生 & ENG>=1%


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def compute_stats(results: list[KwResult], *, now_ts: int, recent_days: int = 60) -> list[KwStats]:
    """各KWの統計を計算する。

    now_ts は基準時刻 (Unix 秒)。create_time(Unix秒) と比較して直近公開を判定する。
    workflow 環境では datetime.now() が使えないため、呼び出し側が now_ts を渡す。
    """
    cutoff = now_ts - recent_days * 86400
    stats: list[KwStats] = []
    for r in results:
        rows = [v for v in r.videos if v.play_count > 0]
        plays_all = [v.play_count for v in rows]
        plays_clean = [p for p in plays_all if p < 500000]
        engs = [v.engagement_rate * 100 for v in rows]
        breakouts = [
            {
                "url": v.url,
                "account_name": v.author.nickname,
                "plays": v.play_count,
                "followers": v.author.follower_count,
                "eng_rate": round(v.engagement_rate * 100, 2),
            }
            for v in rows
            if v.author.follower_count < 50000 and v.play_count >= 100000
        ]
        stats.append(
            KwStats(
                keyword=r.keyword,
                n=len(rows),
                avg_plays_all=int(sum(plays_all) / len(plays_all)) if plays_all else 0,
                avg_plays_clean=int(sum(plays_clean) / len(plays_clean)) if plays_clean else 0,
                median_plays=int(_median([float(p) for p in plays_all])),
                median_eng=round(_median(engs), 2),
                recent_count=sum(1 for v in rows if v.create_time and v.create_time >= cutoff),
                breakouts=breakouts,
                kao_hypothesis_hits=sum(
                    1 for v in rows if v.play_count >= 100000 and v.engagement_rate * 100 >= 1
                ),
            )
        )
    return stats


def stats_to_dict(stats: list[KwStats]) -> dict[str, Any]:
    """KwStats のリストを kw_stats.json 形式 (KW 名キーの dict) に変換。"""
    return {
        s.keyword: {
            "n": s.n,
            "avg_plays_all": s.avg_plays_all,
            "avg_plays_clean": s.avg_plays_clean,
            "median_plays": s.median_plays,
            "median_eng": s.median_eng,
            "recent_count": s.recent_count,
            "breakouts": s.breakouts,
            "kao_hypothesis_hits": s.kao_hypothesis_hits,
        }
        for s in stats
    }


def utc_now_ts() -> int:
    """現在時刻の Unix 秒 (UTC)。workflow 外の通常実行用。"""
    return int(datetime.now(UTC).timestamp())
