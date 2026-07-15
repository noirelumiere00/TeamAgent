"""persist_body（永続化用 要約 markdown ビルダー・Part1）の単体テスト。

v1 は voice（声集め）と comment（コメント分析）のみ記録（needs/buzz は descope）。
商材/主要な声/媒体タグ用語(X（旧Twitter）)/要再確認注記を含むこと、要約であることを検証。
"""

from __future__ import annotations

from teamagent.skills.x_research.persist_body import (
    build_comment_summary_md,
    build_voice_summary_md,
)
from teamagent.skills.x_research.schema import XPostCard, XVoiceSearchOutput


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


def test_persist_injected_only_to_voice_and_comment() -> None:
    """factory 注入の配線: 記録対象(voice/comment)が persister を受け取り保持する。"""
    from teamagent.skills.tiktok_comment_mining.skill import TikTokCommentMiningSkill
    from teamagent.skills.x_research.skill import XVoiceSearchSkill

    sentinel = object()
    assert XVoiceSearchSkill(persister=sentinel)._persister is sentinel
    assert TikTokCommentMiningSkill(persister=sentinel)._persister is sentinel
