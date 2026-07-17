from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "infra/docker/Dockerfile.openclaw"
ENTRYPOINT = ROOT / "infra/docker/openclaw-entrypoint.mjs"
CONFIG = ROOT / "infra/openclaw/openclaw.config.json5"
LOCK = ROOT / "infra/openclaw/plugins-lock.json"
HELPER = ROOT / "infra/openclaw/build-image.sh"
BUILDSPEC = ROOT / "infra/codebuild/buildspec.openclaw.yml"


def test_release_runtime_and_plugin_pins_are_aligned() -> None:
    lock = json.loads(LOCK.read_text())
    assert lock["openclaw"]["version"] == "2026.7.1"
    assert lock["openclaw"]["releaseTag"] == "v2026.7.1"
    assert lock["runtime"]["nodeVersion"].startswith("24.")
    assert lock["runtime"]["uid"] == lock["runtime"]["gid"] == 65532
    assert lock["tooling"]["trivy"]["version"] == "0.72.0"
    assert {plugin["version"] for plugin in lock["plugins"]} == {"2026.7.1"}
    assert {plugin["gitHead"] for plugin in lock["plugins"]} == {lock["openclaw"]["releaseCommit"]}
    for plugin in lock["plugins"]:
        assert re.fullmatch(r"[0-9a-f]{64}", plugin["sha256"])
        assert re.fullmatch(r"[0-9a-f]{64}", plugin["shrinkwrapSha256"])


def test_dockerfile_uses_exact_arm64_children_and_distroless_final() -> None:
    lock = json.loads(LOCK.read_text())
    dockerfile = DOCKERFILE.read_text()
    for value in (
        lock["openclaw"]["linuxArm64Digest"],
        lock["runtime"]["linuxArm64Digest"],
        lock["tooling"]["dockerfileFrontend"]["digest"],
        *(plugin["sha256"] for plugin in lock["plugins"]),
        *(plugin["shrinkwrapSha256"] for plugin in lock["plugins"]),
    ):
        assert value in dockerfile
    assert "FROM gcr.io/distroless/nodejs24-debian13:nonroot@" in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert 'VOLUME ["/tmp"]' in dockerfile
    assert 'ENTRYPOINT ["/nodejs/bin/node", "/opt/teamagent/entrypoint.mjs"]' in dockerfile
    assert "npm install" not in dockerfile
    assert not re.search(r"\b(?:apt|apk|dnf|yum)\b.*install", dockerfile)
    for secret in (
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "OPENCLAW_GATEWAY_TOKEN",
        "TEAMAGENT_MCP_BEARER",
    ):
        assert f"ARG {secret}" not in dockerfile


def test_config_loads_only_reviewed_external_plugins_and_not_browser() -> None:
    config = CONFIG.read_text()
    assert 'allow: ["slack", "amazon-bedrock"]' in config
    assert '"/opt/teamagent/plugins/slack"' in config
    assert '"/opt/teamagent/plugins/amazon-bedrock"' in config
    assert not re.search(r"^\s*browser\s*:", config, flags=re.MULTILINE)


def test_entrypoint_is_readonly_and_secret_safe() -> None:
    entrypoint = ENTRYPOINT.read_text()
    assert not (ROOT / "infra/docker/openclaw-entrypoint.sh").exists()
    assert 'runtimeRoot !== "/tmp/teamagent-openclaw"' in entrypoint
    assert '"/opt/teamagent/state-seed/openclaw.sqlite"' in entrypoint
    assert 'const templatePath = "/opt/teamagent/openclaw.template.json"' in entrypoint
    assert "await chmod(runtimeRoot, 0o700)" in entrypoint
    assert "process.getuid" in entrypoint
    assert "allowFromCount" in entrypoint
    for secret in (
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "OPENCLAW_GATEWAY_TOKEN",
        "TEAMAGENT_MCP_BEARER",
    ):
        assert f"process.env.{secret}" not in entrypoint
    assert "writeFile(templatePath" not in entrypoint


def test_dedicated_builder_is_fail_closed_and_scans_child() -> None:
    helper = HELPER.read_text()
    for required in (
        "refusing to build a dirty or untracked source tree",
        "SOURCE_ARCHIVE_SHA256",
        "SOURCE_ARTIFACT_VERSION",
        "--platform linux/arm64",
        "--provenance=mode=max",
        '--sbom="generator=$SBOM_GENERATOR"',
        "expected exactly one linux/arm64 child",
        '"containerimage.digest"',
        "--scanners vuln",
        "--scanners secret",
        "TRIVY_CACHE_DIR",
        'TRIVY_VERSION" == "$EXPECTED_TRIVY_VERSION',
        "--read-only",
        "--cap-drop ALL",
        "gateway_args=(",
        "OPENCLAW_SKIP_CHANNELS=1",
    ):
        assert required in helper


def test_codebuild_uses_pinned_trivy_and_dedicated_helper() -> None:
    buildspec = BUILDSPEC.read_text()
    assert "v0.72.0" in buildspec
    assert "2ca2c023109c2db6b2b77366b6717291452d4531167377d95c79547f0c8e3467" in buildspec
    assert "infra/openclaw/build-image.sh" in buildspec
    assert "SOURCE_ARCHIVE_SHA256" in buildspec
    assert "CODEBUILD_SOURCE_VERSION" in buildspec
    assert 'Metadata["git-commit"]' in buildspec
    assert 'Metadata["source-sha256"]' in buildspec
    assert "SOURCE_ARTIFACT_VERSION" in buildspec
    assert "git-${SOURCE_COMMIT:0:12}" in buildspec
