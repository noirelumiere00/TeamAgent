from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import subprocess
import tarfile
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
TRUST_CONTRACT = ROOT / "infra/openclaw/trusted-release-contract.json"
FILESYSTEM_SBOM = ROOT / "infra/openclaw/generate-filesystem-sbom.py"
EVIDENCE_INDEXER = ROOT / "infra/openclaw/index-evidence.py"
PLUGIN_OPERATION_SMOKE = ROOT / "infra/openclaw/plugin-operation-smoke.mjs"
ROLLOUT_TASK_CANARY = ROOT / "infra/openclaw/rollout-task-canary.mjs"
ROLLOUT_GATE = ROOT / "infra/openclaw/run-live-rollout-gates.mjs"
CLOUDWATCH_FARGATE = ROOT / "infra/terraform/cloudwatch_fargate.tf"
TASK_FIXTURE = ROOT / "tests/fixtures/openclaw/current-task-definition.json"
ROLLOUT_FIXTURE = ROOT / "tests/fixtures/openclaw/rollout-gates-pass.json"
STARTUP_LOG_FIXTURE = ROOT / "tests/fixtures/openclaw/startup-log-events.jsonl"


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
    for plugin in lock["plugins"]:
        assert "gitHead" not in plugin
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
    assert "computeProductionPackageClosure" in pruner
    assert "closureComputedBeforeMetadataRewrite" in pruner
    assert "excludedForbiddenDeclarations" in pruner
    assert "computePluginOperationModuleClosure" in pruner
    assert "postPruneClosureExactMatch" in pruner
    assert "unresolvedComputedImports" in pruner
    assert "collectModuleGraph" in pruner
    assert "controlUiModuleRoots" in pruner
    assert "Vite's preload dependency map" in pruner
    assert "collectControlUiFullAssetClosure" in pruner
    assert "controlUiReachableModuleAssets" in pruner
    assert "controlUiServedAssets" in pruner
    assert "controlUiDynamicAssetRegistrationsCoveredByWholeTree" in pruner
    assert "controlUiBootstrapConfigRuntimeContractRequired" in pruner
    assert "controlUiMissingLocalImports" in pruner
    assert "preservedControlUiBrowserChunks" in pruner
    assert "retainedFailClosedFacade" in pruner
    assert "disableJitiExtensionSourceTransformLoader" in pruner
    assert "disableTypeScriptCodeModeCompiler" in pruner
    assert "jitiExtensionSourceTransformFacade" in pruner
    assert "typeScriptCodeModeCompilerFacade" in pruner
    assert "servedSha256" in pruner
    assert "servedSize" in pruner
    assert 'data-openclaw-terminal-enabled="false"' in pruner
    assert "reachableBrowserPayloadZero: false" in pruner
    assert "usableBrowserControlPath: false" in pruner
    assert "browserImplementationSignals" in pruner
    assert 'const SKILLS_ROOT = path.join(APP_ROOT, "skills")' in pruner
    assert pruner.count("SKILLS_ROOT,") == 2
    assert "bench(?:marks?)?" in pruner
    assert "bench|benchmark|test|spec" in pruner
    assert "registerBrowserPlugin" in pruner
    assert "browserHelpSourceSignature" in pruner
    assert "runtime-prune-report.json" in pruner
    assert (
        "infra/openclaw/rollout-task-canary.mjs "
        "/opt/teamagent/rollout-task-canary.mjs"
    ) in dockerfile
    assert (
        "infra/openclaw/effective-tool-scope.json "
        "/opt/teamagent/effective-tool-scope.json"
    ) in dockerfile
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
    assert config["gateway"]["terminal"] == {"enabled": False}
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
        "shared trusted-release source verifier is absent",
        "repository integration contract differs from the shared trusted contract",
        "SOURCE_ARCHIVE_SHA256",
        "SOURCE_ARTIFACT_VERSION",
        "SOURCE_URI",
        "BUILD_IDENTITY",
        "BUILDX_VERSION",
        "--platform linux/arm64",
        "--provenance=false --load",
        "build metadata source/material validation failed",
        "EXPECTED_MATERIALS",
        "exactSetMatch",
        "extraExecutableOrRemoteMaterialDetected",
        '"containerimage.digest"',
        "docker export",
        "--format cyclonedx",
        "generate-filesystem-sbom.py",
        "pathTypeModeOwnerSizeLinkContentMultisetExact",
        "wholeFilesystemExactMatch",
        "physicalNpmMultisetExactMatch:true",
        "SBOM npm path/name/version multiset",
        "aquasecurity:trivy:FilePath",
        "bomRefIntegrity",
        "--scanners vuln",
        "--scanners secret",
        "CVE-2026-34182",
        "CVE-2026-55200",
        "candidate does not eliminate the observed live C8/H22 findings",
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
        "plugin-operation-smoke.mjs",
        "plugin-operation-smoke.json",
        "sbom.cdx.json",
        "sbom-npm-inventory.json",
        "rootfs-inventory.json",
        "sbom-equivalence.json",
        "index-evidence.py",
        "evidence-index.json",
        "forbiddenPackageOrPluginArtifacts:0",
        "developmentPayloadArtifacts:0",
        "controlUiImportClosureValidated:true",
        "controlUiFullAssetClosureValidated:true",
        "controlUiMissingLocalImports",
        "controlUiServedAssets",
        "preservedControlUiBrowserChunks",
        "fargateNoNewPrivilegesEnforced:false",
        "schemaVersion:4",
        "deploymentCredential:false",
        'status:"LOCAL_GATES_PASSED"',
        "registryPublished:false",
        "canonicalTagPublished:false",
        "sharedPromoterRequired:true",
        "MANIFEST_SHA256",
        "evidence directory must be a sibling of the release manifest",
        "EVIDENCE_MANIFEST_PREFIX",
    ):
        assert required in helper
    for forbidden in (
        "docker push",
        "docker buildx imagetools create",
        "--push",
        "REGISTRY_PROVENANCE",
        "REGISTRY_SBOM",
        "ATTESTATION_DIGEST",
        "SOURCE_COMMIT=${SOURCE_COMMIT",
        "SOURCE_BRANCH=${SOURCE_BRANCH",
    ):
        assert forbidden not in helper
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
    assert "SOURCE_ARTIFACT_VERSION" in buildspec
    assert "git-${SOURCE_COMMIT:0:12}" in buildspec
    assert "/opt/teamagent/trusted-release/bin/trusted-release" in buildspec
    assert (
        "/opt/teamagent/trusted-release/contracts/"
        "teamagent-openclaw-production-v1.json"
    ) in buildspec
    assert "verify-source" in buildspec
    assert "--source-statement" in buildspec
    assert "--source-root" in buildspec
    assert "--transport-version" in buildspec
    assert "sourceRootReverified" in buildspec
    assert "exactMaterialSetVerified" in buildspec
    assert "wholeFilesystemSbomExact" in buildspec
    assert "allEvidenceFilesBound" in buildspec
    assert ".scan.critical == 0" in buildspec
    assert ".scan.high == 0" in buildspec
    assert ".scan.secrets == 0" in buildspec
    assert ".scan.exactSingleLinuxArm64Subject == true" in buildspec
    assert ".scan.allKnownLiveFindingsAbsent == true" in buildspec
    assert "CODEBUILD_BUILD_ID" in buildspec
    assert buildspec.index("build-image.sh") < buildspec.index("promote-openclaw")
    assert "openclaw-build-evidence" in buildspec
    assert 'resource "aws_codebuild_project" "openclaw_image"' in terraform
    assert (
        'buildspec = file("${path.module}/../codebuild/buildspec.openclaw.yml")'
        in terraform
    )
    assert 'resource "aws_iam_role" "codebuild_openclaw"' in terraform
    assert "service_role = aws_iam_role.codebuild_openclaw.arn" in terraform
    openclaw_policy = terraform.split(
        'data "aws_iam_policy_document" "codebuild_openclaw"',
        1,
    )[1].split('resource "aws_iam_role_policy" "codebuild_openclaw"', 1)[0]
    for forbidden_permission in (
        "ecr:GetAuthorizationToken",
        "ecr:PutImage",
        "ecr:InitiateLayerUpload",
        "sts:AssumeRole",
    ):
        assert forbidden_permission not in openclaw_policy
    assert "aws_ecr_repository.openclaw.arn" not in terraform
    assert '"codebuild/openclaw-evidence"' in terraform
    legacy_project = terraform.split('resource "aws_codebuild_project" "openclaw_image"', 1)[0]
    assert "buildspec.openclaw.yml" not in legacy_project
    assert "openclaw-release/openclaw-build-evidence" in buildspec
    for forbidden in ("docker push", "docker login", "aws ecr", "Metadata["):
        assert forbidden not in buildspec


