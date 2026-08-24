"""root mutation 監視の契約（Freeze v2 の break-glass 監視）。

CloudTrail API 失敗を「0 件」と読まないこと（fail-closed）が最重要。
2026-08 に ExpiredToken を空結果と誤読して偽 green を 2 回出した実害がある。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infra/deploy"))

from root_mutation_monitor import (  # noqa: E402
    _ROOT_ARN_RE,
    MonitorError,
    monitored_events,
    root_mutations,
)

FREEZE = ROOT / "infra/deploy/activation_freeze.json"


def test_monitored_events_come_from_the_freeze_declaration() -> None:
    """event 一覧は宣言を単一の真実源にする（手書き二重管理の禁止）。"""
    declared = json.loads(FREEZE.read_text(encoding="utf-8"))
    expected = declared["generation_publisher_freeze"]["v2"]["scope_definition"]["monitored_events"]
    assert monitored_events(FREEZE) == expected


def test_missing_scope_definition_is_fatal(tmp_path: Path) -> None:
    doc: dict[str, Any] = json.loads(FREEZE.read_text(encoding="utf-8"))
    del doc["generation_publisher_freeze"]["v2"]["scope_definition"]
    path = tmp_path / "f.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(MonitorError, match="scope_definition"):
        monitored_events(path)


def test_api_failure_is_treated_as_a_violation_not_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """CloudTrail が失敗したら「0 件」ではなく検査不能=違反として例外にする。"""
    import root_mutation_monitor as monitor

    class _Failed:
        returncode = 255
        stdout = ""
        stderr = "ExpiredTokenException: The security token included in the request is expired"

    monkeypatch.setattr(monitor.subprocess, "run", lambda *a, **k: _Failed())
    with pytest.raises(MonitorError, match="検査不能"):
        root_mutations("2026-08-24T00:00:00Z", ["StartBuild"])


def test_malformed_since_is_rejected() -> None:
    for bad in ("2026-08-24", "2026-08-24T00:00:00+09:00", "yesterday"):
        with pytest.raises(MonitorError, match="--since"):
            root_mutations(bad, ["StartBuild"])


def test_only_root_arns_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """root 以外の principal を root として数えない（過検出の防止）。"""
    import root_mutation_monitor as monitor

    events = [
        {
            "EventTime": "t1",
            "CloudTrailEvent": json.dumps(
                {"userIdentity": {"arn": "arn:aws:iam::718959508629:root"}}
            ),
        },
        {
            "EventTime": "t2",
            "CloudTrailEvent": json.dumps(
                {"userIdentity": {"arn": "arn:aws:iam::718959508629:user/AIIAdev"}}
            ),
        },
        {
            "EventTime": "t3",
            "CloudTrailEvent": json.dumps(
                {"userIdentity": {"arn": "arn:aws:sts::718959508629:assumed-role/r/s"}}
            ),
        },
    ]

    class _Ok:
        returncode = 0
        stdout = json.dumps({"Events": events})
        stderr = ""

    monkeypatch.setattr(monitor.subprocess, "run", lambda *a, **k: _Ok())
    found = root_mutations("2026-08-24T00:00:00Z", ["StartBuild"])
    assert len(found) == 1
    assert found[0]["time"] == "t1"


def test_root_arn_pattern_does_not_match_lookalikes() -> None:
    assert _ROOT_ARN_RE.match("arn:aws:iam::718959508629:root")
    for lookalike in (
        "arn:aws:iam::718959508629:user/root",
        "arn:aws:iam::718959508629:role/root",
        "arn:aws:sts::718959508629:assumed-role/root/session",
    ):
        assert not _ROOT_ARN_RE.match(lookalike), lookalike
