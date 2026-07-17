from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "infra/docker/Dockerfile.openclaw"
ENTRYPOINT = ROOT / "infra/docker/openclaw-entrypoint.mjs"
CONFIG = ROOT / "infra/openclaw/openclaw.config.json5"
LOCK = ROOT / "infra/openclaw/plugins-lock.json"
HELPER = ROOT / "infra/openclaw/build-image.sh"
PRUNER = ROOT / "infra/openclaw/prune-runtime.mjs"
GATEWAY_RUNTIME = ROOT / "infra/openclaw/gateway-runtime.mjs"
BUILDSPEC = ROOT / "infra/codebuild/buildspec.openclaw.yml"
CODEBUILD_TF = ROOT / "infra/terraform/codebuild.tf"
COMPOSE = ROOT / "infra/openclaw/docker-compose.yml"
README = ROOT / "infra/openclaw/README.md"
RUNBOOK = ROOT / "docs/openclaw/deploy_runbook.md"
TOOL_SCOPE = ROOT / "infra/openclaw/effective-tool-scope.json"
FARGATE = ROOT / "infra/terraform/fargate.tf"
TASK_FILTER = ROOT / "infra/openclaw/harden-task-definition.jq"
DEPLOY_HELPER = ROOT / "infra/terraform/apply_openclaw.sh"
ACTUAL_IMAGE_TEST = ROOT / "tests/scripts/test_openclaw_runtime_image.py"


def _strip_json5_comments(source: str) -> str:
    output: list[str] = []
    index = 0
    quote: str | None = None
    escaped = False
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if quote is not None:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
            output.append(char)
            index += 1
            continue
        if char == "/" and next_char == "/":
            while index < len(source) and source[index] != "\n":
                index += 1
            continue
        if char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(source) and source[index : index + 2] != "*/":
                if source[index] == "\n":
                    output.append("\n")
                index += 1
            index += 2
            continue
        output.append(char)
        index += 1
    assert quote is None, "unterminated JSON5 string"
    return "".join(output)


def _load_reviewed_json5(path: Path) -> dict[str, Any]:
    source = _strip_json5_comments(path.read_text())
    assert not re.search(r"(?<![A-Za-z0-9_])'(?:[^'\\]|\\.)*'", source), (
        "the minimal reviewed JSON5 parser does not accept single-quoted strings"
    )
    source = re.sub(
        r"(?P<prefix>(?:^|[,{])\s*)(?P<key>[A-Za-z_$][A-Za-z0-9_$]*)(?=\s*:)",
        lambda match: f'{match.group("prefix")}"{match.group("key")}"',
        source,
        flags=re.MULTILINE,
    )
    source = re.sub(r",(\s*[}\]])", r"\1", source)
    parsed = json.loads(source)
    assert isinstance(parsed, dict)
    return parsed


def _js_string_array(source: str, constant: str) -> list[str]:
    match = re.search(
        rf"const {re.escape(constant)} = \[(?P<body>.*?)\];",
        source,
        flags=re.DOTALL,
    )
    assert match, f"missing JavaScript array constant: {constant}"
    return re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', match.group("body"))


def _docker_arg_values(dockerfile: str, name: str) -> list[str]:
    return re.findall(rf"^ARG {re.escape(name)}=([^\s]+)$", dockerfile, flags=re.MULTILINE)