def _render_task(task: dict[str, Any], image: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["jq", "--arg", "image", image, "-f", str(TASK_FILTER)],
        input=json.dumps(task),
        check=False,
        text=True,
        capture_output=True,
    )


def test_authoritative_deploy_renderer_enforces_real_fargate_contract(
    tmp_path: Path,
) -> None:
    current_task = json.loads(TASK_FIXTURE.read_text())
    image = (
        "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/"
        f"teamagent-openclaw@sha256:{'b' * 64}"
    )
    rendered_process = _render_task(current_task, image)
    assert rendered_process.returncode == 0, rendered_process.stderr
    rendered = json.loads(rendered_process.stdout)
    assert rendered["runtimePlatform"] == {
        "cpuArchitecture": "ARM64",
        "operatingSystemFamily": "LINUX",
    }
    assert rendered["requiresCompatibilities"] == ["FARGATE"]
    assert rendered["volumes"] == [{"name": "openclaw-tmp"}]
    assert len(rendered["containerDefinitions"]) == 1
    container = rendered["containerDefinitions"][0]
    assert container["image"] == image
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

    adversarial: list[tuple[str, dict[str, Any]]] = []

    mutated = copy.deepcopy(current_task)
    mutated["containerDefinitions"].append(
        copy.deepcopy(mutated["containerDefinitions"][0])
    )
    mutated["containerDefinitions"][1]["name"] = "sidecar"
    adversarial.append(("sidecar", mutated))

    mutated = copy.deepcopy(current_task)
    mutated["volumes"].append({"name": "data"})
    adversarial.append(("extra volume", mutated))

    mutated = copy.deepcopy(current_task)
    mutated["volumes"] = [{"name": "openclaw-tmp", "host": {"sourcePath": "/tmp"}}]
    adversarial.append(("host volume", mutated))

    mutated = copy.deepcopy(current_task)
    mutated["containerDefinitions"][0]["mountPoints"].append(
        {"sourceVolume": "openclaw-tmp", "containerPath": "/data", "readOnly": False}
    )
    adversarial.append(("writable data mount", mutated))

    mutated = copy.deepcopy(current_task)
    mutated["containerDefinitions"][0]["mountPoints"].append(
        {"sourceVolume": "openclaw-tmp", "containerPath": "/readonly", "readOnly": True}
    )
    adversarial.append(("unapproved read-only mount", mutated))

    mutated = copy.deepcopy(current_task)
    mutated["containerDefinitions"][0]["environment"][0]["value"] = "us-east-1"
    adversarial.append(("region retarget", mutated))

    mutated = copy.deepcopy(current_task)
    mutated["containerDefinitions"][0]["environment"].append(
        {"name": "ECS_SERVICE", "value": "retargeted"}
    )
    adversarial.append(("environment retarget", mutated))

    mutated = copy.deepcopy(current_task)
    mutated["containerDefinitions"][0]["secrets"].append(
        copy.deepcopy(mutated["containerDefinitions"][0]["secrets"][0])
    )
    adversarial.append(("duplicate secret", mutated))

    mutated = copy.deepcopy(current_task)
    mutated["containerDefinitions"][0]["portMappings"] = [{"containerPort": 18789}]
    adversarial.append(("unexpected container field", mutated))

    mutated = copy.deepcopy(current_task)
    mutated["ephemeralStorage"] = {"sizeInGiB": 200}
    adversarial.append(("unexpected task field", mutated))

    mutated = copy.deepcopy(current_task)
    mutated["family"] = "attacker-family"
    adversarial.append(("family retarget", mutated))

    mutated = copy.deepcopy(current_task)
    mutated["taskDefinitionArn"] = (
        "arn:aws:ecs:ap-northeast-1:718959508629:"
        "task-definition/attacker-family:53"
    )
    adversarial.append(("task ARN retarget", mutated))

    mutated = copy.deepcopy(current_task)
    mutated["taskRoleArn"] = "arn:aws:iam::718959508629:role/admin"
    adversarial.append(("task role retarget", mutated))

    for label, candidate in adversarial:
        rejected = _render_task(candidate, image)
        assert rejected.returncode != 0, label
        assert "OpenClaw task definition:" in rejected.stderr, label

    wrong_image = _render_task(
        current_task,
        f"attacker.invalid/openclaw@sha256:{'c' * 64}",
    )
    assert wrong_image.returncode != 0
    assert "fixed OpenClaw repository" in wrong_image.stderr

    shared_cli = Path("/opt/teamagent/trusted-release/bin/trusted-release")
    if not shared_cli.exists():
        receipt_path = tmp_path / "untrusted-manifest.json"
        receipt_path.write_text("{}\n")
        fail_closed = subprocess.run(
            [
                "bash",
                str(DEPLOY_HELPER),
                "--render-only",
                str(TASK_FIXTURE),
                str(receipt_path),
            ],
            check=False,
            text=True,
            capture_output=True,
        )
        assert fail_closed.returncode != 0
        assert "shared trusted-release verifier is absent" in fail_closed.stderr


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
    service_start = source.index('resource "aws_ecs_service" "openclaw"')
    service_block = source[service_start:]
    assert "deployment_circuit_breaker" in service_block
    assert "enable   = true" in service_block
    assert "rollback = true" in service_block


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
        "servedAssetInventorySha256",
        "controlUiServedAssets",
        "manifest.webmanifest",
        "ProviderIcon-bedrock.svg",
        "browser",
        "--help",
        "genericOpenClawCliRetained",
        "browserNamedSharedPayloadHonestlyReported",
        "browserBridgeFacadeFailClosed",
        "startBrowserBridgeServer",
        "genericChildProcessPrimitives",
        "no bundled plugin manifest found for browser",
        "pluginOperationModulesLoadWithStubbedProviders",
        "conversations.history",
        "ListFoundationModelsCommand",
        "actual-image-contract.json",
    ):
        assert required in source


