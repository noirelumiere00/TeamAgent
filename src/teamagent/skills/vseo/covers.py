"""VSEO 用サムネ画像ダウンロード (壁A の解消)。

VSEO スキルの thumbnail_features.py / build_proposal.js は、サムネを
ローカルの rankNN.jpeg (Excel 埋め込み画像から抽出したもの) として期待する。
tiktok_search は cover_url (TikTok CDN の署名付き URL) しか返さないため、
ここで URL から画像を DL してローカル保存し、後工程が食える形にする。

署名付き URL は有効期限・hotlink 制限があるため、検索取得直後に DL するのが安全。
3 層分離: Skill 層 (HTTP は httpx を直接使うが、画像 DL は軽量なのでここで完結)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)


def download_covers(
    entries: list[dict[str, Any]],
    out_dir: Path,
    *,
    request_id: str | None = None,
    timeout_s: float = 20.0,
) -> dict[int, Path]:
    """top10 エントリの cover_url を rankNN.jpeg として DL する。

    Args:
        entries: build_top10 が返す 1 KW 分のエントリ列 (rank, cover_url を持つ)。
        out_dir: 保存先ディレクトリ (rank01.jpeg, rank02.jpeg, ...)。
        request_id: トレース ID。
        timeout_s: 1 枚あたりのタイムアウト。

    Returns:
        rank → 保存パス の dict (DL 成功分のみ)。失敗は黙ってスキップ。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: dict[int, Path] = {}
    headers = {
        # TikTok CDN は Referer/UA が無いと 403 を返すことがある
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.tiktok.com/",
    }
    with httpx.Client(timeout=timeout_s, headers=headers, follow_redirects=True) as client:
        for e in entries:
            url = e.get("cover_url")
            rank = e.get("rank")
            if not url or rank is None:
                continue
            try:
                resp = client.get(url)
                resp.raise_for_status()
                path = out_dir / f"rank{int(rank):02d}.jpeg"
                path.write_bytes(resp.content)
                saved[int(rank)] = path
            except Exception:
                # 署名切れ・403 等は黙ってスキップ (提案書は画像欠落でも生成可)
                logger.warning("vseo_cover_dl_failed", request_id=request_id, rank=rank)
                continue
    logger.info(
        "vseo_covers_downloaded",
        request_id=request_id,
        saved=len(saved),
        total=len(entries),
    )
    return saved
