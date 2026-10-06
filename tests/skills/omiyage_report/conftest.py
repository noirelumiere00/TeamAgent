"""従来の Skill 単体契約は通知 OFF。署名付き受け入れテストは明示 ON。"""

import pytest


@pytest.fixture(autouse=True)
def legacy_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_LONG_JOB_NOTIFY", "0")