def test_release_runtime_and_plugin_pins_are_aligned() -> None:
    lock = json.loads(LOCK.read_text())
    assert lock["schemaVersion"] == 1
    assert lock["openclaw"]["version"] == "2026.7.1"
    assert lock["openclaw"]["releaseTag"] == "v2026.7.1"
    assert re.fullmatch(r"[0-9a-f]{40}", lock["openclaw"]["releaseCommit"])
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", lock["openclaw"]["imageIndexDigest"])
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", lock["openclaw"]["linuxArm64Digest"])
    assert lock["runtime"]["nodeVersion"].startswith("24.")
    assert lock["runtime"]["uid"] == lock["runtime"]["gid"] == 65532
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", lock["runtime"]["imageIndexDigest"])
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", lock["runtime"]["linuxArm64Digest"])
    assert lock["tooling"]["trivy"]["version"] == "0.72.0"
    assert lock["tooling"]["sbomGenerator"]["image"] == ("docker.io/docker/buildkit-syft-scanner")
    assert re.fullmatch(
        r"sha256:[0-9a-f]{64}",
        lock["tooling"]["sbomGenerator"]["linuxArm64Digest"],
    )
    assert lock["tooling"]["buildx"]["version"] == "0.33.0"
    assert re.fullmatch(
        r"[0-9a-f]{64}",
        lock["tooling"]["buildx"]["linuxArm64BinarySha256"],
    )
    assert {plugin["id"] for plugin in lock["plugins"]} == {"slack", "amazon-bedrock"}
    assert {plugin["version"] for plugin in lock["plugins"]} == {lock["openclaw"]["version"]}
    assert {plugin["gitHead"] for plugin in lock["plugins"]} == {lock["openclaw"]["releaseCommit"]}
    for plugin in lock["plugins"]:
        assert re.fullmatch(r"[0-9a-f]{64}", plugin["sha256"])
        assert re.fullmatch(r"[0-9a-f]{64}", plugin["shrinkwrapSha256"])
        assert plugin["tarball"].endswith(f"/{plugin['archive']}")
        assert plugin["integrity"].startswith("sha512-")