def test_task_hardening_filter_and_deploy_helper_do_not_claim_fargate_nnp() -> None:
    task_filter = TASK_FILTER.read_text()
    deploy = DEPLOY_HELPER.read_text()
    contract = json.loads(TRUST_CONTRACT.read_text())
    assert "readonlyRootFilesystem" in task_filter
    assert 'drop: ["ALL"]' in task_filter
    assert 'containerPath: "/tmp"' in task_filter
    assert "dockerSecurityOptions" in task_filter
    assert "del(.entryPoint, .command, .dockerSecurityOptions)" in task_filter
    assert "expected exactly one container named openclaw; sidecars are forbidden" in task_filter
    assert "only the task-scoped empty openclaw-tmp volume is allowed" in task_filter
    assert "environment contains an unapproved name or value" in task_filter
    assert "This script never decides deployability from a release manifest" in deploy
    assert "shared trusted-release verifier is absent; render/deploy is blocked" in deploy
    assert "inspect-deployment-receipt" in deploy
    assert "verify-deployment-plan" in deploy
    assert "consume-deployment-receipt" in deploy
    assert "record-deployment-result" in deploy
    assert ".verification.kmsSignatureVerified == true" in deploy
    assert ".verification.fresh == true" in deploy
    assert ".verification.oneTime == true" in deploy
    assert ".verification.unconsumed == true" in deploy
    assert ".release.exactMaterialsVerified == true" in deploy
    assert ".release.wholeFilesystemSbomExact == true" in deploy
    assert ".release.allEvidenceFilesBound == true" in deploy
    assert ".release.canonicalPromotionVerified == true" in deploy
    assert ".release.platformManifestCount == 1" in deploy
    assert "CVE-2026-34182" in deploy
    assert "CVE-2026-55200" in deploy
    assert ".deployment.previousTaskDefinitionArn == $currentArn" in deploy
    assert "--render-only" in deploy
    assert "register-task-definition" in deploy
    assert "deploymentCircuitBreaker={enable=true,rollback=true}" in deploy
    assert "alarm_and_restore" in deploy
    assert "run-live-rollout-gates.mjs" in deploy
    assert "OpenClawRolloutGateFailure" in deploy
    assert "terraform apply" not in deploy
    assert "OC_REPO" not in deploy
    assert "ECS_SERVICE=${" not in deploy
    assert "ECS_FAMILY=${" not in deploy
    assert "no-new-privileges" not in task_filter
    assert contract["source"]["allowArbitraryS3Zip"] is False
    assert contract["source"]["allowBuilderProducedVerificationBooleans"] is False
    assert contract["promotion"]["builderRoleMayAuthenticateOrWriteRegistry"] is False
    assert contract["promotion"]["requiredPlatformManifestCount"] == 1
    assert contract["deploymentReceipt"]["oneTime"] is True
    assert contract["deploymentReceipt"]["requireAtomicConsume"] is True
    assert (
        len(
            contract["deploymentReceipt"]["requiredScanStatus"][
                "knownLiveFindingIdsAbsent"
            ]
        )
        == 8
    )


