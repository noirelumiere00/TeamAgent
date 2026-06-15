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