def test_dockerfile_uses_exact_arm64_children_and_distroless_final() -> None:
    lock = json.loads(LOCK.read_text())
    dockerfile = DOCKERFILE.read_text()
    pruner = PRUNER.read_text()
    gateway_runtime = GATEWAY_RUNTIME.read_text()
    assert dockerfile.splitlines()[0] == (
        f"# syntax=docker/dockerfile:1.7@{lock['tooling']['dockerfileFrontend']['digest']}"
    )
    assert _docker_arg_values(dockerfile, "OPENCLAW_VERSION") == [
        lock["openclaw"]["version"],
        lock["openclaw"]["version"],
    ]
    assert _docker_arg_values(dockerfile, "OPENCLAW_ARM64_DIGEST") == [
        lock["openclaw"]["linuxArm64Digest"],
        lock["openclaw"]["linuxArm64Digest"],
    ]
    assert _docker_arg_values(dockerfile, "DISTROLESS_ARM64_DIGEST") == [
        lock["runtime"]["linuxArm64Digest"],
        lock["runtime"]["linuxArm64Digest"],
    ]
    from_lines = re.findall(r"^FROM\s+(.+)$", dockerfile, flags=re.MULTILINE)
    assert from_lines == [
        "ghcr.io/openclaw/openclaw:${OPENCLAW_VERSION}@${OPENCLAW_ARM64_DIGEST} AS upstream",
        "gcr.io/distroless/nodejs24-debian13:nonroot@${DISTROLESS_ARM64_DIGEST} AS runtime",
    ]
    add_artifacts = {
        url: checksum
        for checksum, url in re.findall(
            r"^ADD --checksum=(sha256:[0-9a-f]{64}) \\\n\s+(\S+) /tmp/\S+$",
            dockerfile,
            flags=re.MULTILINE,
        )
    }
    assert add_artifacts == {
        plugin["tarball"]: f"sha256:{plugin['sha256']}" for plugin in lock["plugins"]
    }
    for plugin in lock["plugins"]:
        assert (
            f"sha256sum /opt/teamagent/plugins/{plugin['id']}/npm-shrinkwrap.json"
            if plugin["id"] == "slack"
            else "sha256sum /opt/teamagent/plugins/amazon-bedrock/npm-shrinkwrap.json"
        ) in dockerfile
        assert plugin["shrinkwrapSha256"] in dockerfile
    assert re.search(r"^USER 65532:65532$", dockerfile, flags=re.MULTILINE)
    assert re.search(r'^VOLUME \["/tmp"\]$', dockerfile, flags=re.MULTILINE)
    assert re.search(
        r'^ENTRYPOINT \["/nodejs/bin/node", "/opt/teamagent/entrypoint.mjs"\]$',
        dockerfile,
        flags=re.MULTILINE,
    )
    assert re.search(
        r'^CMD \["/opt/teamagent/gateway-runtime.mjs", "gateway", "--bind", "loopback", "--port", "18789"\]$',
        dockerfile,
        flags=re.MULTILINE,
    )
    assert ("infra/openclaw/gateway-runtime.mjs /opt/teamagent/gateway-runtime.mjs") in dockerfile
    assert 'NODE_DISABLE_COMPILE_CACHE: "1"' in gateway_runtime
    assert "delete gatewayEnv.NODE_COMPILE_CACHE" in gateway_runtime
    assert '"/app/openclaw.mjs", ...expectedArgs' in gateway_runtime
    assert 'typeof process.execve !== "function"' in gateway_runtime
    assert "COPY infra/openclaw/prune-runtime.mjs /tmp/prune-runtime.mjs" in dockerfile
    assert "node /tmp/prune-runtime.mjs" in dockerfile
    for forbidden_artifact in (
        '"@openclaw/browser-plugin"',
        '"@openai/codex"',
        '"@types/"',
        '"@vitest/"',
        '"jiti"',
        '"playwright-core"',
        '"typescript"',
        '"vite"',
        '"vitest"',
    ):
        assert forbidden_artifact in pruner
    for path_component in (
        '"extensions", "browser"',
        '"pnpm-workspace.yaml"',
        '"src"',
        '"node_modules", ".bin"',
        '"node_modules", ".pnpm"',
    ):
        assert path_component in pruner
    assert "delete metadata.devDependencies" in pruner
    assert "delete metadata.scripts" in pruner
    assert "markProductionPackage" in pruner
    assert "collectModuleGraph" in pruner
    assert "controlUiModuleRoots" in pruner
    assert "Vite's preload dependency map" in pruner
    assert "controlUiReachableAssets" in pruner
    assert "controlUiMissingLocalImports" in pruner
    assert "preservedControlUiBrowserChunks" in pruner
    assert "browserImplementationSignals" in pruner
    assert 'const SKILLS_ROOT = path.join(APP_ROOT, "skills")' in pruner
    assert pruner.count("SKILLS_ROOT,") == 2
    assert "bench(?:marks?)?" in pruner
    assert "bench|benchmark|test|spec" in pruner
    assert "registerBrowserPlugin" in pruner
    assert "browserHelpSourceSignature" in pruner
    assert "runtime-prune-report.json" in pruner
    assert "fs.writeFileSync(1,JSON.stringify({" in HELPER.read_text()
    assert "plugins registry --refresh" in dockerfile
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
    config = _load_reviewed_json5(CONFIG)
    assert config["plugins"]["allow"] == ["slack", "amazon-bedrock"]
    assert config["plugins"]["load"]["paths"] == [
        "/opt/teamagent/plugins/slack",
        "/opt/teamagent/plugins/amazon-bedrock",
    ]
    assert config["plugins"]["entries"] == {
        "slack": {"enabled": True},
        "amazon-bedrock": {"enabled": True},
    }
    assert config["channels"]["slack"]["botToken"] == "${SLACK_BOT_TOKEN}"
    assert config["channels"]["slack"]["appToken"] == "${SLACK_APP_TOKEN}"
    assert config["gateway"]["auth"]["token"] == "${OPENCLAW_GATEWAY_TOKEN}"
    assert config["gateway"]["bind"] == "loopback"
    assert config["tools"]["exec"]["mode"] == "deny"
    assert config["tools"]["fs"]["workspaceOnly"] is True
    assert "browser" not in config["plugins"]["entries"]
    assert "browser" not in config["tools"]