def test_openclaw_startup_alarm_matches_both_exact_log_fixtures() -> None:
    terraform = CLOUDWATCH_FARGATE.read_text()
    events = [
        json.loads(line)
        for line in STARTUP_LOG_FIXTURE.read_text().splitlines()
        if line.strip()
    ]
    assert [event["event"] for event in events] == [
        "openclaw_entrypoint_error",
        "openclaw_config_invariant_violation",
        "openclaw_runtime_ready",
    ]
    matched = [
        event
        for event in events
        if event["event"]
        in {
            "openclaw_entrypoint_error",
            "openclaw_config_invariant_violation",
        }
    ]
    assert len(matched) == 2
    assert (
        'pattern        = "{ $.event = \\"openclaw_config_invariant_violation\\" '
        '|| $.event = \\"openclaw_entrypoint_error\\" }"'
    ) in terraform
    assert 'name          = "OpenClawStartupFailure"' in terraform
    assert 'metric_name         = "OpenClawStartupFailure"' in terraform
    assert 'metric_name         = "OpenClawRolloutGateFailure"' in terraform
    assert '"openclaw_entrypoint_error"' in ENTRYPOINT.read_text()


def test_rollout_gate_contract_is_fail_closed_without_provider_calls(
    tmp_path: Path,
) -> None:
    fixture = json.loads(ROLLOUT_FIXTURE.read_text())
    passed = subprocess.run(
        ["node", str(ROLLOUT_GATE), "--validate-fixture", str(ROLLOUT_FIXTURE)],
        check=False,
        text=True,
        capture_output=True,
    )
    assert passed.returncode == 0, passed.stderr
    assert json.loads(passed.stdout)["passed"] is True

    mutations: list[tuple[str, dict[str, Any]]] = []
    changed = copy.deepcopy(fixture)
    changed["consumption"]["atomic"] = False
    mutations.append(("replay/consume", changed))
    changed = copy.deepcopy(fixture)
    changed["service"]["services"][0]["deploymentConfiguration"][
        "deploymentCircuitBreaker"
    ]["rollback"] = False
    mutations.append(("circuit breaker", changed))
    changed = copy.deepcopy(fixture)
    changed["task"]["tasks"][0]["taskDefinitionArn"] = fixture["expected"][
        "previousTaskDefinition"
    ]
    mutations.append(("wrong task", changed))
    changed = copy.deepcopy(fixture)
    changed["taskEvent"]["mcp"]["toolNamesSha256"] = "0" * 64
    mutations.append(("tool scope", changed))
    changed = copy.deepcopy(fixture)
    changed["taskEvent"]["bedrock"]["credentialSource"] = "AWS_ACCESS_KEY_ID"
    mutations.append(("static credentials", changed))

    for index, (label, candidate) in enumerate(mutations):
        candidate_path = tmp_path / f"rollout-{index}.json"
        candidate_path.write_text(json.dumps(candidate))
        rejected = subprocess.run(
            ["node", str(ROLLOUT_GATE), "--validate-fixture", str(candidate_path)],
            check=False,
            text=True,
            capture_output=True,
        )
        assert rejected.returncode != 0, label

    task_canary = ROLLOUT_TASK_CANARY.read_text()
    rollout = ROLLOUT_GATE.read_text()
    for required in (
        "ConverseCommand",
        "ECS task-role credential endpoint is unavailable",
        'method: "tools/list"',
        "expectedToolNames",
        "toolNamesSha256",
        "static AWS credential variables reached the rollout task",
    ):
        assert required in task_canary
    for required in (
        "teamagent-dev-openclaw",
        "deploymentCircuitBreaker",
        "run-task",
        "tasks-stopped",
        "openclaw_rollout_task_canary",
        "teamagent/dev/openclaw/rollout-canary",
        "chat.postMessage",
        "conversations.replies",
        "mentionReplyExact",
    ):
        assert required in rollout
    assert "process.env.ECS_SERVICE" not in rollout
    assert "process.env.ECS_CLUSTER" not in rollout
    assert "process.env.CANARY_SECRET" not in rollout


