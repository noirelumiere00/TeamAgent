"""Read pinned buildspec downloads without executing any buildspec shell code."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BUCKET = "teamagent-dev-image-release-evidence"
OWNER = "718959508629"
REGION = "ap-northeast-1"
BUILDSPECS = (
    "image-attestor-buildspec.yml",
    "openclaw-provenance-buildspec.yml",
    "image-promoter-buildspec.yml",
    "tiktok-buildspec.yml",
)
CURL = [
    "curl",
    "--proto",
    "=https",
    "--tlsv1.2",
    "--fail",
    "--silent",
    "--show-error",
    "--location",
    "--retry",
    "6",
    "--retry-delay",
    "20",
    "--retry-all-errors",
    "--retry-max-time",
    "600",
    "--connect-timeout",
    "10",
    "--max-time",
    "90",
]


def expand(text: str, values: dict[str, str]) -> str:
    for _ in range(len(values) + 1):
        if "$" not in text:
            return text
        expanded = re.sub(
            r"\$\{([A-Z_0-9]+)\}|\$([A-Z_0-9]+)",
            lambda item: values[item[1] or item[2]],
            text,
        )
        if expanded == text:
            break
        text = expanded
    raise ValueError("unsupported or cyclic shell substitution")


def inventory(root: Path) -> list[dict[str, str]]:
    tools = {}
    for name in BUILDSPECS:
        body = (root / "infra/codebuild" / name).read_text()
        values = dict(re.findall(r'^\s+([A-Z_0-9]+)="([^"\n]*)"$', body, re.M))
        # OpenClaw pins its tooling in a JSON lock rather than literal assignments.
        for variable, field, lock_var in re.findall(
            r'([A-Z_0-9]+)="\$\(jq -er \'(\.[^\']+)\' "\$([A-Z_0-9]+)"\)"', body
        ):
            lock_path = (root / values[lock_var]).resolve()
            if not lock_path.is_relative_to(root.resolve()):
                raise ValueError("lock path escapes repository")
            value = json.loads(lock_path.read_text())
            for part in field.lstrip(".").split("."):
                value = value[part]
            values[variable] = value

        urls = re.findall(r'"(https://github.com/[^"\s]+/releases/download/[^"\s]+)"', body)
        if not urls:
            raise ValueError(f"no release downloads in {name}")
        for template in urls:
            url = expand(template, values)
            match = re.fullmatch(
                r"https://github.com/[\w.-]+/([\w.-]+)/releases/download/"
                r"v([0-9]+\.[0-9]+\.[0-9]+)/([\w.-]+)",
                url,
            )
            if not match:
                raise ValueError(f"unsupported release URL: {url}")
            tool, version, filename = match.groups()
            sha256 = values[f"{tool.upper()}_SHA256"]
            if not re.fullmatch(r"[0-9a-f]{64}", sha256):
                raise ValueError(f"invalid SHA-256 in {name}: {tool}")
            if version != values[f"{tool.upper()}_VERSION"]:
                raise ValueError(f"version mismatch in {name}: {tool}")
            key = f"build-tools/{sha256}/{filename}"
            entry = {"url": url, "sha256": sha256, "key": key}
            if key in tools and tools[key] != entry:
                raise ValueError(f"conflicting release URLs for {key}")
            tools[key] = entry
    return list(tools.values())


def mirror(tools: list[dict[str, str]], kms_key: str, *, apply: bool) -> None:
    if not re.fullmatch(rf"arn:aws:kms:{REGION}:{OWNER}:key/[0-9a-f-]+", kms_key):
        raise ValueError("unexpected evidence KMS key")
    if not apply:
        for tool in tools:
            print(f"PLAN verify {tool['url']} -> s3://{BUCKET}/{tool['key']} (skip if present)")
        return
    # Match the buildspec trust boundary, including profile-configured endpoints.
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("AWS_ENDPOINT_URL")
    }
    env["AWS_IGNORE_CONFIGURED_ENDPOINT_URLS"] = "true"
    aws = ["aws", "--region", REGION, "--cli-connect-timeout", "10", "--cli-read-timeout", "30"]
    retain_until = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=3650)).isoformat()
    with tempfile.TemporaryDirectory(prefix="mirror-build-tools-") as temporary:
        for tool in tools:
            common = ["--bucket", BUCKET, "--key", tool["key"], "--expected-bucket-owner", OWNER]
            head = subprocess.run(
                [*aws, "s3api", "head-object", *common],
                capture_output=True,
                text=True,
                env=env,
            )
            if head.returncode == 0:
                print(f"SKIP s3://{BUCKET}/{tool['key']}")
                continue
            if not re.search(r"\((404|NoSuchKey|NotFound)\)", head.stderr):
                raise RuntimeError(f"head-object failed: {head.stderr.strip()}")
            archive = Path(temporary) / tool["key"].split("/")[-1]
            subprocess.run([*CURL, "--output", str(archive), tool["url"]], check=True, env=env)
            if hashlib.sha256(archive.read_bytes()).hexdigest() != tool["sha256"]:
                raise ValueError(f"SHA-256 mismatch: {tool['url']}")
            subprocess.run(
                [
                    *aws,
                    "s3api",
                    "put-object",
                    *common,
                    "--body",
                    str(archive),
                    "--if-none-match",
                    "*",
                    "--checksum-algorithm",
                    "SHA256",
                    "--server-side-encryption",
                    "aws:kms",
                    "--ssekms-key-id",
                    kms_key,
                    "--object-lock-mode",
                    "GOVERNANCE",
                    "--object-lock-retain-until-date",
                    retain_until,
                ],
                check=True,
                env=env,
            )
            print(f"PUT s3://{BUCKET}/{tool['key']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="download, verify and upload missing tools"
    )
    args = parser.parse_args()
    manifest = json.loads((ROOT / "infra/deploy/buildspec_generation_inputs.json").read_text())
    mirror(inventory(ROOT), manifest["kms_keys"]["image_release_evidence"], apply=args.apply)


if __name__ == "__main__":
    main()