def test_entrypoint_is_readonly_secret_safe_and_environment_allowlisted() -> None:
    entrypoint = ENTRYPOINT.read_text()
    assert not (ROOT / "infra/docker/openclaw-entrypoint.sh").exists()
    assert 'runtimeRoot !== "/tmp/teamagent-openclaw"' in entrypoint
    assert '"/opt/teamagent/state-seed/openclaw.sqlite"' in entrypoint
    assert 'const templatePath = "/opt/teamagent/openclaw.template.json"' in entrypoint
    assert "await chmod(runtimeRoot, 0o700)" in entrypoint
    assert "process.getuid" in entrypoint
    assert "allowFromCount" in entrypoint
    required_secrets = {
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "OPENCLAW_GATEWAY_TOKEN",
        "TEAMAGENT_MCP_BEARER",
    }
    assert set(_js_string_array(entrypoint, "REQUIRED_SECRETS")) == required_secrets
    for secret in required_secrets:
        assert f"process.env.{secret}" not in entrypoint
    passthrough = set(_js_string_array(entrypoint, "PASSTHROUGH_ENV"))
    assert passthrough == {
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
        "AWS_EC2_METADATA_DISABLED",
        "AWS_EXECUTION_ENV",
        "ECS_AGENT_URI",
        "ECS_CONTAINER_METADATA_URI",
        "ECS_CONTAINER_METADATA_URI_V4",
        "AWS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "ALL_PROXY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "NODE_USE_ENV_PROXY",
        "all_proxy",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
    assert passthrough.isdisjoint(
        {
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "LD_PRELOAD",
            "NODE_OPTIONS",
            "NODE_PATH",
            "OPENCLAW_SKIP_CHANNELS",
        }
    )
    assert "env: process.env" not in entrypoint
    assert "env: childEnv" in entrypoint
    assert 'NODE_ENV: "production"' in entrypoint
    assert "AWS_DEFAULT_REGION: region" in entrypoint
    assert (
        "process.execve(process.execPath, [process.execPath, ...command], childEnv)" in entrypoint
    )
    assert "process.exit(128 + signalNumber)" in entrypoint
    assert "writeFile(templatePath" not in entrypoint


def test_dedicated_builder_is_fail_closed_and_scans_child() -> None:
    helper = HELPER.read_text()
    for required in (
        "refusing to build a dirty or untracked source tree",
        "SOURCE_ARCHIVE_SHA256",
        "SOURCE_ARTIFACT_VERSION",
        "SOURCE_URI",
        "ATTESTATION_BUILDER_ID",
        "BUILDX_VERSION",
        "--platform linux/arm64",
        "type=provenance,mode=max,builder-id=",
        "type=sbom,generator=",
        "expected exactly one arm64 attestation manifest",
        "pushed SBOM/provenance attestations are missing",
        "--format '{{ json .Provenance }}'",
        "--format '{{ json .SBOM }}'",
        "registry provenance source/builder/material validation failed",
        "registry SPDX attestation payload validation failed",
        "build metadata source/material validation failed",
        "expected exactly one linux/arm64 child",
        '"containerimage.digest"',
        "--format cyclonedx",
        'generator:{name:"trivy",version:$trivyVersion}',
        "physicalNpmMultisetExactMatch:true",
        "SBOM npm path/name/version multiset",
        "aquasecurity:trivy:FilePath",
        "bomRefIntegrity:true",
        "--scanners vuln",
        "--scanners secret",
        "TRIVY_CACHE_DIR",
        'TRIVY_VERSION" == "$EXPECTED_TRIVY_VERSION',
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--user 0:0",
        "privileged-path browser cache inventory failed",
        "gateway_args=(",
        "/readyz",
        "child environment allowlist failed",
        "gateway container isolation contract failed",
        "gateway PID 1 child-process contract failed",
        "gateway SIGTERM shutdown must exit 0",
        "fs.lstatSync",
        "danglingSymlinks",
        '"jiti"',
        '"@types/"',
        '"@openclaw/browser-plugin"',
        '"@openai/codex"',
        '"@vitest/"',
        "/app/dist/extensions/browser",
        "/app/node_modules/.bin",
        "/app/node_modules/.pnpm",
        "runtime-inventory.json",
        "actual-image-contract.json",
        "test_openclaw_runtime_image.py",
        "sbom.cdx.json",
        "sbom-npm-inventory.json",
        "forbiddenPackageOrPluginArtifacts:0",
        "developmentPayloadArtifacts:0",
        "browserReachabilityValidated:true",
        "controlUiImportClosureValidated:true",
        "controlUiHttpAssetClosureValidated:true",
        "controlUiMissingLocalImports",
        "controlUiReachableAssets",
        "preservedControlUiBrowserChunks",
        "fargateNoNewPrivilegesEnforced:false",
        "schemaVersion:3",
        "MANIFEST_SHA256",
        "evidence directory must be a sibling of the release manifest",
        "EVIDENCE_MANIFEST_PREFIX",
    ):
        assert required in helper
    assert "--sbom=false" not in helper
    assert "sbomGenerator:$sbomAttestationGenerator" not in helper
    assert 'format:"CycloneDX 1.6"' not in helper
    assert helper.count("sort_by(.path, .name, .version)") >= 3
    gateway_args = re.search(
        r"gateway_args=\((?P<body>.*?)gateway_launcher=",
        helper,
        flags=re.DOTALL,
    )
    assert gateway_args
    assert "OPENCLAW_SKIP_CHANNELS" not in gateway_args.group("body")
    assert "config.channels.slack.enabled=false" in helper


def test_legacy_compose_path_is_formally_decommissioned() -> None:
    compose = COMPOSE.read_text()
    assert "services: {}" in compose
    assert "intentionally decommissioned" in compose
    assert "build-image.sh" in compose
    for stale in (
        "2026" + ".6.1",
        "1000" + ":1000",
        "/healthz",
        "SLACK_BOT_TOKEN:",
        "SLACK_APP_TOKEN:",
    ):
        assert stale not in compose


def test_docs_require_verified_runtime_and_provenance_path() -> None:
    readme = README.read_text()
    runbook = RUNBOOK.read_text()
    combined = f"{readme}\n{runbook}"
    for required in (
        "2026.7.1",
        "65532",
        "/readyz",
        "OPENCLAW_GATEWAY_TOKEN",
        "TEAMAGENT_MCP_BEARER",
        "build-image.sh",
        "--evidence-dir",
        "Critical=0",
        "High=0",
        "Secrets=0",
        "apply_openclaw.sh",
        "effective-tool-scope.json",
        "physical npm package instance",
    ):
        assert required in combined
    assert "Dockerfile.openclaw       -t" not in runbook
    assert "docker-compose.yml up" not in combined
    assert "2026" + ".6.1" not in combined
    assert "1000" + ":1000" not in combined
    assert 'openclaw_image=""' in runbook
    assert "Fargate does not support Docker `no-new-privileges`" in runbook
    assert "This is **not read-only**" in runbook
    assert "tools=会社ナレッジ4のみ" not in runbook


def test_codebuild_uses_pinned_trivy_and_dedicated_helper() -> None:
    buildspec = BUILDSPEC.read_text()
    terraform = CODEBUILD_TF.read_text()
    assert "v0.72.0" in buildspec
    assert "2ca2c023109c2db6b2b77366b6717291452d4531167377d95c79547f0c8e3467" in buildspec
    assert "buildx-v0.33.0.linux-arm64" in buildspec
    assert "204dc28447d3bb48f42ed1ce5747e0885cd57e306506a39029311becdb1ef786" in buildspec
    assert "infra/openclaw/build-image.sh" in buildspec
    assert "SOURCE_ARCHIVE_SHA256" in buildspec
    assert "CODEBUILD_SOURCE_VERSION" in buildspec
    assert 'Metadata["git-commit"]' in buildspec
    assert 'Metadata["source-sha256"]' in buildspec
    assert "SOURCE_ARTIFACT_VERSION" in buildspec
    assert "git-${SOURCE_COMMIT:0:12}" in buildspec
    assert "ATTESTATION_BUILDER_ID" in buildspec
    assert "CODEBUILD_BUILD_ID" in buildspec
    assert ".buildAttestations.subjectValidated == true" in buildspec
    assert ".buildAttestations.sourceValidated == true" in buildspec
    assert ".buildAttestations.builderValidated == true" in buildspec
    assert "openclaw-build-evidence" in buildspec
    assert 'resource "aws_codebuild_project" "openclaw_image"' in terraform
    assert 'buildspec = "infra/codebuild/buildspec.openclaw.yml"' in terraform
    assert '"codebuild/openclaw-evidence"' in terraform
    legacy_project = terraform.split('resource "aws_codebuild_project" "openclaw_image"', 1)[0]
    assert "buildspec.openclaw.yml" not in legacy_project
    assert "openclaw-release/openclaw-build-evidence" in buildspec


def _deployable_manifest() -> dict[str, Any]:
    repository = "123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-openclaw"
    digest = "sha256:" + "a" * 64
    return {
        "schemaVersion": 3,
        "image": {
            "requested": f"{repository}:git-0123456789ab",
            "runtimeRef": f"{repository}@{digest}",
            "manifestDigest": digest,
        },
        "runtime": {
            "platform": "linux/arm64",
            "uid": 65532,
            "gid": 65532,
            "actualImageContractPassed": True,
            "forbiddenPackageOrPluginArtifacts": 0,
            "developmentPayloadArtifacts": 0,
            "browserReachabilityValidated": True,
            "controlUiImportClosureValidated": True,
            "controlUiHttpAssetClosureValidated": True,
        },
        "scan": {"critical": 0, "high": 0, "secrets": 0},
        "sbom": {
            "format": "CycloneDX 1.7",
            "physicalNpmMultisetExactMatch": True,
        },
        "buildAttestations": {
            "registryPublished": True,
            "subjectValidated": True,
            "sourceValidated": True,
            "builderValidated": True,
            "builderId": (
                "https://codebuild.ap-northeast-1.amazonaws.com/"
                "builds/teamagent-dev-openclaw-image-builder:build-id"
            ),
        },
    }


def _write_manifest_with_checksum(path: Path, manifest: dict[str, Any]) -> None:
    payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    path.write_text(payload)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    Path(f"{path}.sha256").write_text(f"{digest}  {path}\n")


def test_authoritative_deploy_renderer_enforces_real_fargate_contract(
    tmp_path: Path,
) -> None:
    current_task = {
        "family": "teamagent-dev-openclaw",
        "taskRoleArn": "arn:aws:iam::123456789012:role/openclaw-task",
        "executionRoleArn": "arn:aws:iam::123456789012:role/openclaw-exec",
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": "512",
        "memory": "1024",
        "runtimePlatform": {
            "cpuArchitecture": "ARM64",
            "operatingSystemFamily": "LINUX",
        },
        "volumes": [{"name": "preserved-volume"}],
        "containerDefinitions": [
            {
                "name": "openclaw",
                "image": "old:mutable",
                "essential": True,
                "user": "0",
                "readonlyRootFilesystem": False,
                "privileged": True,
                "entryPoint": ["node"],
                "command": ["dist/index.js", "gateway"],
                "dockerSecurityOptions": ["no-new-privileges"],
                "linuxParameters": {
                    "tmpfs": [{"containerPath": "/tmp"}],
                    "capabilities": {"add": ["SYS_ADMIN"]},
                },
                "mountPoints": [],
                "environment": [
                    {"name": "AWS_REGION", "value": "ap-northeast-1"},
                    {
                        "name": "OPENCLAW_CONFIG_PATH",
                        "value": "/opt/teamagent/openclaw.json",
                    },
                    {"name": "SLACK_DM_ALLOWLIST", "value": "U123"},
                ],
                "secrets": [
                    {"name": "TEAMAGENT_MCP_BEARER", "valueFrom": "secret-a"},
                    {"name": "SLACK_BOT_TOKEN", "valueFrom": "secret-b"},
                    {"name": "SLACK_APP_TOKEN", "valueFrom": "secret-c"},
                    {"name": "OPENCLAW_GATEWAY_TOKEN", "valueFrom": "secret-d"},
                ],
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {"awslogs-group": "/teamagent/dev/openclaw"},
                },
            }
        ],
    }
    task_path = tmp_path / "task.json"
    task_path.write_text(json.dumps(current_task))
    manifest_path = tmp_path / "manifest.json"
    _write_manifest_with_checksum(manifest_path, _deployable_manifest())

    rendered_process = subprocess.run(
        [
            "bash",
            str(DEPLOY_HELPER),
            "--render-only",
            str(task_path),
            str(manifest_path),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    rendered = json.loads(rendered_process.stdout)
    assert rendered["runtimePlatform"] == {
        "cpuArchitecture": "ARM64",
        "operatingSystemFamily": "LINUX",
    }
    assert rendered["requiresCompatibilities"] == ["FARGATE"]
    assert {volume["name"] for volume in rendered["volumes"]} == {
        "preserved-volume",
        "openclaw-tmp",
    }
    container = next(
        item for item in rendered["containerDefinitions"] if item["name"] == "openclaw"
    )
    assert container["image"] == _deployable_manifest()["image"]["runtimeRef"]
    assert container["readonlyRootFilesystem"] is True
    assert container["user"] == "65532:65532"
    assert container["privileged"] is False
    assert container["linuxParameters"]["capabilities"] == {"drop": ["ALL"]}
    assert "tmpfs" not in container["linuxParameters"]
    assert "dockerSecurityOptions" not in container
    assert "entryPoint" not in container
    assert "command" not in container
    assert container["mountPoints"] == [
        {
            "sourceVolume": "openclaw-tmp",
            "containerPath": "/tmp",
            "readOnly": False,
        }
    ]
    assert "/readyz" in container["healthCheck"]["command"][3]
    assert container["stopTimeout"] == 30
    assert container["secrets"] == current_task["containerDefinitions"][0]["secrets"]
    assert (
        container["logConfiguration"] == current_task["containerDefinitions"][0]["logConfiguration"]
    )
    assert {entry["name"] for entry in container["environment"]} == {
        "AWS_REGION",
        "SLACK_DM_ALLOWLIST",
    }

    rejected_manifest = _deployable_manifest()
    rejected_manifest["buildAttestations"]["builderValidated"] = False
    _write_manifest_with_checksum(manifest_path, rejected_manifest)
    rejected = subprocess.run(
        [
            "bash",
            str(DEPLOY_HELPER),
            "--render-only",
            str(task_path),
            str(manifest_path),
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    assert rejected.returncode != 0
    assert "release manifest is not deployable" in rejected.stderr

    rejected_manifest = _deployable_manifest()
    rejected_manifest["runtime"]["controlUiHttpAssetClosureValidated"] = False
    _write_manifest_with_checksum(manifest_path, rejected_manifest)
    rejected = subprocess.run(
        [
            "bash",
            str(DEPLOY_HELPER),
            "--render-only",
            str(task_path),
            str(manifest_path),
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    assert rejected.returncode != 0
    assert "release manifest is not deployable" in rejected.stderr


def test_terraform_bootstrap_task_matches_cli_hardening_contract() -> None:
    source = FARGATE.read_text()
    start = source.index('resource "aws_ecs_task_definition" "openclaw"')
    end = source.index(
        "# ============================================================\n# Services",
        start,
    )
    block = source[start:end]
    for required in (
        'name = "openclaw-tmp"',
        "readonlyRootFilesystem = true",
        'user                   = "65532:65532"',
        "privileged             = false",
        'drop = ["ALL"]',
        'containerPath = "/tmp"',
        "readOnly      = false",
        "stopTimeout            = 30",
        "/readyz",
    ):
        assert required in block
    assert "dist/index.js" not in block
    assert "OPENCLAW_CONFIG_PATH" not in block
    assert "/healthz" not in block
    assert not re.search(r"^\s*dockerSecurityOptions\s*=", block, flags=re.MULTILINE)
    assert "linuxParameters.tmpfs" in block
    assert "サポートしない" in block


def test_effective_tool_scope_matches_config_and_deployment_gates() -> None:
    config = _load_reviewed_json5(CONFIG)
    scope = json.loads(TOOL_SCOPE.read_text())
    included = config["mcp"]["servers"]["teamagent"]["toolFilter"]["include"]
    inventory_names = [tool["name"] for tool in scope["tools"]]
    assert scope["schemaVersion"] == 1
    assert len(inventory_names) == len(set(inventory_names)) == 28
    assert set(inventory_names) == set(included)
    default_enabled = {tool["name"] for tool in scope["tools"] if tool["defaultEnabledByTerraform"]}
    assert default_enabled == {
        "search",
        "clientkarte",
        "proposal_draft",
        "proposal_review",
        "mail_summary",
        "mail_followup",
        "mail_to_internal_context",
        "mail_reply",
        "mail_draft",
        "morning_digest",
        "oauth_connect",
        "knowledge_deliver",
    }
    effects = {tool["effect"] for tool in scope["tools"]}
    assert "gmail-draft-write-no-send" in effects
    assert "calendar-write-no-invite" in effects
    assert "external-job-submit-s3-write" in effects
    fargate = FARGATE.read_text()
    for gate in (
        "USE_MAIL_SUMMARY_TOOL",
        "USE_FOLLOWUP_TOOL",
        "USE_MAIL_LINK_TOOL",
        "USE_MAIL_REPLY_TOOL",
        "USE_MAIL_DRAFT_TOOL",
        "USE_MORNING_DIGEST_TOOL",
        "USE_OAUTH_CONNECT_TOOL",
        "USE_KNOWLEDGE_DELIVER",
        "enable_scrape_tools",
        "enable_tiktok_acquire",
        "enable_x_research",
    ):
        assert gate in fargate
    for unwired_gate in (
        "USE_VIDEO_APPROVAL",
        "USE_OPERATION_LOG_TOOLS",
        "USE_KNOWLEDGE_SEARCH_URL_TOOL",
    ):
        assert unwired_gate not in fargate


def test_actual_image_test_is_executable_and_checks_kernel_and_payload() -> None:
    source = ACTUAL_IMAGE_TEST.read_text()
    for required in (
        "--read-only",
        "--cap-drop",
        "no-new-privileges",
        "/proc/self/status",
        "capBnd",
        "appWrite",
        "optWrite",
        "tmpWrite",
        "jitiResolvable",
        "browserHelpMetadata",
        "process.exit(42)",
        "/proc/1/task/1/children",
        "gatewayIsPid1",
        "gatewaySigtermExitZero",
        "gatewayRuntimeSecretLeakAbsent",
        "CONTROL_UI_HTTP_PROBE",
        "controlUiAssetClosureServed",
        "controlUiRuntimeSecretLeakAbsent",
        "servedModuleInventorySha256",
        "browser",
        "--help",
        "browserBridgeFacadeFailClosed",
        "startBrowserBridgeServer",
        "no bundled plugin manifest found for browser",
        "actual-image-contract.json",
    ):
        assert required in source


def test_task_hardening_filter_and_deploy_helper_do_not_claim_fargate_nnp() -> None:
    task_filter = TASK_FILTER.read_text()
    deploy = DEPLOY_HELPER.read_text()
    assert "readonlyRootFilesystem" in task_filter
    assert 'drop: ["ALL"]' in task_filter
    assert 'containerPath: "/tmp"' in task_filter
    assert "dockerSecurityOptions" in task_filter
    assert "del(.entryPoint, .command, .dockerSecurityOptions)" in task_filter
    assert "release manifest is not deployable" in deploy
    assert ".runtime.controlUiImportClosureValidated == true" in deploy
    assert ".runtime.controlUiHttpAssetClosureValidated == true" in deploy
    assert "--render-only" in deploy
    assert "register-task-definition" in deploy
    assert "terraform apply" not in deploy
