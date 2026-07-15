"""persist_body（永続化用 要約 markdown ビルダー・Part1）の単体テスト。

商材/日付/主要な声/媒体タグ用語(X（旧Twitter）)/要再確認注記を含むこと、要約であること。
"""

from __future__ import annotations

from teamagent.skills.x_research.persist_body import (
    build_comment_summary_md,
    build_needs_summary_md,
    build_voice_summary_md,
)
from teamagent.skills.x_research.schema import (
    NeedCluster,
    XNeedsMiningOutput,
    XPostCard,
    XVoiceSearchOutput,
)


def _post(
    text: str, *, handle: str = "user_a", likes: int = 100, verified: bool = True
) -> XPostCard:
    return XPostCard(
        post_id="1",
        url="https://x.com/u/1",
        author_handle=handle,
        text=text,
        like_count=likes,
        verified=verified,
        verify_note="" if verified else "要再確認: 取得不可",
    )


def test_voice_summary_has_product_media_and_voice() -> None:
    out = XVoiceSearchOutput(
        product_name="辻利 抹茶ミルク",
        posts=[_post("濃厚で最高", likes=8227), _post("甘さ控えめ", verified=False)],
        searched=42,
        selected=2,
        verified_count=1,
        unverified_count=1,
        noise_note="辻利=人名の可能性",
    )
    md = build_voice_summary_md(out)
    assert "辻利 抹茶ミルク" in md
    assert "X（旧Twitter）" in md  # 媒体/X タグの自動付与
    assert "濃厚で最高" in md and "8,227" in md
    assert "⚠️" in md  # 未検証の声は注記付きで残す（黙って捨てない）
    assert "辻利=人名" in md  # 検索メモ


def test_needs_summary_has_clusters_and_hypothesis() -> None:
    out = XNeedsMiningOutput(
        theme="コンビニ 白湯",
        posts=[_post("売ってほしい")],
        clusters=[NeedCluster(label="入手性", insight="コンビニで買えない不満", post_ids=["1"])],
        hypothesis_summary="常温飲料の棚が狭い",
    )
    md = build_needs_summary_md(out)
    assert "コンビニ 白湯" in md
    assert "入手性" in md and "コンビニで買えない不満" in md
    assert "常温飲料の棚が狭い" in md


def test_buzz_summary_has_period_and_total() -> None:
    from teamagent.skills.x_research.persist_body import build_buzz_summary_md

    md = build_buzz_summary_md(
        keyword="セブン 新商品",
        start_date="2026-07-01",
        end_date="2026-07-07",
        daily_counts=[{"date": "2026-07-03", "count": 120}, {"date": "2026-07-04", "count": 80}],
        top_posts=[_post("バズった")],
        spike_analysis="7/3にキャンペーンで急増",
    )
    assert "セブン 新商品" in md and "X（旧Twitter）" in md
    assert "200件" in md  # 総発話 120+80
    assert "7/3にキャンペーンで急増" in md and "バズった" in md


def test_comment_summary_uses_client_name() -> None:
    from types import SimpleNamespace

    v = SimpleNamespace(
        key_themes=["時短", "コスパ"],
        pain_points=["高い"],
        desires=["もっと欲しい"],
        purchase_signals=[],
    )
    out = SimpleNamespace(cross_vocabulary=["神コスパ", "リピ確定"], videos=[v])
    md = build_comment_summary_md(out, client_name="サンマルクカフェ")
    assert "サンマルクカフェ" in md
    assert "時短" in md and "神コスパ" in md and "高い" in md


def test_skills_store_injected_persister() -> None:
    """factory 注入の配線: skill が persister を受け取り保持する（run 側 hook の前提）。"""
    from teamagent.skills.tiktok_comment_mining.skill import TikTokCommentMiningSkill
    from teamagent.skills.x_research.skill import (
        XBuzzMeasureStatusSkill,
        XNeedsMiningSkill,
        XVoiceSearchSkill,
    )

    sentinel = object()
    assert XVoiceSearchSkill(persister=sentinel)._persister is sentinel
    assert XNeedsMiningSkill(persister=sentinel)._persister is sentinel
    assert XBuzzMeasureStatusSkill(persister=sentinel)._persister is sentinel
    assert TikTokCommentMiningSkill(persister=sentinel)._persister is sentinel
