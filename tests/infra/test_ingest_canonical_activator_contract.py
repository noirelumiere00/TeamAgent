"""ingest activator type の正名化契約。

Activation 中は ingest を `eventbridge_rule_ecs_target` と宣言したまま
`ACTIVATION-SHIM(ingest)` タグ付きの consumer_id 一致 shim で実態を吸収していた。
正名化後は registry が実態（EventBridge rule → dispatch Lambda の env に taskdef ARN）を
宣言し、分岐は **型** で行う。本テストはその不可逆性を固定する:

1. shim タグが 1 つも残っていないこと（散文を含む repo 全体で 0）
2. registry の ingest が正名化後の activator type であること
3. 実装が consumer_id のリテラル一致で ingest を特別扱いしていないこと
4. 正名化後の type が frozen generation surface（release_evidence.py）に届いていること
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "infra/codebuild/image_deployment_consumers.json"
GENERATION_INPUTS = ROOT / "infra/deploy/buildspec_generation_inputs.json"

_SHIM_KEY = "ACTIVATION-SHIM" + "(ingest)"
_CANONICAL_ACTIVATOR_TYPE = "eventbridge_rule_" + "lambda_taskdef_arn_environment"
_EXACT_INGEST_IDENTITIES = {"ingest", "teamagent-dev-ingest-weekly"}
# 正名化後に「型ではなく consumer_id で分岐していない」ことを見る対象。
_TYPE_DRIVEN_MODULES = (
    "infra/terraform/ecs_service_apply_saga.py",
    "infra/terraform/image_release_context.py",
)


def _repository_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / item for item in result.stdout.decode("utf-8").split("\0") if item]


def _repository_file_bytes(path: Path) -> bytes:
    if path.is_symlink():
        return os.readlink(path).encode("utf-8")
    return path.read_bytes()


def _string_literals(node: ast.AST) -> set[str]:
    return {
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }


def test_no_activation_shim_tag_remains_anywhere() -> None:
    key = _SHIM_KEY.encode("utf-8")
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in _repository_files()
        if key in _repository_file_bytes(path) and path != Path(__file__)
    ]

    assert offenders == []


def test_ingest_registry_declares_the_canonical_activator_type() -> None:
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    ingest = [c for c in registry["consumers"] if c["consumer_id"] == "ingest"]

    assert len(ingest) == 1
    assert ingest[0]["activator"] == {
        "type": _CANONICAL_ACTIVATOR_TYPE,
        "identity": "teamagent-dev-ingest-weekly",
    }
    # 正名化は ingest 1 件だけ。他の 7 consumer の型は動かさない。
    assert sorted(c["activator"]["type"] for c in registry["consumers"]) == [
        "ecs_service",
        "ecs_service",
        "ecs_service",
        "eventbridge_rule_ecs_target",
        "eventbridge_rule_ecs_target",
        _CANONICAL_ACTIVATOR_TYPE,
        "lambda_taskdef_arn_environment",
        "lambda_taskdef_arn_environment",
    ]


def test_implementation_branches_on_the_type_not_on_the_consumer_id() -> None:
    for relative_path in _TYPE_DRIVEN_MODULES:
        path = ROOT / relative_path
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        offenders: list[int] = []
        for comparison in (n for n in ast.walk(tree) if isinstance(n, ast.Compare)):
            left = comparison.left
            for operator, right in zip(comparison.ops, comparison.comparators, strict=True):
                literals = _string_literals(left) | _string_literals(right)
                if isinstance(operator, (ast.Eq, ast.In, ast.NotIn)) and (
                    literals & _EXACT_INGEST_IDENTITIES
                ):
                    offenders.append(comparison.lineno)
                left = right
        assert offenders == [], f"{relative_path}: {offenders}"
        assert _CANONICAL_ACTIVATOR_TYPE in source, relative_path

    guard = (ROOT / "infra/deploy/terraform_runtime_guard.sh").read_text(encoding="utf-8")
    assert _CANONICAL_ACTIVATOR_TYPE in guard


def test_canonical_type_reaches_the_frozen_generation_surface() -> None:
    manifest = json.loads(GENERATION_INPUTS.read_text(encoding="utf-8"))
    frozen_inputs = set(manifest["inputs"])

    assert len(frozen_inputs) == 18
    assert "infra/codebuild/release_evidence.py" in frozen_inputs
    evidence = (ROOT / "infra/codebuild/release_evidence.py").read_text(encoding="utf-8")
    assert _CANONICAL_ACTIVATOR_TYPE in evidence
