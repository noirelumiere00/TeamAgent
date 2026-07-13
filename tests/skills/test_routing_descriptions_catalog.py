"""カタログ第一弾ツールのルーティング硬化リグレッション（CLAUDE.md §10 E2）。

OC の外側ルーター(Haiku)は name+description だけでツールを選ぶ。ルーティング・シミュ
（tests/routing/README.md・n=51 で 2 シミュ全一致）で潰した混同を、description の
トリガー語と相互排他注記の存在として固定する。description を将来いじって棲み分けが
壊れたら、ここが赤くなって気づける。

corpus 自体の突き合わせは LLM 依存で非決定的なため pytest ゲートにはしない（README 手順で
手動実行）。ここは decision-substring の固定のみ。
"""

from __future__ import annotations

import json
from pathlib import Path

from teamagent.skills.base import SkillRegistry
from teamagent.skills.search_surface_check.skill import SearchSurfaceCheckSkill
from teamagent.skills.tiktok_comment_mining.skill import TikTokCommentMiningSkill
from teamagent.skills.tiktok_search.skill import TikTokSearchSkill
from teamagent.skills.x_research.skill import (
    XBuzzMeasureSkill,
    XNeedsMiningSkill,
    XVoiceSearchSkill,
)


def test_new_tools_registered() -> None:
    for n in (
        "x_voice_search",
        "x_needs_mining",
        "x_buzz_measure",
        "x_buzz_measure_status",
        "search_surface_check",
        "tiktok_comment_mining",
    ):
        assert n in SkillRegistry.list_all()


def test_voice_vs_needs_boundary_hardened() -> None:
    # 混同の核: 商材名＋『不満/欲求』が x_needs_mining に流出した問題の硬化を固定。
    v = XVoiceSearchSkill.description
    assert "商材名が主語" in v  # 商材名が主語なら（不満収集でも）voice
    assert "x_needs_mining" in v  # テーマ全体は needs へ、の相互排他
    n = XNeedsMiningSkill.description
    assert "業界/テーマ全体" in n  # 商材非特定
    assert "x_voice_search" in n  # 商材名主語は voice へ、の相互排他


def test_buzz_points_to_sync_x_tools() -> None:
    d = XBuzzMeasureSkill.description
    assert "発話量" in d
    assert "x_voice_search" in d and "x_needs_mining" in d  # 今すぐ見る単発は同期系へ


def test_surface_check_excludes_algo_and_voice() -> None:
    d = SearchSurfaceCheckSkill.description
    assert "勢力図" in d and "媒体比較" in d
    assert "video_algorithm" in d  # 中身分析は algorithm
    assert "x_voice_search" in d  # 声集めは voice


def test_comment_mining_excludes_algo_and_surface() -> None:
    d = TikTokCommentMiningSkill.description
    assert "コメント" in d
    assert "video_algorithm" in d  # 映像分析は algorithm
    assert "search_surface_check" in d  # 面の勢力図は surface


def test_tiktok_search_points_to_new_tools() -> None:
    # §10: 被るツール側（既存 tiktok_search）にも相互排他注記を入れる。
    d = TikTokSearchSkill.description
    assert "search_surface_check" in d  # 面の勢力図・媒体比較は surface
    assert "video_algorithm" in d  # 勝ち筋タイムラインは algorithm
    assert "tiktok_acquire" in d  # 本体DL/大量取得の非同期ジョブは acquire（R3敵対で解消）
    assert "今すぐ" in d or "即時" in d  # 同期/即時の性格を明示


def _all_registered_skills() -> set[str]:
    """全 skill パッケージを import してレジストリを満たす（corpus の expect 実在確認用）。"""
    import importlib
    import pkgutil

    import teamagent.skills as sk

    for _, name, ispkg in pkgutil.iter_modules(sk.__path__):
        if ispkg:
            try:
                importlib.import_module(f"teamagent.skills.{name}.skill")
            except Exception:
                pass
    return set(SkillRegistry.list_all())


def test_corpus_is_wellformed() -> None:
    # コーパスが壊れていないこと（id 重複なし・expect が実在ツール・alt_ok も実在）。
    path = Path(__file__).parent.parent / "routing" / "catalog_routing_corpus.jsonl"
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(rows) >= 45
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), "corpus に id 重複がある"
    known = _all_registered_skills()
    for r in rows:
        assert r["expect"] in known, f"{r['id']}: expect={r['expect']} が未登録"
        for alt in r.get("alt_ok", []):
            assert alt in known, f"{r['id']}: alt_ok={alt} が未登録"
