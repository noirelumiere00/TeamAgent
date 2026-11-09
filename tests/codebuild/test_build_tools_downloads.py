"""Exercise the actual download snippets with shell functions in place of AWS/curl."""

import hashlib
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
BUILDSPECS = (
    "image-attestor-buildspec.yml",
    "openclaw-provenance-buildspec.yml",
    "image-promoter-buildspec.yml",
    "tiktok-buildspec.yml",
)


def downloads():
    for name in BUILDSPECS:
        spec = yaml.safe_load((ROOT / "infra/codebuild" / name).read_text())
        for phase in spec["phases"].values():
            for command in phase["commands"]:
                for match in re.finditer(
                    r"^aws s3api get-object \\\n(?:[ \t]+.*\n)+", command, re.M
                ):
                    block = match[0]
                    if '"build-tools/' not in block:
                        continue
                    sha = re.search(r"\$([A-Z]+_SHA256)", block)[1]
                    check = next(
                        line
                        for line in command.splitlines()
                        if line.startswith("printf ") and f'"${sha}"' in line
                    )
                    yield pytest.param(block, check, id=f"{name}-{sha}")


@pytest.mark.parametrize("block,check", list(downloads()))
@pytest.mark.parametrize("source", ["s3", "github", "corrupt", "failed"])
def test_s3_fallback_keeps_digest_gate(tmp_path: Path, block: str, check: str, source: str) -> None:
    payload = "valid archive"
    sha256 = hashlib.sha256(payload.encode()).hexdigest()
    variables = "\n".join(
        f'{name}="{sha256 if name.endswith("SHA256") else "fixture"}"'
        for name in set(re.findall(r"\$\{?([A-Z_0-9]+)", block))
    )
    block = block.replace("/tmp/", f"{tmp_path}/")
    check = check.replace("/tmp/", f"{tmp_path}/")
    # These functions cannot invoke the real clients, including on a failing path.
    script = f"""set -euo pipefail
{variables}
aws() {{
  printf '%s' '{"bad bytes" if source == "corrupt" else payload}' > "${{!#}}"
  return {0 if source in {"s3", "corrupt"} else 1}
}}
curl() {{
  printf called > '{tmp_path}/curl-called'
  while [ "$1" != '--output' ]; do shift; done
  printf '%s' '{payload}' > "$2"
  return {1 if source == "failed" else 0}
}}
{block}
{check}
"""
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert (result.returncode == 0) == (source in {"s3", "github"}), result.stderr
    assert (tmp_path / "curl-called").exists() == (source in {"github", "failed"})
    assert "--retry 6 --retry-delay 20 --retry-all-errors --retry-max-time 600" in block
    assert "--connect-timeout 10 --max-time 90" in block
    assert "--expected-bucket-owner 718959508629" in block


def test_all_release_downloads_are_covered() -> None:
    blocks = list(downloads())
    assert len(blocks) == 8
    for name in BUILDSPECS:
        body = (ROOT / "infra/codebuild" / name).read_text()
        assert body.count("--retry-all-errors") == len(
            re.findall(r'https://github.com/[^"\s]+/releases/download/', body)
        )
