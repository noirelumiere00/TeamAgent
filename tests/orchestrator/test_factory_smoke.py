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
