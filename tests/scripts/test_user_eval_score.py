"""外部送受信なしで記録・採点・改善レポートの契約を検証する。"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.user_eval.fakes import FakeJudge
from scripts.user_eval.recorded import CRITICAL_VIOLATIONS, load_recorded
from scripts.user_eval.score import (
    infer_files,
    load_previous,
    load_score_scenarios,
    overall,
    report,
    score_recorded,
    score_scenario,
)

ROOT = Path(__file__).resolve().parents[2]


def scenario() -> dict[str, Any]:
    return {
        "id": "example",
        "utterances": ["資料を作って"],
        "checks": [
            {"type": "must_attach_file", "weight": 3},
            {"type": "max_latency_s", "seconds": 120, "weight": 1},
            {"type": "llm_judge"},
        ],
    }


def record() -> dict[str, Any]:
    return {
        "scenario_id": "example",
        "sent": [{"ts": "100.00", "text": "資料を作って"}],
        "received": [
            {
                "ts": "110.50",
                "author": "aico",
                "text": "資料です",
                "files": [{"name": "資料.pdf", "mimetype": "application/pdf"}],
                "is_feedback_buttons": False,
            }
        ],
        "request_ids": ["r1"],
        "logs": [{"event": "search_fanout_lanes"}],
        "judge": {"verdict": "good", "reason": "依頼どおり"},
    }


def write_json(path: Path, data: Any) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False))
    return path


def test_load_recorded_preserves_data(tmp_path: Path) -> None:
    row = record()
    assert load_recorded(write_json(tmp_path / "r.json", [row])) == [row]
    second = {**row, "scenario_id": "second"}
    assert len(load_recorded(write_json(tmp_path / "r.json", [row, second]))) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"received": [{"author": "bot"}]},
        {"sent": [{"ts": "nan", "text": "a"}]},
        {"request_ids": "r1"},
        {"logs": {}},
        {"judge": {"verdict": "unknown", "reason": "x"}},
        {"critical_violations": ["unknown"]},
        {"judge": {"verdict": "good", "reason": ""}},
        {"sent": [{"ts": True, "text": "a"}]},
        {"sent": [{"ts": -1, "text": "a"}]},
        {"received": [{**record()["received"][0], "is_feedback_buttons": "false"}]},
        {"received": [{**record()["received"][0], "files": [{"name": "a.pdf"}]}]},
    ],
)
def test_invalid_record_rejected(tmp_path: Path, change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        load_recorded(write_json(tmp_path / "r.json", [{**record(), **change}]))


@pytest.mark.parametrize("data", [{}, [], [record(), record()], [None]])
def test_invalid_top_level(tmp_path: Path, data: Any) -> None:
    with pytest.raises(ValueError):
        load_recorded(write_json(tmp_path / "r.json", data))


@pytest.mark.parametrize("verdict,expected", [("good", 100), ("partial", 85), ("bad", 70)])
def test_verdict_points(verdict: str, expected: float) -> None:
    row = record()
    row["judge"]["verdict"] = verdict
    assert score_scenario(scenario(), row)["score"] == expected


def test_weighted_and_equal_checks() -> None:
    row = record()
    row["received"][0]["files"] = []
    result = score_scenario(scenario(), row)
    assert result["score"] == 47.5
    assert result["checks"][0]["max_points"] == 52.5
    assert "-52.5点" in result["deductions"][0]
    definition = scenario()
    for spec in definition["checks"]:
        spec.pop("weight", None)
    assert score_scenario(definition, row)["score"] == 65


@pytest.mark.parametrize("weight", [0, -1, True, "1", float("inf"), float("nan")])
def test_invalid_weights(tmp_path: Path, weight: Any) -> None:
    definition = scenario()
    definition["checks"][0]["weight"] = weight
    with pytest.raises(ValueError, match="weight"):
        load_score_scenarios(write_json(tmp_path / "s.json", [definition]))


@pytest.mark.parametrize("violation", CRITICAL_VIOLATIONS)
@pytest.mark.parametrize("location", ["record", "judge"])
def test_critical_violation_forces_zero(violation: str, location: str) -> None:
    row = record()
    source = row if location == "record" else row["judge"]
    source["critical_violations"] = [violation]
    result = score_scenario(scenario(), row)
    assert result["score"] == 0
    assert not result["passed"]
    assert any("重大違反" in reason for reason in result["deductions"])


def test_critical_patterns_and_own_citation() -> None:
    definition, row = scenario(), record()
    definition["critical_patterns"] = ["進行中"]
    row["received"][0]["text"] = "2026-10-15 時点では進行中"
    assert score_scenario(definition, row)["score"] == 0
    row = record()
    row["received"][0]["citations"] = [
        {"url": "https://demo.slack.com/archives/D1/p1", "author_id": "aico", "is_aico_dm": True}
    ]
    assert score_scenario(scenario(), row)["score"] == 0


def test_judge_unavailable_recorded_preferred_and_filtered() -> None:
    row, judge = record(), FakeJudge("partial")
    assert score_scenario(scenario(), row, judge)["score"] == 100
    assert not judge.calls
    del row["judge"]
    assert score_scenario(scenario(), row)["score"] == 70
    row["received"].append({**row["received"][0], "text": "評価専用", "is_feedback_buttons": True})
    assert score_scenario(scenario(), row, judge)["score"] == 85
    assert len(judge.calls) == 1
    assert len(judge.calls[0]["conversation"]["recorded"]["received"]) == 1


def test_judge_exception_visible() -> None:
    class BrokenJudge(FakeJudge):
        def judge(
            self, prompt: str, conversation: dict[str, Any], *, timeout_s: float
        ) -> dict[str, str]:
            raise TimeoutError("offline")

    row = record()
    del row["judge"]
    result = score_scenario(scenario(), row, BrokenJudge())
    assert result["score"] == 70
    assert "offline" in result["deductions"][0]


@pytest.mark.parametrize("excluded", ["buttons", "user", "history"])
def test_excluded_messages_cannot_satisfy_check_or_latency(excluded: str) -> None:
    row = record()
    extra = copy.deepcopy(row["received"][0])
    extra.update(ts=101, text="2026-10-15 時点では進行中")
    if excluded == "buttons":
        extra["is_feedback_buttons"] = True
    elif excluded == "user":
        extra["author"] = "user"
    else:
        extra["ts"] = 99
    row["received"][0].update(ts=250, files=[])
    row["received"].append(extra)
    definition = scenario()
    definition["critical_patterns"] = ["進行中"]
    result = score_scenario(definition, row)
    assert result["score"] == 30
    assert result["latencies_s"] == [150]
    assert not result["critical_violations"]


def test_all_turns_required_and_ask_back_not_hidden() -> None:
    row, definition = record(), scenario()
    definition["utterances"].append("続けて")
    assert score_scenario(definition, row)["score"] == 0
    row["sent"].append({"ts": 120, "text": "続けて"})
    assert score_scenario(definition, row)["score"] == 0
    row["received"].append({**row["received"][0], "ts": 130})
    assert score_scenario(definition, row)["latencies_s"] == [10.5, 10]
    definition["checks"].insert(0, {"type": "must_not_ask_back"})
    row["received"][0]["text"] = "何が必要ですか？"
    assert not score_scenario(definition, row)["checks"][0]["passed"]


def test_cli_previous_delta_and_report(tmp_path: Path) -> None:
    recorded = write_json(tmp_path / "r.json", [record()])
    scenarios = write_json(tmp_path / "s.json", [scenario()])
    previous = tmp_path / "previous.jsonl"
    previous.write_text(json.dumps({"id": "example", "score": 80}) + "\n")
    output = tmp_path / "out"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.user_eval",
            "score",
            "--recorded",
            str(recorded),
            "--scenarios",
            str(scenarios),
            "--out",
            str(output),
            "--previous",
            str(previous),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    row = json.loads((output / "results.jsonl").read_text())
    assert row["delta"] == 20
    assert row["previous_score"] == 80
    text = (output / "report.md").read_text()
    assert "100.0 / 100" in text and "+20.0点" in text
    assert "目標95点: 到達" in text and "## 直すべきこと" in text
    assert row["request_ids"] == ["r1"]


def test_missing_scenario_zero_and_unknown_rejected(tmp_path: Path) -> None:
    definitions = [scenario(), {**scenario(), "id": "missing"}]
    path = write_json(tmp_path / "s.json", definitions)
    recorded = write_json(tmp_path / "r.json", [record()])
    rows = score_recorded(recorded, path, tmp_path / "out")
    assert overall(rows) == 50
    assert rows[1]["score"] == 0
    write_json(recorded, [{**record(), "scenario_id": "unknown"}])
    with pytest.raises(ValueError, match="定義にない"):
        score_recorded(recorded, path, tmp_path / "out")


def test_report_deductions_hints_and_changed_cohort(tmp_path: Path) -> None:
    row = record()
    row["received"][0]["files"] = []
    result = score_scenario(scenario(), row)
    text = report([result], [{"id": "old", "score": 30}], ROOT)
    assert "全体差は比較不可" in text
    assert "前回のみのシナリオ: old" in text
    assert "推測" in text and "must_attach_file: -52.5点" in text
    assert "src/teamagent/" in text
    assert infer_files(result, tmp_path) == []
    assert overall([{"score": 90}, {"score": 99.9}, {"score": 97}]) == 95.6


@pytest.mark.parametrize(
    "data",
    [
        [{"id": "x"}],
        [{"id": "x", "score": 101}],
        [{"id": "x", "score": True}],
        [{"id": "x", "score": 1}] * 2,
        [],
    ],
)
def test_previous_must_have_scores(tmp_path: Path, data: list[dict[str, Any]]) -> None:
    path = tmp_path / "old.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in data))
    with pytest.raises(ValueError):
        load_previous(path)


def test_repository_scenarios_need_no_local_names() -> None:
    definitions = load_score_scenarios(ROOT / "scripts/user_eval/scenarios.json")
    assert len(definitions) == 13
    assert definitions[0]["critical_patterns"]
