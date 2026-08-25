from __future__ import annotations

import ast
import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "infra/codebuild/image_deployment_consumers.json"
GENERATION_INPUTS = ROOT / "infra/deploy/buildspec_generation_inputs.json"

_SHIM_KEY = "ACTIVATION-SHIM" + "(ingest)"
_SHIM_COMMENT = (
    f"# {_SHIM_KEY}: 一時対応。Activation 完了後に canonical registry と",
    "# release_evidence を原子的に正名化して撤去する。docs/activation/ACTIVATION_STATE.md 参照。",
)
_FORBIDDEN_ACTIVATOR_TYPE = "eventbridge_rule_" + "lambda_taskdef_arn_environment"
_EXACT_INGEST_IDENTITIES = {"ingest", "teamagent-dev-ingest-weekly"}
_EXPECTED_SHIM_TAG_COUNTS = {
    "infra/deploy/terraform_runtime_guard.sh": 10,
    "infra/terraform/ecs_service_apply_saga.py": 9,
    "infra/terraform/image_release_context.py": 6,
}
_EXPECTED_PYTHON_EXACT_EQUALITY_COUNTS = {
    "infra/terraform/ecs_service_apply_saga.py": 8,
    "infra/terraform/image_release_context.py": 5,
}


def _repository_files() -> list[Path]:
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / item for item in result.stdout.decode("utf-8").split("\0") if item]


def _repository_file_bytes(path: Path) -> bytes:
    if path.is_symlink():
        return os.readlink(path).encode("utf-8")
    return path.read_bytes()


def _tag_counts() -> dict[str, int]:
    key = _SHIM_KEY.encode("utf-8")
    counts: dict[str, int] = {}
    for path in _repository_files():
        count = _repository_file_bytes(path).count(key)
        if count:
            counts[path.relative_to(ROOT).as_posix()] = count
    return counts


def _string_literals(node: ast.AST) -> set[str]:
    return {
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }


def test_repository_does_not_define_the_rejected_activator_type() -> None:
    forbidden = _FORBIDDEN_ACTIVATOR_TYPE.encode("utf-8")
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in _repository_files()
        if forbidden in _repository_file_bytes(path)
    ]

    assert offenders == []


def test_ingest_registry_keeps_the_canonical_eventbridge_ecs_activator_type() -> None:
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    ingest = [consumer for consumer in registry["consumers"] if consumer["consumer_id"] == "ingest"]

    assert len(ingest) == 1
    assert ingest[0]["activator"] == {
        "type": "eventbridge_rule_ecs_target",
        "identity": "teamagent-dev-ingest-weekly",
    }


def test_shim_tag_locations_counts_and_comment_text_are_pinned() -> None:
    counts = _tag_counts()

    assert counts == _EXPECTED_SHIM_TAG_COUNTS
    assert sum(counts.values()) == 25
    for relative_path, expected_count in _EXPECTED_SHIM_TAG_COUNTS.items():
        lines = (ROOT / relative_path).read_text(encoding="utf-8").splitlines()
        tagged_lines = [index for index, line in enumerate(lines) if _SHIM_KEY in line]
        assert len(tagged_lines) == expected_count
        for index in tagged_lines:
            assert tuple(line.strip() for line in lines[index : index + 2]) == _SHIM_COMMENT


def test_each_shim_file_uses_exact_ingest_equality_without_set_membership() -> None:
    for relative_path, expected_exact_matches in _EXPECTED_PYTHON_EXACT_EQUALITY_COUNTS.items():
        path = ROOT / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        exact_matches = 0
        forbidden_memberships: list[int] = []
        for comparison in (node for node in ast.walk(tree) if isinstance(node, ast.Compare)):
            left = comparison.left
            for operator, right in zip(comparison.ops, comparison.comparators, strict=True):
                literals = _string_literals(left) | _string_literals(right)
                if isinstance(operator, ast.Eq) and literals & _EXACT_INGEST_IDENTITIES:
                    exact_matches += 1
                if isinstance(operator, (ast.In, ast.NotIn)) and (
                    literals & _EXACT_INGEST_IDENTITIES
                ):
                    forbidden_memberships.append(comparison.lineno)
                left = right
        assert exact_matches == expected_exact_matches, path
        assert forbidden_memberships == [], path

    guard = (ROOT / "infra/deploy/terraform_runtime_guard.sh").read_text(encoding="utf-8")
    assert re.search(r'\$(?:component|consumer_id|id)\s*==\s*"ingest"', guard)


def test_shim_tag_surface_does_not_intersect_the_frozen_generation_inputs() -> None:
    manifest = json.loads(GENERATION_INPUTS.read_text(encoding="utf-8"))
    frozen_inputs = set(manifest["inputs"])
    tagged_files = set(_tag_counts())

    assert len(frozen_inputs) == 18
    assert "infra/codebuild/release_evidence.py" in frozen_inputs
    assert tagged_files.isdisjoint(frozen_inputs)
