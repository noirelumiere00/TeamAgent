"""factory.py の軽量スモーク（heavy deps 無しの env でも通る範囲）.

`build_production_tools()` の実行は実 Skill 依存（boto3/psycopg/sentence-transformers）を要するため
ここでは **import と呼び出し可能性のみ** 検証する（重い import は関数内 遅延 import なのでモジュール
import は軽量）。実 search を繋いだ E2E は full env + SSMトンネルで run_orchestrator_prod.py を使う。
"""

from __future__ import annotations

import teamagent.orchestrator.factory as factory


def test_factory_module_imports_light() -> None:
    # 重い依存が無い環境でも factory モジュールは import できる（遅延 import 設計）.
    assert callable(factory.build_production_tools)


def test_envflag_helper() -> None:
    import os

    os.environ["__TA_TEST_FLAG__"] = "true"
    assert factory._envflag("__TA_TEST_FLAG__") is True
    os.environ["__TA_TEST_FLAG__"] = "no"
    assert factory._envflag("__TA_TEST_FLAG__") is False
    del os.environ["__TA_TEST_FLAG__"]
    assert factory._envflag("__TA_TEST_FLAG__", "false") is False


def test_scrape_video_skills_importable() -> None:
    # §L Phase1: 露出候補スキルが軽量 import できる（重依存は lazy ＝ google-genai/yt-dlp 無しでもOK）.
    from teamagent.skills.tiktok_search.skill import TikTokSearchSkill
    from teamagent.skills.video.skill import VideoAnalysisSkill
    from teamagent.skills.video_algorithm.skill import VideoAlgorithmSkill

    assert VideoAnalysisSkill.name == "video_analysis"
    assert VideoAlgorithmSkill.name == "video_algorithm"
    assert TikTokSearchSkill.name == "tiktok_search"


def test_operation_log_skill_importable() -> None:
    # Wave1-②: operation_log Skill が軽量 import できる（bedrock/slack 依存は run() で遅延生成）.
    from teamagent.skills.operation_log.skill import OperationLogSkill

    assert OperationLogSkill.name == "operation_log"
    # description に CRM 関連語があるか（factory 登録時の説明文として配線確認に使う）
    assert "CRM" in OperationLogSkill.description or "活動ログ" in OperationLogSkill.description


def test_operation_log_envflag_wired() -> None:
    # factory.py が USE_OPERATION_LOG_TOOLS gated で operation_log を append する分岐を持つ.
    # build_production_tools() の重 deps を呼ばずに、ソース文字列で配線の存在を確認するスモーク。
    import inspect

    src = inspect.getsource(factory.build_production_tools)
    assert "USE_OPERATION_LOG_TOOLS" in src
    assert "OperationLogSkill" in src


def test_recommend_skill_importable() -> None:
    # recommend Skill が軽量 import できる（SearchSkill 型参照のみ・heavy deps は search が遅延生成）.
    from teamagent.skills.recommend.skill import RecommendSkill

    assert RecommendSkill.name == "recommend"


def test_recommend_envflag_gated() -> None:
    # recommend は USE_RECOMMEND_SKILL gated（既定 OFF）で append される分岐を持つ.
    import inspect

    src = inspect.getsource(factory.build_production_tools)
    assert "USE_RECOMMEND_SKILL" in src
    assert "RecommendSkill" in src
    assert src.index("USE_RECOMMEND_SKILL") < src.index("RecommendSkill")
    # 常時 ON 群（最初の env-gate より前の head スライス）に RecommendSkill が混ざっていない
    # ＝必ず gate ブロック側にあることを担保（後方互換・既定 OFF の回帰検知）。
    head = src[: src.index("USE_KNOWLEDGE_DELIVER")]
    assert "RecommendSkill" not in head


def test_proposal_deck_skill_importable() -> None:
    # Wave3-⑧: proposal_deck Skill が軽量 import できる（bedrock 依存は __init__/run で遅延生成）.
    from teamagent.skills.proposal_deck.skill import ProposalDeckSkill

    assert ProposalDeckSkill.name == "proposal_deck"


def test_proposal_deck_envflag_gated() -> None:
    # proposal_deck は USE_PROPOSAL_DECK_TOOLS gated（既定 OFF）で append される分岐を持つ.
    # FMT テンプレ未提供で呼ぶと必ずエラーになるため、他の任意スキルと同じ opt-in に統一済。
    import inspect

    src = inspect.getsource(factory.build_production_tools)
    assert "USE_PROPOSAL_DECK_TOOLS" in src
    assert "ProposalDeckSkill" in src
    # 初期 specs（常時 ON 群）に proposal_deck が混ざっていない＝gate ブロック側にあること。
    # 「ProposalDeckSkill」の初出が USE_PROPOSAL_DECK_TOOLS 分岐より後にあることで担保する。
    assert src.index("USE_PROPOSAL_DECK_TOOLS") < src.index("ProposalDeckSkill")
