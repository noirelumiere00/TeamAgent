from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TERRAFORM_DIR = ROOT / "infra" / "terraform"

# This is deliberately an audited allowlist of the ECR actions used by this
# module, rather than a permissive pattern. Adding an action requires an
# explicit review here, which catches typos and nonexistent IAM actions that
# `terraform validate` accepts as opaque strings.
AUDITED_ECR_ACTIONS = {
    "ecr:*",
    "ecr:BatchCheckLayerAvailability",
    "ecr:BatchDeleteImage",
    "ecr:BatchGetImage",
    "ecr:CompleteLayerUpload",
    "ecr:CreateRepository",
    "ecr:DeleteLifecyclePolicy",
    "ecr:DeleteRepository",
    "ecr:DeleteRepositoryPolicy",
    "ecr:DescribeImageScanFindings",
    "ecr:DescribeImages",
    "ecr:DescribeRepositories",
    "ecr:GetAuthorizationToken",
    "ecr:GetDownloadUrlForLayer",
    "ecr:GetLifecyclePolicy",
    "ecr:GetRepositoryPolicy",
    "ecr:InitiateLayerUpload",
    "ecr:ListTagsForResource",
    "ecr:PutImage",
    "ecr:PutImageScanningConfiguration",
    "ecr:PutImageTagMutability",
    "ecr:PutLifecyclePolicy",
    "ecr:SetRepositoryPolicy",
    "ecr:TagResource",
    "ecr:UntagResource",
    "ecr:UploadLayerPart",
}


def _hcl_block(body: str, opening_brace: int) -> str:
    depth = 0
    in_string = False
    escaped = False
    line_comment = False
    block_comment = False
    index = opening_brace
    while index < len(body):
        character = body[index]
        following = body[index + 1] if index + 1 < len(body) else ""
        if line_comment:
            if character == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if character == "*" and following == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
        elif character == "#":
            line_comment = True
        elif character == "/" and following == "/":
            line_comment = True
            index += 1
        elif character == "/" and following == "*":
            block_comment = True
            index += 1
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return body[opening_brace : index + 1]
        index += 1
    raise AssertionError("unterminated HCL block")


def test_every_terraform_ecr_iam_action_is_explicitly_audited() -> None:
    actions: set[str] = set()
    locations: dict[str, set[str]] = {}
    for path in sorted(TERRAFORM_DIR.glob("*.tf")):
        body = path.read_text(encoding="utf-8")
        for action in re.findall(r'"(ecr:[A-Za-z*]+)"', body):
            actions.add(action)
            locations.setdefault(action, set()).add(path.name)

    unknown = actions - AUDITED_ECR_ACTIONS
    assert not unknown, {action: sorted(locations[action]) for action in sorted(unknown)}
    assert "ecr:ListImageReferrers" not in actions


def test_batch_get_image_permissions_are_repository_scoped() -> None:
    statement = re.compile(r"\bstatement\s*(\{)")
    discovered = 0
    for path in sorted(TERRAFORM_DIR.glob("*.tf")):
        body = path.read_text(encoding="utf-8")
        for match in statement.finditer(body):
            block = _hcl_block(body, match.start(1))
            if '"ecr:BatchGetImage"' not in block:
                continue
            discovered += 1
            assert not re.search(r"resources\s*=\s*\[\s*\"\*\"\s*\]", block), (
                f"{path.name} grants ecr:BatchGetImage without repository scope"
            )

    assert discovered > 0


def test_ecr_wildcards_are_confined_to_explicit_deny_statements() -> None:
    statement = re.compile(r"\bstatement\s*(\{)")
    wildcard_statements = 0
    for path in sorted(TERRAFORM_DIR.glob("*.tf")):
        body = path.read_text(encoding="utf-8")
        for match in statement.finditer(body):
            block = _hcl_block(body, match.start(1))
            if '"ecr:*"' not in block:
                continue
            wildcard_statements += 1
            assert re.search(r'effect\s*=\s*"Deny"', block), path.name

    assert wildcard_statements > 0
