from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PRUNER = ROOT / "infra" / "openclaw" / "prune-runtime.mjs"
BLOCK_START = "// BEGIN bundled-skills-pruning"
BLOCK_END = "// END bundled-skills-pruning"


def _run_skills_pruner(
    tmp_path: Path,
    skills_root: Path,
    *,
    disable_deletion: bool = False,
) -> subprocess.CompletedProcess[str]:
    source = PRUNER.read_text(encoding="utf-8")
    assert source.count(BLOCK_START) == 1
    assert source.count(BLOCK_END) == 1
    pruning_block = source.split(BLOCK_START, maxsplit=1)[1].split(
        BLOCK_END, maxsplit=1
    )[0]
    remove_override = ", () => {}" if disable_deletion else ""
    runner = tmp_path / "prune-bundled-skills.mjs"
    runner.write_text(
        f'''import fs from "node:fs";
import path from "node:path";

{pruning_block}

const skillsRoot = process.argv[2];
const removedNames = pruneBundledSkills(skillsRoot{remove_override});
assertBundledSkillsPruned(skillsRoot);
process.stdout.write(JSON.stringify({{
  removedNames,
  remainingNames: fs.readdirSync(skillsRoot),
}}));
''',
        encoding="utf-8",
    )
    return subprocess.run(
        ["node", str(runner), str(skills_root)],
        check=False,
        text=True,
        capture_output=True,
    )


def test_pruner_removes_every_bundled_skill_and_records_names(tmp_path: Path) -> None:
    skills_root = tmp_path / "app" / "skills"
    (skills_root / "clawhub").mkdir(parents=True)
    (skills_root / "clawhub" / "SKILL.md").write_text("external install path\n")
    (skills_root / "healthcheck" / "nested").mkdir(parents=True)
    (skills_root / "healthcheck" / "nested" / "audit.js").write_text("ssh audit\n")

    completed = _run_skills_pruner(tmp_path, skills_root)

    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(completed.stdout)
    assert receipt["removedNames"] == ["clawhub", "healthcheck"]
    assert receipt["remainingNames"] == []
    assert skills_root.is_dir()
    assert list(skills_root.iterdir()) == []
    assert "removedNames: bundledSkillsRemoved" in PRUNER.read_text(encoding="utf-8")


def test_pruner_fails_closed_when_skill_deletion_is_disabled(tmp_path: Path) -> None:
    skills_root = tmp_path / "app" / "skills"
    (skills_root / "clawhub").mkdir(parents=True)

    completed = _run_skills_pruner(
        tmp_path,
        skills_root,
        disable_deletion=True,
    )

    assert completed.returncode != 0
    assert "bundled skills pruning failed" in completed.stderr
    assert '"residualEntries":["clawhub"]' in completed.stderr
    assert (skills_root / "clawhub").is_dir()
