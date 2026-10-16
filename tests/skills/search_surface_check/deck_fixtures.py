"""レポートの PowerPoint（DeckSpec）のテスト用データ。

面は fixtures.py の「スパイスカレー 作り方」TikTok 上位 15 本（本番の取得結果に仮の値を足したもの）を
SurfacePost にしたもの。タイプは本番の分類器の答え（CLASSIFY_BY_ACCOUNT）を skill と同じ規則で直す。
表紙は stdlib で作る小さな PNG（9:16 と 3:4 の 2 種類＝切り取り 0 の確認用）。
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Sequence

from teamagent.skills.search_surface_check.insights import compute_facts, is_pr_post, mentions
from teamagent.skills.search_surface_check.schema import (
    ConclusionPoint,
    KwSurface,
    SurfaceConclusion,
    SurfacePost,
)
from teamagent.skills.search_surface_check.skill import _category
from tests.skills.search_surface_check.fixtures import CLASSIFY_BY_ACCOUNT, KEYWORD, NOW, s3_rows

CLIENT = "クラシル"
REPORT_ID = "ss-test-0001"


def png(width: int, height: int, rgb: tuple[int, int, int] = (200, 180, 160)) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def posts(
    n: int = 15,
    *,
    platform: str = "tiktok",
    keyword: str = KEYWORD,
    classify: bool = True,
    client_name: str | None = CLIENT,
) -> list[SurfacePost]:
    rows = s3_rows(keyword)
    out: list[SurfacePost] = []
    for i in range(n):
        row = rows[i % len(rows)]
        rank = i + 1
        ig = platform == "instagram"
        followers = 0 if ig else int(row["followers"])
        post = SurfacePost(
            platform=platform,  # type: ignore[arg-type]
            keyword=keyword,
            rank=rank,
            url=row["url"].replace(
                str(7_400_000_000_000_000_000 + row["rank_display"]),
                str(7_400_000_000_000_000_000 + rank),
            ),
            author=row["account_id"],
            author_name="" if ig else row["account_name"],
            author_followers=followers,
            desc=row["title"],
            hashtags=[] if ig else row["hashtags"],
            play_count=0 if ig and i % 3 != 0 else row["plays"],
            like_count=row["likes"],
            comment_count=row["comments"],
            share_count=0 if ig else row["shares"],
            save_count=0 if ig else row["saves"],
            posted_at=0 if ig else row["create_time"],
            duration_sec=0 if ig else row["duration"],
            appearances=max(1, n - i) if ig else 1,
            category=(
                _category(CLASSIFY_BY_ACCOUNT.get(row["account_id"], ""), followers)  # type: ignore[arg-type]
                if classify
                else "unknown"
            ),
            is_client=client_name is not None and row["account_id"] == "kurashiru.com",
        )
        out.append(
            post.model_copy(
                update={"is_pr": is_pr_post(post), "mentions_client": mentions(post, client_name)}
            )
        )
    return out


def surface(
    n: int = 15,
    *,
    platform: str = "tiktok",
    keyword: str = KEYWORD,
    classify: bool = True,
    conclusion: SurfaceConclusion | None = None,
    client_name: str | None = CLIENT,
) -> KwSurface:
    items = posts(n, platform=platform, keyword=keyword, classify=classify, client_name=client_name)
    facts = compute_facts(items, keyword=keyword, client_name=client_name, now_epoch=NOW)
    return KwSurface(
        keyword=keyword,
        platform=platform,
        posts=items,
        client_ranks=facts.client_ranks,
        facts=facts,
        conclusion=conclusion,
    )


def ai_conclusion(
    *,
    headline: str = "料理系クリエイターが上位を持つ面",
    winning: str = "クリエイター7本で再生の68%。保存率の上位は2位と9位のスパイス配合の解説",
    angles: Sequence[tuple[str, list[int]]] = (("スパイスの配合を数字で見せる", [2, 9]),),
) -> SurfaceConclusion:
    return SurfaceConclusion(
        headline=headline,
        winning=ConclusionPoint(text=winning, ranks=[2, 9]),
        gap=ConclusionPoint(text="公式は0本。直近90日以内の投稿は6本", ranks=[]),
        actions=[ConclusionPoint(text="配合を数字で見せる解説を作る", ranks=[2])],
        angles=[ConclusionPoint(text=t, ranks=r) for t, r in angles],
        generated_by="llm",
    )


def covers(items: Sequence[SurfacePost]) -> dict[str, bytes]:
    """奇数位は 9:16、偶数位は 3:4（枠と比率が違う＝切り取らずに収める確認用）。"""
    return {p.url: png(9, 16) if p.rank % 2 else png(3, 4, (120, 140, 160)) for p in items}


__all__ = ["CLIENT", "NOW", "REPORT_ID", "ai_conclusion", "covers", "png", "posts", "surface"]
