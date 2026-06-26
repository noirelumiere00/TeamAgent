"""ルーティング硬化のリグレッション（説明文の棲み分けを固定）。

ルーティング・シミュレーション(n=32, 97%)で最大の潜在実害だった
video_approval ↔ video_analysis の取り違え（納品物の合否が競合分析に化ける＝納品事故）を、
description のトリガー語と相互排他注記で硬化したことを固定する。
"""

from __future__ import annotations

from teamagent.skills.base import SkillRegistry
from teamagent.skills.search.skill import SearchSkill
from teamagent.skills.video.skill import VideoAnalysisSkill
from teamagent.skills.video_approval.skill import VideoApprovalSkill


def test_video_approval_registered() -> None:
    assert "video_approval" in SkillRegistry.list_all()


def test_video_approval_description_has_trigger_words() -> None:
    d = VideoApprovalSkill.description
    # 発火トリガー（③の核）
    for w in ("納品", "編集者", "オリエン", "誤植", "尺"):
        assert w in d, f"video_approval description に『{w}』が無い"
    # 曖昧『この動画チェックして』をデフォルトで拾う宣言
    assert "チェック" in d
    # 外部競合は video_analysis へ、の相互排他注記
    assert "video_analysis" in d


def test_video_analysis_excludes_delivery() -> None:
    d = VideoAnalysisSkill.description
    assert "競合" in d or "他社" in d
    # 自社納品物は対象外→video_approval、の相互排他注記
    assert "video_approval" in d


def test_search_description_has_exploration_words() -> None:
    d = SearchSkill.description
    # 『探して/あったっけ』等の探索意図を search に明示（knowledge_deliver 等への流出防止）
    assert "探して" in d or "あったっけ" in d
