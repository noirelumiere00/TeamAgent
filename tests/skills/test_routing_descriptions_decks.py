"""資料系（デッキ生成）ツールのルーティング硬化リグレッション（お土産資料 D1）。

OC の外側ルーター(Haiku)は name+description だけでツールを選ぶ。営業が「資料にして」
「パワポにして」と依頼したとき、目的と別の生成系（動画分析・86枚提案書・骨子ドラフト）
が返る事故（2026-10-30 DM判定: お土産・資料作成の良い率 8〜21%）を、description の
トリガー語と相互排他注記で防ぐ。

tests/routing/decks_routing_corpus.jsonl と対になる決定論ゲート:
1. 資料系4ツール（omiyage_report_submit / proposal_builder_submit /
   video_algorithm / proposal_draft）の description にトリガー語と
   相互排他ポインタが在ること
2. corpus の形式と凍結ID（事故再発検知の行が消えていないこと）
3. corpus の expect / alt_ok / forbid が実在するツール（または ask_back
   センチネル）を指すこと

corpus 自体の LLM 突き合わせ（どのツールが選ばれるか）は非決定的なので pytest ゲートに
しない（tests/routing/README.md の手順で手動実行）。
"""

from __future__ import annotations

import importlib
import json
import pkgutil
from pathlib import Path
from typing import Any

from teamagent.skills.base import SkillRegistry
from teamagent.skills.omiyage_report.skill import (
    OmiyageReportStatusSkill,
    OmiyageReportSubmitSkill,
)
from teamagent.skills.proposal.skill import ProposalDraftSkill
from teamagent.skills.proposal_builder.skill import (
    ProposalBuilderStatusSkill,
    ProposalBuilderSubmitSkill,
)
from teamagent.skills.video_algorithm.skill import VideoAlgorithmSkill

DECKS_CORPUS = Path(__file__).resolve().parents[1] / "routing" / "decks_routing_corpus.jsonl"

# 消したら赤: 資料系の混同事故（10/30 DM判定）の再発検知に効く行。
REQUIRED_DECKS_IDS = frozenset(
    {
        "om-05",  # 「パワポにして」で動画分析が始まった実害の再現行
        "bd-01",  # 分析語つきだが成果物は資料
        "bd-02",  # 「この分析、パワポにして」
        "bd-04",  # 素材収集（x_voice_search）を資料作成と取り違えない
        "bd-07",  # 「パワポ」語つき86枚提案書
        "bd-08",  # 複数成果物は聞き返す
        "ow-01",  # 一語入力「パワポ」
    }
)

# expect/alt_ok に書ける疑似ラベル（SkillRegistry には実在しない）。
ASK_BACK = "__ask_back__"
SENTINEL_EXPECTS = frozenset({ASK_BACK})


def test_omiyage_submit_leads_deck_requests_and_excludes_other_decks() -> None:
    d = OmiyageReportSubmitSkill.description
    assert d.startswith("初対面の営業先へ持っていくTikTok調査のお土産資料")
    assert "お土産" in d
    assert "競合比較" in d
    assert "パワポ" in d or "スライド" in d
    assert "proposal_builder_submit" in d  # 86枚提案書は提案書側
    assert "video_algorithm" in d  # 分析は algorithm


def test_proposal_builder_submit_marks_omiyage_out_of_scope() -> None:
    d = ProposalBuilderSubmitSkill.description
    assert "商材の企画提案書" in d
    assert "お土産資料は対象外" in d
    assert "omiyage_report_submit" in d


def test_video_algorithm_refuses_deck_requests() -> None:
    d = VideoAlgorithmSkill.description
    assert "資料・パワポ・スライドの作成は対象外" in d
    assert "omiyage_report_submit" in d
    assert "proposal_builder_submit" in d
    # 既存の固定（test_generated_at_and_slack.py と同じ禁止語）を維持する。
    assert "勝ち筋" not in d and "勝ちパターン" not in d


def test_proposal_draft_declines_pptx() -> None:
    d = ProposalDraftSkill.description
    assert "完成品のPPTX資料は作らない" in d
    assert "omiyage_report_submit" in d
    assert "proposal_builder_submit" in d


def test_status_tools_point_back_to_their_submits() -> None:
    assert "omiyage_report_submit" in OmiyageReportStatusSkill.description
    assert "proposal_builder_submit" in ProposalBuilderStatusSkill.description


def _all_registered_skills() -> set[str]:
    """全 skill パッケージを import してレジストリを満たす（corpus の expect 実在確認用）。"""
    import teamagent.skills as sk

    for _, name, ispkg in pkgutil.iter_modules(sk.__path__):
        if ispkg:
            try:
                importlib.import_module(f"teamagent.skills.{name}.skill")
            except Exception:
                pass
    return set(SkillRegistry.list_all())


def _load_corpus() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in DECKS_CORPUS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def test_decks_corpus_frozen_ids_exist() -> None:
    rows = _load_corpus()
    ids = {row["id"] for row in rows}
    assert len(ids) == len(rows), "corpus に重複 id がある"
    missing = REQUIRED_DECKS_IDS - ids
    assert not missing, f"凍結 id が消えた: {sorted(missing)}"


def test_decks_corpus_targets_exist() -> None:
    registered = _all_registered_skills()
    rows = _load_corpus()
    for row in rows:
        for field in ("expect", "alt_ok", "forbid"):
            labels = row.get(field) or []
            if isinstance(labels, str):
                labels = [labels]
            for label in labels:
                if label.startswith("__"):
                    assert label in SENTINEL_EXPECTS, f"{row['id']}: 未知のセンチネル {label}"
                else:
                    assert label in registered, f"{row['id']}: 実在しないツール {label}"
        assert row.get("utterance"), f"{row['id']}: utterance が空"
