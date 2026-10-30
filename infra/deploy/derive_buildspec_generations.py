#!/usr/bin/env python3
"""Render buildspec generation hashes offline using the repository's Terraform locals.

Only ``terraform console`` runs, in an empty temporary module without a backend,
providers, state, or credentials. KMS ARNs come from the frozen input manifest.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

BUILDSPECS = {
    "teamagent-dev-image-builder": "image_builder_buildspec",
    "teamagent-dev-mcp-source-publisher": "mcp_source_publisher_buildspec",
    "teamagent-dev-image-attestor": "image_attestor_buildspec",
    "teamagent-dev-image-promoter": "image_promoter_buildspec",
    "teamagent-dev-approval-publisher": "approval_publisher_buildspec",
}
SUPPORT_LOCALS = {
    "runtime_contract_sha256",
    "mcp_release_contract_path",
    "mcp_release_contract_sha256",
    "expected_build_account_id",
    "expected_build_region",
    "approval_evidence_prefix",
}


def terraform_module(repo_root: Path, manifest: dict[str, Any]) -> str:
    """Copy the actual render expressions, including Terraform heredoc semantics."""
    module = repo_root.resolve() / "infra/terraform"
    declarations: dict[str, str] = {}
    for filename in ("codebuild.tf", "mcp_approval.tf"):
        source = (module / filename).read_text(encoding="utf-8")
        for match in re.finditer(
            r"^  ([a-z][a-z0-9_]*)\s*=.*?(?=^  [a-z][a-z0-9_]*\s*=|^})",
            source,
            re.MULTILINE | re.DOTALL,
        ):
            name = match[1]
            if name not in SUPPORT_LOCALS and not any(
                re.fullmatch(re.escape(base) + r"(?:_\d+)?", name) for base in BUILDSPECS.values()
            ):
                continue
            if name in declarations:
                raise ValueError(f"duplicate render local: {name}")
            declarations[name] = match[0]
    missing = (set(BUILDSPECS.values()) | SUPPORT_LOCALS) - declarations.keys()
    if missing:
        raise ValueError(f"missing render locals: {sorted(missing)}")
    text = "locals {\n" + "\n".join(declarations.values()) + "\n}\n"
    # Preserve the source module's path while evaluating in the isolated module.
    text = text.replace("${path.module}", module.as_posix())
    text = re.sub(
        r"aws_kms_key\.([a-z0-9_]+)\.arn",
        lambda match: json.dumps(manifest["kms_keys"][match[1]]),
        text,
    )
    return text


def derive(repo_root: Path, manifest: dict[str, Any]) -> dict[str, str]:
    module = terraform_module(repo_root, manifest)
    expression = (
        "jsonencode({"
        + ",".join(
            f"{json.dumps(project)}=sha256(local.{name})" for project, name in BUILDSPECS.items()
        )
        + "})\n"
    )
    with tempfile.TemporaryDirectory(prefix="teamagent-generation-") as temporary:
        root = Path(temporary)
        (root / "main.tf").write_text(module, encoding="utf-8")
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("AWS_", "TF_", "TFE_"))
        }
        environment.update(TF_IN_AUTOMATION="1", CHECKPOINT_DISABLE="1")
        result = subprocess.run(
            ["terraform", f"-chdir={root}", "console", "-no-color"],
            input=expression,
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        if result.returncode:
            raise ValueError(f"offline Terraform rendering failed:\n{result.stdout}{result.stderr}")
        values = json.loads(json.loads(result.stdout))
    if set(values) != set(BUILDSPECS) or any(
        not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
        for value in values.values()
    ):
        raise ValueError("offline Terraform returned invalid generation hashes")
    return cast(dict[str, str], values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(
            (args.repo_root / "infra/deploy/buildspec_generation_inputs.json").read_text(
                encoding="utf-8"
            )
        )
        values = derive(args.repo_root, manifest)
    except (OSError, ValueError, KeyError) as error:
        print(f"generation derivation failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(values, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
