"""一時ディレクトリで契約を一つずつ壊し、pytest が赤になることを確認する。"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "scripts/user_eval/core.py").read_text()
    names = ["must_contain_link", "must_attach_file", "must_not_contain",
             "must_not_repeat_numbers_after_metrics_post", "must_not_ask_back",
             "must_not_cite_own_answers", "status_freshness", "max_latency_s"]
    marker = '    return {"type": name, "passed": bool(passed),'
    mutations = [(name, source.replace(marker, f'    if name == "{name}":\n        passed = True\n' + marker)) for name in names]
    mutations += [
        ("dm_guard", source.replace('if not re.fullmatch(r"D[A-Z0-9]+", dm_id):', 'if False:')),
        ("evaluation_buttons", source.replace('if message.kind == "evaluation_buttons":', 'if False:')),
        ("wait_deadline", source.replace('deadline = start + self.timeout_s', 'deadline = start + self.timeout_s + 1')),
        ("judge_verdict", source.replace('verdict["verdict"] == "good"', 'True')),
    ]
    jobs = [(name, "core.py", changed) for name, changed in mutations]
    scoring = (root / "scripts/user_eval/score.py").read_text()
    recording = (root / "scripts/user_eval/recorded.py").read_text()
    extra = [
        ("recorded_buttons", "recorded.py", recording, 'not m["is_feedback_buttons"]', 'True'),
        ("recorded_author", "recorded.py", recording, 'm["author"] == "aico"', 'True'),
        ("recorded_history", "recorded.py", recording, 'timestamp(m["ts"]) >= start', 'True'),
        ("recorded_validation", "recorded.py", recording, 'if not isinstance(message.get("is_feedback_buttons"), bool):', 'if False:'),
        ("score_weights", "score.py", scoring, '70 * spec.get("weight", 1) / total_weight', '70 / len(specs)'),
        ("score_partial", "score.py", scoring, '"partial": 0.5', '"partial": 1.0'),
        ("score_critical", "score.py", scoring, 'if complete and not critical else 0.0', 'if complete else 0.0'),
        ("score_complete", "score.py", scoring, 'if complete and not critical else 0.0', 'if not critical else 0.0'),
        ("score_previous_delta", "score.py", scoring, 'round(row["score"] - old[row["id"]]["score"], 1)', '0.0'),
        ("score_overall", "score.py", scoring, 'sum(r["score"] for r in rows) / len(rows)', 'max(r["score"] for r in rows)'),
        ("score_missing", "score.py", scoring, 'for scenario in definitions:\n', 'for scenario in definitions:\n        if scenario["id"] not in records:\n            continue\n'),
    ]
    for name, filename, original, before, after in extra:
        changed = original.replace(before, after)
        if changed == original:
            raise RuntimeError(f"変異の差し込みに失敗: {name}")
        jobs.append((name, filename, changed))
    output = root / "scripts/user_eval/results/mutations.txt"
    output.parent.mkdir(parents=True, exist_ok=True)
    failed = False
    with output.open("w") as log:
        for name, filename, changed in jobs:
            if filename == "core.py" and source == changed:
                raise RuntimeError(f"変異の差し込みに失敗: {name}")
            with tempfile.TemporaryDirectory(prefix="user-eval-mutation-") as tmp:
                target = Path(tmp)
                shutil.copytree(root / "scripts/user_eval", target / "scripts/user_eval",
                                ignore=shutil.ignore_patterns("results", "__pycache__", "scenarios.local.json"))
                (target / "scripts/user_eval" / filename).write_text(changed)
                (target / "src").mkdir()
                shutil.copytree(root / "src/teamagent", target / "src/teamagent", ignore=shutil.ignore_patterns("__pycache__"))
                (target / "tests/scripts").mkdir(parents=True)
                for test in (root / "tests/scripts").glob("test_user_eval*.py"):
                    shutil.copy(test, target / "tests/scripts" / test.name)
                result = subprocess.run(
                    [sys.executable, "-m", "pytest", "-q", "-p", "no:warnings", "tests/scripts"],
                    cwd=target, capture_output=True, text=True, timeout=60, check=False,
                )
                killed = result.returncode == 1 and " failed" in result.stdout and "ERROR" not in result.stdout
                log.write(f"=== {name}: exit={result.returncode} ===\n{result.stdout}{result.stderr}\n")
                print(f"{name}: {'KILLED' if killed else 'UNEXPECTED'} (pytest exit={result.returncode})")
                failed |= not killed
    print(f"全失敗出力: {output.relative_to(root)}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