def _minimal_trivy_sbom(image_id: str) -> dict[str, Any]:
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": "urn:uuid:00000000-0000-4000-8000-000000000001",
        "version": 1,
        "metadata": {
            "component": {
                "type": "container",
                "name": "fixture",
                "bom-ref": "urn:fixture:image",
                "properties": [
                    {"name": "aquasecurity:trivy:ImageID", "value": image_id}
                ],
            },
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "group": "aquasecurity",
                        "name": "trivy",
                        "version": "0.72.0",
                    }
                ]
            },
        },
        "components": [],
        "dependencies": [{"ref": "urn:fixture:image", "dependsOn": []}],
    }


def test_whole_filesystem_sbom_and_evidence_index_are_exact(tmp_path: Path) -> None:
    rootfs_tar = tmp_path / "rootfs.tar"
    with tarfile.open(rootfs_tar, "w") as archive:
        directory = tarfile.TarInfo("app")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        directory.uid = 65532
        directory.gid = 65532
        archive.addfile(directory)

        payload = b"runtime\n"
        regular = tarfile.TarInfo("app/index.js")
        regular.size = len(payload)
        regular.mode = 0o444
        regular.uid = 65532
        regular.gid = 65532
        archive.addfile(regular, io.BytesIO(payload))

        symlink = tarfile.TarInfo("app/current.js")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = "index.js"
        symlink.mode = 0o777
        symlink.uid = 65532
        symlink.gid = 65532
        archive.addfile(symlink)

    image_id = f"sha256:{'a' * 64}"
    manifest_digest = f"sha256:{'b' * 64}"
    config_digest = f"sha256:{'c' * 64}"
    trivy_path = tmp_path / "trivy.cdx.json"
    trivy_path.write_text(json.dumps(_minimal_trivy_sbom(image_id)))
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    inventory = evidence_dir / "rootfs-inventory.json"
    sbom = evidence_dir / "sbom.cdx.json"
    equivalence = evidence_dir / "sbom-equivalence.json"
    generated = subprocess.run(
        [
            "python3",
            str(FILESYSTEM_SBOM),
            "--rootfs-tar",
            str(rootfs_tar),
            "--trivy-sbom",
            str(trivy_path),
            "--inventory-output",
            str(inventory),
            "--sbom-output",
            str(sbom),
            "--equivalence-output",
            str(equivalence),
            "--image-id",
            image_id,
            "--manifest-digest",
            manifest_digest,
            "--config-digest",
            config_digest,
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    assert generated.returncode == 0, generated.stderr
    inventory_json = json.loads(inventory.read_text())
    equivalence_json = json.loads(equivalence.read_text())
    assert inventory_json["entryCount"] == 3
    assert {
        (entry["path"], entry["type"]) for entry in inventory_json["entries"]
    } == {
        ("app", "directory"),
        ("app/current.js", "symlink"),
        ("app/index.js", "file"),
    }
    file_entry = next(
        entry for entry in inventory_json["entries"] if entry["path"] == "app/index.js"
    )
    assert file_entry["contentSha256"] == hashlib.sha256(b"runtime\n").hexdigest()
    assert equivalence_json["wholeFilesystemExactMatch"] is True
    assert (
        equivalence_json["pathTypeModeOwnerSizeLinkContentMultisetExact"] is True
    )

    index_path = evidence_dir / "evidence-index.json"
    indexed = subprocess.run(
        [
            "python3",
            str(EVIDENCE_INDEXER),
            "--evidence-dir",
            str(evidence_dir),
            "--output",
            str(index_path),
            "--image-id",
            image_id,
            "--manifest-digest",
            manifest_digest,
            "--config-digest",
            config_digest,
            "--rootfs-inventory-sha256",
            hashlib.sha256(inventory.read_bytes()).hexdigest(),
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    assert indexed.returncode == 0, indexed.stderr
    index = json.loads(index_path.read_text())
    assert index["allRegularEvidenceFilesBound"] is True
    assert index["entryCount"] == len(index["entries"])
    assert {entry["path"] for entry in index["entries"]} == {
        "rootfs-inventory.json",
        "sbom-equivalence.json",
        "sbom.cdx.json",
    }

    (evidence_dir / "forbidden-link").symlink_to(sbom)
    rejected_index = subprocess.run(
        [
            "python3",
            str(EVIDENCE_INDEXER),
            "--evidence-dir",
            str(evidence_dir),
            "--output",
            str(evidence_dir / "second-index.json"),
            "--image-id",
            image_id,
            "--manifest-digest",
            manifest_digest,
            "--config-digest",
            config_digest,
            "--rootfs-inventory-sha256",
            hashlib.sha256(inventory.read_bytes()).hexdigest(),
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    assert rejected_index.returncode != 0
    assert "evidence symlink is forbidden" in rejected_index.stderr
