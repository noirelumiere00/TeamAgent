#!/usr/bin/env python3
"""Render the three codebuild.tf replace chains offline, using the manifest's KMS ARNs.

No backend, providers, init, or AWS calls are used. Approval publisher's separate
heredoc and the OpenClaw/TikTok buildspecs are outside these three chains.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


def render_buildspecs(repo_root: Path) -> dict[str, str]:
    manifest = json.loads(
        (repo_root / "infra/deploy/buildspec_generation_inputs.json").read_text(encoding="utf-8")
    )
    source = (repo_root / "infra/terraform/codebuild.tf").read_text(encoding="utf-8")
    start = source.index("locals {\n  mcp_source_publisher_buildspec_1 = replace(")
    end = source.index('\nresource "aws_codebuild_project" "mcp_source_publisher"', start)
    source = source[start:end]
    for name, arn in manifest["kms_keys"].items():
        source = source.replace(f"aws_kms_key.{name}.arn", json.dumps(arn))
    if "aws_kms_key." in source:
        raise ValueError("generation manifest does not bind every referenced KMS key")

    with tempfile.TemporaryDirectory(prefix="buildspec-generation-") as temporary:
        module = Path(temporary)
        codebuild = module / "codebuild"
        codebuild.mkdir()
        for name in re.findall(r"\$\{path\.module\}/\.\./codebuild/([^\"]+)", source):
            (codebuild / name).write_bytes((repo_root / "infra/codebuild" / name).read_bytes())
        source = source.replace("${path.module}/../codebuild/", f"{codebuild}/")
        (module / "main.tf").write_text(source, encoding="utf-8")
        names = ("mcp_source_publisher", "image_attestor", "image_promoter")
        expression = (
            "jsonencode({" + ",".join(f"{name}=local.{name}_buildspec" for name in names) + "})\n"
        )
        environment = {key: value for key, value in os.environ.items() if not key.startswith("TF_")}
        environment.update(
            TF_DATA_DIR=str(module / "tf-data"),
            TF_CLI_CONFIG_FILE="/dev/null",
            CHECKPOINT_DISABLE="1",
        )
        result = subprocess.run(
            ["terraform", "console", "-no-color"],
            input=expression,
            cwd=module,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        return json.loads(json.loads(result.stdout))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    bodies = render_buildspecs(Path(__file__).resolve().parents[2])
    args.output_dir.mkdir(parents=True, exist_ok=True)
    generations = {}
    for name, body in bodies.items():
        project = f"teamagent-dev-{name.replace('_', '-')}"
        payload = body.encode("utf-8")
        (args.output_dir / f"{project}.yml").write_bytes(payload)
        generations[project] = hashlib.sha256(payload).hexdigest()
    print(json.dumps(generations, indent=2))


if __name__ == "__main__":
    main()
