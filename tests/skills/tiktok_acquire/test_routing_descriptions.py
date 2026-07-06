"""ルーティング硬化のリグレッション（tiktok系ツールの棲み分けを固定）。

tiktok_acquire（非同期の素材一括取得）↔ tiktok_search（同期のその場検索）の取り違えは
「提案素材が貯まらない/その場の質問に数分待たせる」の実害になるため、採用済み description の
トリガー語と相互参照（search→acquire / acquire→video_algorithm・tiktok_search /
status→単独入口にしない）を固定する（video_approval の前例に倣う）。
"""

from __future__ import annotations

from teamagent.skills.base import SkillRegistry
from teamagent.skills.tiktok_acquire.skill import TikTokAcquireSkill, TikTokAcquireStatusSkill
from teamagent.skills.tiktok_search.skill import TikTokSearchSkill


def test_tiktok_acquire_registered() -> None:
    assert "tiktok_acquire" in SkillRegistry.list_all()
    assert "tiktok_acquire_status" in SkillRegistry.list_all()


def test_tiktok_acquire_description_has_trigger_words() -> None:
    d = TikTokAcquireSkill.description
    # 発火トリガー（素材収集の意図）
    for w in ("取得して", "保存率上位の動画も", "素材"):
        assert w in d, f"tiktok_acquire description に『{w}』が無い"
    # 分析はしない→構造分析は video_algorithm / その場検索は tiktok_search、の相互排他注記
    assert "video_algorithm" in d
    assert "tiktok_search" in d


def test_tiktok_search_description_has_trigger_words() -> None:
    d = TikTokSearchSkill.description
    # 発火トリガー（その場で見たい・調べたい意図）
    for w in ("検索", "調べて", "今すぐ"):
        assert w in d, f"tiktok_search description に『{w}』が無い"
    # 素材の一括収集は acquire へ、の相互排他注記
    assert "tiktok_acquire" in d


def test_tiktok_acquire_status_is_not_an_entrypoint() -> None:
    # status は acquire 専用の後工程。単独発火して「job_id が無い」で詰まらせない
    d = TikTokAcquireStatusSkill.description
    assert "tiktok_acquire" in d
    assert "単独の入口にはしない" in d
