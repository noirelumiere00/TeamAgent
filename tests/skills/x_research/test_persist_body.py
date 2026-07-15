"""persist_body（永続化用 要約 markdown ビルダー・Part1）の単体テスト。

v1 は voice（声集め）と comment（コメント分析）のみ記録（needs/buzz は descope）。
商材/主要な声/媒体タグ用語(X（旧Twitter）)/要再確認注記を含むこと、要約であることを検証。
"""

from __future__ import annotations

import re

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
    assert "https://x.com/u/1" in md  # 元投稿URL(provenance)を残す
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


# 未エスケープの Markdown リンク/画像/wikilink/HTML/コードを成立させる素の記号が本文に残らない
# こと（`\[` のように退避されていれば描画は文字＝記法は死ぬ）。ヘッダ/フッタ/テンプレは記号を
# 持たないので、md 全体でこの不変条件が成り立つ。
_UNESCAPED_MD = re.compile(r"(?<!\\)[\[\]<>`]")


def test_voice_summary_neutralizes_stored_markdown_injection() -> None:
    """投稿本文・表示名・商材名の Markdown 記法を無害化する（stored link injection 対策・P1）。"""
    out = XVoiceSearchOutput(
        product_name="[悪意](javascript:alert(1))",
        posts=[
            _post(
                "釣り[ここ](javascript:steal())と![x](https://evil.example/a.png)"
                "と[[clients/機密顧客]]と`code`と<b>tag</b>",
                handle="ev`il]<script>",
                likes=3,
                verified=False,
            ),
        ],
        searched=1,
        selected=1,
        verified_count=0,
        unverified_count=1,
    )
    md = build_voice_summary_md(out)
    assert not _UNESCAPED_MD.search(md), "未エスケープの [ ] < > ` が残っている（injection 面）"
    assert "[[clients/" not in md  # Obsidian wikilink（偽バックリンク/グラフ汚染）を殺す
    assert "<b>" not in md and "<script>" not in md  # 生 HTML を殺す
    assert "javascript:" not in md or "\\[" in md  # リンク開き括弧が退避され記法が成立しない
    assert "釣り" in md  # 可読テキスト自体は保持（黙って全消ししない）


def test_comment_summary_neutralizes_stored_markdown_injection() -> None:
    """コメント分析ノートも語彙/テーマ/client_name の Markdown 記法を無害化する。"""
    from types import SimpleNamespace

    v = SimpleNamespace(
        key_themes=["[t](javascript:x)"],
        pain_points=["<img src=x>"],
        desires=["`rm -rf`"],
        purchase_signals=[],
    )
    out = SimpleNamespace(cross_vocabulary=["[[secret]]"], videos=[v])
    md = build_comment_summary_md(out, client_name="[client](evil)")
    # 未エスケープの [ ] < > ` が残らない＝Markdown 記法として成立しない（描画上は元の文字）。
    assert not _UNESCAPED_MD.search(md)
    assert "\\[\\[secret\\]\\]" in md  # wikilink がバックスラッシュ退避されている
    assert "\\<img" in md  # HTML 開き < が退避されている
