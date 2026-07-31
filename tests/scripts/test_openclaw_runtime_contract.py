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
BUNDLE_HELPER = ROOT / "infra/openclaw/build-bundle.sh"
PRUNER = ROOT / "infra/openclaw/prune-runtime.mjs"
GATEWAY_RUNTIME = ROOT / "infra/openclaw/gateway-runtime.mjs"
BUILDSPEC = ROOT / "infra/codebuild/openclaw-provenance-buildspec.yml"
CODEBUILD_TF = ROOT / "infra/terraform/codebuild.tf"
COMPOSE = ROOT / "infra/openclaw/docker-compose.yml"
README = ROOT / "infra/openclaw/README.md"
RUNBOOK = ROOT / "docs/openclaw/deploy_runbook.md"
TOOL_SCOPE = ROOT / "infra/openclaw/effective-tool-scope.json"
FARGATE = ROOT / "infra/terraform/fargate.tf"
FARGATE_VARIABLES = ROOT / "infra/terraform/variables_fargate.tf"
FARGATE_OUTPUTS = ROOT / "infra/terraform/outputs_fargate.tf"
TASK_FILTER = ROOT / "infra/openclaw/harden-task-definition.jq"
DEPLOY_HELPER = ROOT / "infra/terraform/apply_openclaw.sh"
ACTUAL_IMAGE_TEST = ROOT / "tests/scripts/test_openclaw_runtime_image.py"
TRUST_CONTRACT = ROOT / "infra/codebuild/openclaw_bundle_contract.json"
FILESYSTEM_SBOM = ROOT / "infra/openclaw/generate-filesystem-sbom.py"
EVIDENCE_INDEXER = ROOT / "infra/openclaw/index-evidence.py"
PLUGIN_OPERATION_SMOKE = ROOT / "infra/openclaw/plugin-operation-smoke.mjs"
ROLLOUT_TASK_CANARY = ROOT / "infra/openclaw/rollout-task-canary.mjs"
ROLLOUT_GATE = ROOT / "infra/openclaw/run-live-rollout-gates.mjs"
RUNTIME_GUARD = ROOT / "infra/deploy/terraform_runtime_guard.sh"
FORCED_ROLLBACK_DM_QA_PROBE = ROOT / "infra/deploy/forced_rollback_dm_qa_probe.py"
ROLLOUT_EVIDENCE_TF = ROOT / "infra/terraform/openclaw_rollout_evidence.tf"
RUNTIME_EVIDENCE_TF = ROOT / "infra/terraform/runtime_evidence.tf"
CLOUDWATCH_FARGATE = ROOT / "infra/terraform/cloudwatch_fargate.tf"
TASK_FIXTURE = ROOT / "tests/fixtures/openclaw/current-task-definition.json"
ROLLOUT_FIXTURE = ROOT / "tests/fixtures/openclaw/rollout-gates-pass.json"
STARTUP_LOG_FIXTURE = ROOT / "tests/fixtures/openclaw/startup-log-events.jsonl"
ADVERSARIAL_RUNBOOK = ROOT / "docs/openclaw/adversarial_harness_runbook.md"
SLACK_CANARY_SKIP_REASON_CODES = [
    "slack_self_authored_message_filtered",
    "aila_prompt_injection_defense_rejected_canary",
]
SLACK_CANARY_TOKEN_SHA256 = "34fc25aac72a3608fc4fe8c0914f128c51aa767b93d2d4ff4915d35aa9415e19"


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


def _bind_rollout_fixture_result_hash(fixture: dict[str, Any]) -> None:
    canonical_result = json.dumps(
        fixture["persistedResult"],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    fixture["immutableEvidence"]["resultSha256"] = hashlib.sha256(
        f"{canonical_result}\n".encode()
    ).hexdigest()


def _active_slack_canary_evidence(fixture: dict[str, Any]) -> dict[str, Any]:
    task = fixture["runningBefore"]["tasks"][0]
    task_id = task["taskArn"].rsplit("/", maxsplit=1)[-1]
    return {
        "connected": True,
        "mentionReplyExact": True,
        "responseTokenAbsentFromPrompt": True,
        "postedTs": "1784420000.100000",
        "replyTs": "1784420001.200000",
        "tokenSha256": SLACK_CANARY_TOKEN_SHA256,
        "correlationSha256": "9" * 64,
        "candidateLogCorrelation": {
            "matched": True,
            "taskArn": task["taskArn"],
            "logStreamName": f"openclaw/openclaw/{task_id}",
            "eventId": "fixture-event-1",
            "eventTimestamp": 1784420001200,
            "tokenSha256": SLACK_CANARY_TOKEN_SHA256,
        },
    }


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
    # The list stays exact so an unpinned or unexpected base cannot be added
    # unnoticed. upstream-templates is the same pinned digest as upstream, kept
    # unpruned so the workspace templates the runtime reads can be copied from
    # it; the pruning stage deletes /app/src.
    assert from_lines == [
        "ghcr.io/openclaw/openclaw:${OPENCLAW_VERSION}@${OPENCLAW_ARM64_DIGEST} AS upstream-templates",
        "ghcr.io/openclaw/openclaw:${OPENCLAW_VERSION}@${OPENCLAW_ARM64_DIGEST} AS upstream",
        "gcr.io/distroless/nodejs24-debian13:nonroot@${DISTROLESS_ARM64_DIGEST} AS runtime",
    ]
    # Every workspace template directory the runtime resolves must ship, or the
    # gateway starts healthy and then fails on the first message.
    for template_dir in ("/app/src/agents/templates", "/app/docs/reference/templates"):
        assert (
            f"COPY --from=upstream-templates --chown=65532:65532 \\\n"
            f"  {template_dir} {template_dir}\n"
        ) in dockerfile
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
    assert (
        "COPY infra/openclaw/caller-identity-plugin "
        "/opt/teamagent/plugins/teamagent-caller-identity/"
    ) in dockerfile
    assert "@teamagent/openclaw-caller-identity" in dockerfile
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
        "infra/openclaw/rollout-task-canary.mjs /opt/teamagent/rollout-task-canary.mjs"
    ) in dockerfile
    assert (
        "infra/openclaw/effective-tool-scope.json /opt/teamagent/effective-tool-scope.json"
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
        "TEAMAGENT_CALLER_CLAIM_SECRET",
    ):
        assert f"ARG {secret}" not in dockerfile


def test_config_loads_only_reviewed_plugins_and_not_browser() -> None:
    config = _load_reviewed_json5(CONFIG)
    assert config["plugins"]["allow"] == [
        "slack",
        "amazon-bedrock",
        "teamagent-caller-identity",
    ]
    assert config["plugins"]["load"]["paths"] == [
        "/opt/teamagent/plugins/slack",
        "/opt/teamagent/plugins/amazon-bedrock",
        "/opt/teamagent/plugins/teamagent-caller-identity",
    ]
    assert config["plugins"]["entries"] == {
        "slack": {"enabled": True},
        "amazon-bedrock": {"enabled": True},
        "teamagent-caller-identity": {
            "enabled": True,
            "hooks": {"allowConversationAccess": True},
        },
    }
    assert config["channels"]["slack"]["botToken"] == "${SLACK_BOT_TOKEN}"
    assert config["channels"]["slack"]["appToken"] == "${SLACK_APP_TOKEN}"
    assert config["channels"]["slack"]["dmPolicy"] == "open"
    assert config["channels"]["slack"]["allowFrom"] == ["*"]
    assert config["gateway"]["auth"]["token"] == "${OPENCLAW_GATEWAY_TOKEN}"
    assert config["gateway"]["bind"] == "loopback"
    assert config["gateway"]["terminal"] == {"enabled": False}
    assert config["tools"]["profile"] == "minimal"
    assert config["tools"]["alsoAllow"] == ["bundle-mcp"]
    assert set(config["tools"]["deny"]) == {
        "message",
        "read",
        "write",
        "edit",
        "apply_patch",
        "send",
        "delete",
        "upload",
        "sessions_list",
        "sessions_history",
        "sessions_send",
        "sessions_spawn",
        "sessions_yield",
        "subagents",
        "session_status",
    }
    assert config["tools"]["exec"]["mode"] == "deny"
    assert config["tools"]["fs"]["workspaceOnly"] is True
    assert "browser" not in config["plugins"]["entries"]
    assert "browser" not in config["tools"]


def test_internal_caller_identity_plugin_uses_installed_openclaw_schema() -> None:
    package = json.loads((ROOT / "infra/openclaw/caller-identity-plugin/package.json").read_text())
    plugin = (ROOT / "infra/openclaw/caller-identity-plugin/dist/index.js").read_text()
    assert package["openclaw"]["extensions"] == ["./dist/index.js"]
    assert "runtimeExtensions" not in package["openclaw"]
    assert package["openclaw"]["compat"]["pluginApi"] == ">=2026.7.1"
    assert "registerInteractiveHandler" in plugin
    assert "namespace: MAIL_DRAFT_ACTION_ID" in plugin
    assert "ctx?.auth?.isAuthorizedSender !== true" in plugin
    assert 'ctx?.trigger === "heartbeat"' in plugin
    assert "parseMailDraftSystemEvent" in plugin
    assert "interactionId !== expectedInteractionId" in plugin
    assert "mail_draft requires an authoritative Slack button action" in plugin


def test_entrypoint_is_readonly_secret_safe_and_environment_allowlisted() -> None:
    entrypoint = ENTRYPOINT.read_text()
    assert not (ROOT / "infra/docker/openclaw-entrypoint.sh").exists()
    assert 'runtimeRoot !== "/tmp/teamagent-openclaw"' in entrypoint
    assert '"/opt/teamagent/state-seed/openclaw.sqlite"' in entrypoint
    assert 'const templatePath = "/opt/teamagent/openclaw.template.json"' in entrypoint
    assert "await chmod(runtimeRoot, 0o700)" in entrypoint
    assert "process.getuid" in entrypoint
    assert "allowFromCount" in entrypoint
    assert "parseSlackDmAccess" in entrypoint
    assert (
        'runtimeSecrets.get("TEAMAGENT_CALLER_CLAIM_SECRET") ===\n'
        '    runtimeSecrets.get("TEAMAGENT_MCP_BEARER")'
    ) in entrypoint
    assert 'return { dmPolicy: "open", allowFrom: ["*"] }' in entrypoint
    assert 'return { dmPolicy: "allowlist", allowFrom: entries }' in entrypoint
    assert "config.channels.slack.dmPolicy = slackDmAccess.dmPolicy" in entrypoint
    assert "config.channels.slack.allowFrom = slackDmAccess.allowFrom" in entrypoint
    assert "is required; use" in entrypoint
    assert 'if (value === undefined || value === "")' in entrypoint
    assert "if (injectedAllowlist !== null)" not in entrypoint
    required_secrets = {
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "OPENCLAW_GATEWAY_TOKEN",
        "TEAMAGENT_MCP_BEARER",
        "TEAMAGENT_CALLER_CLAIM_SECRET",
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
        "detached builds require exact CodeBuild source identity",
        "detached build is not exact origin/dev",
        "SOURCE_TREE",
        "SOURCE_ARCHIVE_SHA256",
        "SOURCE_ARTIFACT_VERSION",
        "SOURCE_URI",
        "BUNDLE_CONTRACT_SHA256",
        "RELEASE_CONTRACT_SHA256",
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
        "schemaVersion:5",
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
    assert "local-validation-only helper" in compose
    assert "never pushes" in compose
    assert "trusted source-free promoter" in compose
    assert "valid signed active receipt" in compose
    assert "before any optional push" not in compose
    assert "consumes the verified digest recorded by" not in compose
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
        "TEAMAGENT_CALLER_CLAIM_SECRET",
        "TEAMAGENT_CALLER_CLAIM_REPLAY_TABLE",
        "SLACK_TEAM_ID",
        "build-image.sh",
        "--evidence-dir",
        "Critical=0",
        "High=0",
        "Secrets=0",
        "plan_image_release.sh",
        "apply_image_release_plan.sh",
        "effective-tool-scope.json",
        "physical npm package instance",
    ):
        assert required in combined
    assert "Dockerfile.openclaw       -t" not in runbook
    assert "docker-compose.yml up" not in combined
    assert "2026" + ".6.1" not in combined
    assert "1000" + ":1000" not in combined
    assert "release.ready=false" in runbook
    assert "Fargate は Docker `no-new-privileges` を強制できません" in runbook
    assert "scope is not read-only" in readme
    assert "tools=会社ナレッジ4のみ" not in runbook
    assert 'slack_dm_allowlist = "*"' in runbook
    assert "dmPolicy=allowlist" in combined
    assert "invalid production sentinel" in readme
    assert "merged export tar" in runbook
    assert "inventorySha256" in runbook
    assert "byte hash" in runbook


def test_docs_do_not_recommend_direct_ecs_rollback_or_stale_sections() -> None:
    outputs = FARGATE_OUTPUTS.read_text()
    adversarial = ADVERSARIAL_RUNBOOK.read_text()
    assert "--desired-count 0" not in outputs
    assert "direct ECS" in outputs
    assert "durable previous task revision" in outputs
    assert "saved-plan" in outputs
    assert "§I" not in adversarial
    assert "7. Post-apply functional gates" in adversarial
    assert "docs/v3.2/data_model_v1.md" in adversarial
    assert "scripts/migrate.py" in adversarial
    assert "schema_migrations" in adversarial


def test_all_current_openclaw_docs_reject_direct_desired_count_rollback() -> None:
    docs = [
        path
        for path in (ROOT / "docs").rglob("*")
        if path.is_file() and path.suffix.lower() in {".md", ".txt", ".html"}
    ]
    violations: list[str] = []
    for path in docs:
        text = path.read_text(errors="replace")
        if "openclaw" not in text.lower():
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            lowered = line.lower()
            if re.search(r"desired[_ -]?count\s*[=:]\s*0|--desired-count\s+0", lowered):
                violations.append(f"{path.relative_to(ROOT)}:{line_number}")
    assert violations == []
    for path in (
        ROOT / "docs/v3.2/ops/risk_register.md",
        ROOT / "docs/v3.2/spec_vs_current_full_matrix_2026-06-15.md",
    ):
        text = path.read_text()
        for required in (
            "durable previous task revision",
            "ECS deployment circuit breaker",
            "fresh signed rollback authorization",
            "one-use full saved plan",
        ):
            assert required in text


def test_effective_tool_scope_comments_name_real_side_effects() -> None:
    config = CONFIG.read_text()
    for required in (
        "Gmail下書き保存",
        "本人Calendar書込",
        "Slackファイル配信",
        "外部job/S3 report生成",
        "メール送信・招待送信・Drive書込は許可しない",
    ):
        assert required in config
    assert "MCP read only" not in config


def test_identity_dashboard_tracks_signed_claim_boundary_events() -> None:
    cloudwatch = CLOUDWATCH_FARGATE.read_text()
    for event in (
        "identity_resolved",
        "caller_claim_rejected",
        "identity_spoof_rejected",
    ):
        assert event in cloudwatch
    assert "identity_company_shared" not in cloudwatch
    assert "slack_user_id_audit" not in cloudwatch


def test_mcp_image_contract_does_not_claim_company_shared_is_identity_free() -> None:
    dockerfile = (ROOT / "infra/docker/Dockerfile.teamagent-mcp").read_text()
    assert "本人識別不要" not in dockerfile
    for required in ("署名済みSlack event", "member resolver成功", "fail closed"):
        assert required in dockerfile


def test_filesystem_evidence_uses_only_canonical_inventory_digest() -> None:
    generator = (ROOT / "infra/openclaw/generate-filesystem-sbom.py").read_text()
    helper = HELPER.read_text()
    runbook = RUNBOOK.read_text()
    for forbidden in (
        "rootfsTarSha256",
        "RootfsTarSha256",
        "ROOTFS_TAR_SHA256",
    ):
        assert forbidden not in f"{generator}\n{helper}"
    assert helper.count("mergedExportTarSha256") == 1
    assert 'has("mergedExportTarSha256") | not' in helper
    assert "rootfsInventorySha256" in helper
    assert "wholeFilesystemInventorySha256" in generator
    assert "唯一の digest claim" in runbook


def test_codebuild_and_local_runtime_have_one_fail_closed_release_boundary() -> None:
    buildspec = BUILDSPEC.read_text()
    terraform = CODEBUILD_TF.read_text()
    contract = json.loads(TRUST_CONTRACT.read_text())
    bundle_helper = BUNDLE_HELPER.read_text()

    # The contract is open now that the emitter and the measured core probes
    # landed. What must survive is the boundary itself, pinned below: exactly one
    # gate, and it runs before anything reaches a registry or a builder.
    assert contract["release"]["ready"] is True
    assert contract["bundle"]["interfaces"]["build"] == "infra/openclaw/build-bundle.sh"
    assert buildspec.index("assert-release-ready") < buildspec.index(
        "bash infra/openclaw/build-bundle.sh"
    )
    assert "create-source-manifest" in buildspec
    assert "verify-source-manifest" in buildspec
    assert "aws kms verify" in buildspec
    assert "teamagent-openclaw-quarantine:candidate-${SOURCE_COMMIT}-core" in buildspec
    assert "teamagent-openclaw-media-quarantine:candidate-${SOURCE_COMMIT}-media" in buildspec
    assert 'resource "aws_codebuild_project" "openclaw_provenance"' in terraform
    assert "service_role = aws_iam_role.openclaw_codebuild.arn" in terraform
    assert 'resource "aws_codebuild_project" "openclaw_image"' not in terraform
    assert not (ROOT / "infra/codebuild/buildspec.openclaw.yml").exists()
    # This used to pin the emitter's unconditional die.  The emitter is now
    # implemented, so the invariant it really protected is pinned directly: the
    # release gate must run before any external mutation, and nothing may reach
    # a registry or a builder ahead of it.
    assert bundle_helper.index("assert-release-ready") < bundle_helper.index("docker ")
    assert bundle_helper.index("assert-release-ready") < bundle_helper.index("aws ")


def test_blocked_bundle_interface_stops_before_output_or_external_work(
    tmp_path: Path,
) -> None:
    # The checked-in contract is open now, and the emitter accepts only the
    # canonical path, so block the canonical file in place and restore its exact
    # bytes afterwards. The invariant under test is the emitter's behaviour when
    # the gate is closed, not the current state of the file: a blocked contract
    # must stop before it writes a manifest or touches a registry, and say why.
    original_bytes = TRUST_CONTRACT.read_bytes()
    blocked_contract = json.loads(original_bytes)
    assert blocked_contract["release"]["ready"] is True
    blocked_contract["release"] = {
        "ready": False,
        "blocked_reason": "held closed for the fail-closed emitter regression test",
    }

    manifest = tmp_path / "bundle.json"
    try:
        TRUST_CONTRACT.write_text(json.dumps(blocked_contract, indent=2) + "\n", encoding="utf-8")
        completed = subprocess.run(
            [
                "bash",
                str(BUNDLE_HELPER),
                "--bundle-contract",
                str(TRUST_CONTRACT),
                "--core-image",
                "example.invalid/core:candidate",
                "--media-image",
                "example.invalid/media:candidate",
                "--push",
                "--manifest",
                str(manifest),
            ],
            cwd=ROOT,
            check=False,
            text=True,
            capture_output=True,
        )
    finally:
        TRUST_CONTRACT.write_bytes(original_bytes)

    assert TRUST_CONTRACT.read_bytes() == original_bytes
    assert completed.returncode != 0
    assert "OpenClaw core/media release is blocked" in completed.stderr
    assert "OpenClaw core/media release contract is not active" in completed.stderr
    assert not manifest.exists()


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
        f"718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-openclaw@sha256:{'b' * 64}"
    )
    rendered_process = _render_task(current_task, image)
    assert rendered_process.returncode == 0, rendered_process.stderr
    rendered = json.loads(rendered_process.stdout)
    assert rendered["runtimePlatform"] == {
        "cpuArchitecture": "ARM64",
        "operatingSystemFamily": "LINUX",
    }
    assert rendered["requiresCompatibilities"] == ["FARGATE"]
    assert rendered["volumes"] == current_task["volumes"]
    assert {volume["name"] for volume in rendered["volumes"]} == {"tmp", "state"}
    assert rendered["volumes"][1]["efsVolumeConfiguration"]["transitEncryption"] == "ENABLED"
    assert (
        rendered["volumes"][1]["efsVolumeConfiguration"]["authorizationConfig"]["iam"] == "ENABLED"
    )
    assert len(rendered["containerDefinitions"]) == 1
    container = rendered["containerDefinitions"][0]
    assert container["image"] == image
    assert container["readonlyRootFilesystem"] is True
    assert container["user"] == "65532:65532"
    assert container["privileged"] is False
    assert container["linuxParameters"] == {
        "initProcessEnabled": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert "tmpfs" not in container["linuxParameters"]
    assert "dockerSecurityOptions" not in container
    assert "entryPoint" not in container
    assert "command" not in container
    assert container["mountPoints"] == [
        {
            "sourceVolume": "tmp",
            "containerPath": "/tmp",
            "readOnly": False,
        },
        {
            "sourceVolume": "state",
            "containerPath": "/tmp/teamagent-openclaw/state",
            "readOnly": False,
        },
    ]
    assert container["healthCheck"]["command"][1] == "/nodejs/bin/node"
    assert "/readyz" in container["healthCheck"]["command"][3]
    assert container["stopTimeout"] == 120
    assert container["secrets"] == current_task["containerDefinitions"][0]["secrets"]
    assert (
        container["logConfiguration"] == current_task["containerDefinitions"][0]["logConfiguration"]
    )
    assert {entry["name"] for entry in container["environment"]} == {
        "AWS_REGION",
        "TMPDIR",
        "SLACK_DM_ALLOWLIST",
        "SLACK_TEAM_ID",
    }

    wildcard = copy.deepcopy(current_task)
    next(
        entry
        for entry in wildcard["containerDefinitions"][0]["environment"]
        if entry["name"] == "SLACK_DM_ALLOWLIST"
    )["value"] = "*"
    wildcard_rendered = _render_task(wildcard, image)
    assert wildcard_rendered.returncode == 0, wildcard_rendered.stderr

    exact_users = copy.deepcopy(current_task)
    next(
        entry
        for entry in exact_users["containerDefinitions"][0]["environment"]
        if entry["name"] == "SLACK_DM_ALLOWLIST"
    )["value"] = "U09CX1CCBLN,U0123456789"
    users_rendered = _render_task(exact_users, image)
    assert users_rendered.returncode == 0, users_rendered.stderr

    adversarial: list[tuple[str, dict[str, Any]]] = []

    mutated = copy.deepcopy(current_task)
    mutated["containerDefinitions"].append(copy.deepcopy(mutated["containerDefinitions"][0]))
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
    mutated["containerDefinitions"][0]["environment"] = [
        entry
        for entry in mutated["containerDefinitions"][0]["environment"]
        if entry["name"] != "SLACK_DM_ALLOWLIST"
    ]
    adversarial.append(("missing Slack DM allowlist", mutated))

    mutated = copy.deepcopy(current_task)
    mutated["containerDefinitions"][0]["environment"] = [
        entry
        for entry in mutated["containerDefinitions"][0]["environment"]
        if entry["name"] != "SLACK_TEAM_ID"
    ]
    adversarial.append(("missing Slack team", mutated))

    mutated = copy.deepcopy(current_task)
    next(
        entry
        for entry in mutated["containerDefinitions"][0]["environment"]
        if entry["name"] == "SLACK_TEAM_ID"
    )["value"] = "U0123456789"
    adversarial.append(("invalid Slack team", mutated))

    for label, value in (
        ("empty Slack DM allowlist", ""),
        ("whitespace Slack DM allowlist", " U09CX1CCBLN"),
        ("mixed wildcard Slack DM allowlist", "*,U09CX1CCBLN"),
        ("duplicate Slack DM allowlist", "U09CX1CCBLN,U09CX1CCBLN"),
        ("non-U Slack DM allowlist", "W0123456789"),
    ):
        mutated = copy.deepcopy(current_task)
        next(
            entry
            for entry in mutated["containerDefinitions"][0]["environment"]
            if entry["name"] == "SLACK_DM_ALLOWLIST"
        )["value"] = value
        adversarial.append((label, mutated))

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
        "arn:aws:ecs:ap-northeast-1:718959508629:task-definition/attacker-family:53"
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
    assert fail_closed.returncode == 64
    assert "apply_openclaw.sh is permanently disabled" in fail_closed.stderr
    assert "terraform_runtime_guard.sh" in fail_closed.stderr


def test_terraform_bootstrap_task_matches_cli_hardening_contract() -> None:
    source = FARGATE.read_text()
    variables = FARGATE_VARIABLES.read_text()
    start = source.index('resource "aws_ecs_task_definition" "openclaw"')
    end = source.index(
        "# ============================================================\n# Services",
        start,
    )
    block = source[start:end]
    for required in (
        'name = "tmp"',
        'name = "state"',
        "readonlyRootFilesystem = true",
        'user                   = "65532:65532"',
        "privileged             = false",
        'drop = ["ALL"]',
        'containerPath = "/tmp"',
        'containerPath = "/tmp/teamagent-openclaw/state"',
        "readOnly      = false",
        "stopTimeout            = 120",
        "/readyz",
    ):
        assert required in block
    assert "dist/index.js" not in block
    assert "OPENCLAW_CONFIG_PATH" not in block
    assert "127.0.0.1:18789/healthz" not in block
    assert not re.search(r"^\s*dockerSecurityOptions\s*=", block, flags=re.MULTILINE)
    assert "linuxParameters.tmpfs" in block
    assert "サポートしない" in block
    assert '{ name = "SLACK_TEAM_ID", value = var.slack_team_id }' in block
    assert '{ name = "SLACK_DM_ALLOWLIST", value = var.slack_dm_allowlist }' in block
    assert (
        '{ name = "TEAMAGENT_CALLER_CLAIM_SECRET", '
        "valueFrom = data.aws_secretsmanager_secret.caller_claim.arn }"
    ) in block
    assert 'variable "openclaw_caller_claim_secret_name"' in variables
    assert "teamagent/dev/openclaw/caller-claim-hmac" in variables
    assert 'can(regex("^T[A-Z0-9]{8,}$", var.slack_team_id))' in variables
    assert 'var.slack_dm_allowlist == "*"' in variables
    assert ('regex("^U[A-Z0-9]{8,}(,U[A-Z0-9]{8,}){0,99}$", var.slack_dm_allowlist)') in variables
    assert 'distinct(split(",", var.slack_dm_allowlist))' in variables
    assert 'default     = ""' in variables
    assert "明示値なしのplanをfail-closed" in variables
    service_start = source.index('resource "aws_ecs_service" "openclaw"')
    service_block = source[service_start:]
    assert "deployment_circuit_breaker" in service_block
    assert "enable   = true" in service_block
    assert "rollback = true" in service_block


def test_mcp_caller_claim_replay_contract_is_cluster_wide_and_least_privilege() -> None:
    source = FARGATE.read_text()
    table_start = source.index('resource "aws_dynamodb_table" "mcp_caller_claim_nonces"')
    table_end = source.index(
        "# ============================================================\n# IAM", table_start
    )
    table = source[table_start:table_end]
    assert 'hash_key     = "nonce"' in table
    assert 'attribute_name = "expires_at"' in table
    assert 'billing_mode = "PAY_PER_REQUEST"' in table
    assert "server_side_encryption" in table
    assert "point_in_time_recovery" in table
    assert "deletion_protection_enabled = true" in table

    policy_start = source.index('data "aws_iam_policy_document" "mcp_task"')
    policy_end = source.index('resource "aws_iam_role" "mcp_task"', policy_start)
    policy = source[policy_start:policy_end]
    assert 'sid       = "ConsumeCallerClaimNonce"' in policy
    assert 'actions   = ["dynamodb:PutItem"]' in policy
    assert "aws_dynamodb_table.mcp_caller_claim_nonces.arn" in policy
    nonce_statement = policy[policy.index('sid       = "ConsumeCallerClaimNonce"') :]
    next_statement = nonce_statement.find("\n  statement {", 1)
    if next_statement >= 0:
        nonce_statement = nonce_statement[:next_statement]
    for forbidden in ("dynamodb:GetItem", "dynamodb:DeleteItem", "dynamodb:Scan"):
        assert forbidden not in nonce_statement

    mcp_start = source.index('resource "aws_ecs_task_definition" "mcp"')
    openclaw_start = source.index('resource "aws_ecs_task_definition" "openclaw"')
    mcp_block = source[mcp_start:openclaw_start]
    assert (
        '{ name = "TEAMAGENT_CALLER_CLAIM_REPLAY_TABLE", '
        "value = aws_dynamodb_table.mcp_caller_claim_nonces.name }"
    ) in mcp_block
    openclaw_end = source.index(
        "# ============================================================\n# Services",
        openclaw_start,
    )
    assert "TEAMAGENT_CALLER_CLAIM_REPLAY_TABLE" not in source[openclaw_start:openclaw_end]


def test_effective_tool_scope_matches_config_and_deployment_gates() -> None:
    config = _load_reviewed_json5(CONFIG)
    scope = json.loads(TOOL_SCOPE.read_text())
    included = config["mcp"]["servers"]["teamagent"]["toolFilter"]["include"]
    excluded = config["mcp"]["servers"]["teamagent"]["toolFilter"]["exclude"]
    inventory_names = [tool["name"] for tool in scope["tools"]]
    assert scope["schemaVersion"] == 2
    assert len(inventory_names) == len(set(inventory_names)) == 29
    assert set(inventory_names) == set(included)
    assert {
        "chitchat",
        "recommend",
        "proposal_campaign",
        "mail_constraints",
        "workspace_search",
        "proposal_deck",
    } <= set(excluded)
    assert not set(included) & set(excluded)
    assert scope["nativeTools"]["profile"] == config["tools"]["profile"]
    assert scope["nativeTools"]["alsoAllow"] == config["tools"]["alsoAllow"]
    assert set(scope["nativeTools"]["deny"]) == set(config["tools"]["deny"])
    assert scope["nativeTools"]["nativeMessageActionsDenied"] == [
        "send",
        "read",
        "edit",
        "delete",
        "upload",
    ]
    assert "runId/toolCallId" in scope["nativeTools"]["authorizedPath"]
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
    activation_by_name = {tool["name"]: tool["enabledBy"] for tool in scope["tools"]}
    assert activation_by_name["search"] == {"kind": "always"}
    assert activation_by_name["video_analysis"] == {
        "kind": "envAllTrue",
        "names": ["USE_VIDEO_TOOLS"],
    }
    assert activation_by_name["tiktok_search"] == {
        "kind": "envAllTrue",
        "names": ["USE_TIKTOK_TOOLS"],
    }
    assert activation_by_name["proposal_builder"] == {
        "kind": "envAllTrue",
        "names": [
            "USE_PROPOSAL_BUILDER_TOOLS",
            "USE_PROPOSAL_BUILDER_SYNC_RUNTIME_VERIFIED",
        ],
    }
    assert activation_by_name["x_voice_search"] == {
        "kind": "envAllTrue",
        "names": ["USE_X_RESEARCH_TOOLS"],
    }
    assert activation_by_name["video_approval"] == {"kind": "never"}
    effects = {tool["effect"] for tool in scope["tools"]}
    assert "gmail-draft-write-no-send" in effects
    assert "calendar-write-no-invite" in effects
    assert "external-job-submit-s3-write" in effects
    tools_by_name = {tool["name"]: tool for tool in scope["tools"]}
    assert tools_by_name["x_voice_search"]["effect"] == "external-read-scrape-analysis-report-write"
    assert tools_by_name["x_needs_mining"]["effect"] == "external-read-analysis-report-write"
    assert tools_by_name["search_surface_check"]["effect"] == "external-read-analysis-report-write"
    assert (
        tools_by_name["x_buzz_measure_status"]["effect"]
        == "job-status-read-lazy-analysis-report-write-cache-write"
    )
    media_worker_gate = "enable_media_worker || enable_tiktok_acquire (deprecated alias)"
    assert tools_by_name["tiktok_acquire"]["terraformGate"] == media_worker_gate
    assert tools_by_name["tiktok_acquire_status"]["terraformGate"] == media_worker_gate
    fargate = FARGATE.read_text()
    terraform = "\n".join(
        path.read_text() for path in sorted((ROOT / "infra/terraform").glob("*.tf"))
    )
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
        "enable_media_worker",
        "enable_proposal_builder",
        "proposal_builder_sync_runtime_verified",
        "enable_x_research",
    ):
        assert gate in terraform
    assert (
        "media_worker_enabled = var.enable_media_worker || var.enable_tiktok_acquire" in terraform
    )
    assert "media_enabled        = local.media_worker_enabled ? 1 : 0" in terraform
    assert "local.media_enabled == 1 ? [" in fargate
    assert '{ name = "USE_TIKTOK_ACQUIRE", value = "1" }' in fargate
    mcp_start = fargate.index('resource "aws_ecs_task_definition" "mcp"')
    openclaw_start = fargate.index('resource "aws_ecs_task_definition" "openclaw"')
    mcp_block = fargate[mcp_start:openclaw_start]
    media_gate_start = mcp_block.index("local.media_enabled == 1 ? [")
    media_gate_end = mcp_block.index(
        "] : [], var.enable_x_research ? [",
        media_gate_start,
    )
    media_gate_block = mcp_block[media_gate_start:media_gate_end]
    tiktok_env = '{ name = "USE_TIKTOK_ACQUIRE", value = "1" }'
    assert media_gate_block.count(tiktok_env) == 1
    assert mcp_block.count(tiktok_env) == 1
    assert '{ name = "DRAFT_ON_DEMAND_ONLY", value = "true" }' in mcp_block
    for unwired_gate in (
        "USE_VIDEO_APPROVAL",
        "USE_OPERATION_LOG_TOOLS",
        "USE_KNOWLEDGE_SEARCH_URL_TOOL",
    ):
        assert unwired_gate not in fargate


def _derive_rollout_expected_tools(
    tmp_path: Path,
    environment: dict[str, str],
    actual_names: list[str],
) -> subprocess.CompletedProcess[str]:
    script = """
import { pathToFileURL } from "node:url";
const [gatePath, canaryPath, environmentJson, actualJson] = process.argv.slice(2);
const { readExpectedToolNames } = await import(pathToFileURL(gatePath).href);
const { assertExactToolNames } = await import(pathToFileURL(canaryPath).href);
const expected = readExpectedToolNames({
  taskDefinition: {
    taskDefinitionArn: "arn:aws:ecs:ap-northeast-1:718959508629:task-definition/teamagent-dev-mcp:42",
    family: "teamagent-dev-mcp",
    status: "ACTIVE",
    containerDefinitions: [{
      name: "teamagent-mcp",
      environment: Object.entries(JSON.parse(environmentJson)).map(([name, value]) => ({name, value}))
    }]
  }
});
assertExactToolNames(expected, JSON.parse(actualJson).sort());
process.stdout.write(JSON.stringify(expected));
"""
    runner = tmp_path / "rollout-tool-derivation.mjs"
    runner.write_text(script)
    return subprocess.run(
        [
            "node",
            str(runner),
            str(ROLLOUT_GATE),
            str(ROLLOUT_TASK_CANARY),
            json.dumps(environment),
            json.dumps(actual_names),
        ],
        check=False,
        text=True,
        capture_output=True,
    )


def test_rollout_canary_derives_exact_tools_from_mcp_gate_environment(
    tmp_path: Path,
) -> None:
    default_environment = {
        "USE_MAIL_SUMMARY_TOOL": "true",
        "USE_FOLLOWUP_TOOL": "true",
        "USE_MAIL_LINK_TOOL": "true",
        "USE_MAIL_REPLY_TOOL": "true",
        "USE_MAIL_DRAFT_TOOL": "true",
        "USE_MORNING_DIGEST_TOOL": "true",
        "USE_OAUTH_CONNECT_TOOL": "true",
        "USE_KNOWLEDGE_DELIVER": "true",
        "USE_VIDEO_TOOLS": "false",
        "USE_TIKTOK_TOOLS": "false",
        "USE_TIKTOK_ACQUIRE": "0",
        "USE_X_RESEARCH_TOOLS": "0",
        "USE_SEARCH_SURFACE_TOOL": "0",
        "USE_TIKTOK_COMMENT_TOOLS": "0",
    }
    default_expected = [
        "clientkarte",
        "knowledge_deliver",
        "mail_draft",
        "mail_followup",
        "mail_reply",
        "mail_summary",
        "mail_to_internal_context",
        "morning_digest",
        "oauth_connect",
        "proposal_draft",
        "proposal_review",
        "search",
    ]
    baseline = _derive_rollout_expected_tools(
        tmp_path,
        default_environment,
        default_expected,
    )
    assert baseline.returncode == 0, baseline.stderr
    assert json.loads(baseline.stdout) == default_expected

    x_research_environment = {
        **default_environment,
        "USE_X_RESEARCH_TOOLS": "1",
        "USE_SEARCH_SURFACE_TOOL": "1",
        "USE_TIKTOK_COMMENT_TOOLS": "1",
    }
    x_research_expected = sorted(
        [
            *default_expected,
            "x_voice_search",
            "x_needs_mining",
            "x_buzz_measure",
            "x_buzz_measure_status",
            "search_surface_check",
            "tiktok_comment_mining",
        ]
    )
    expanded = _derive_rollout_expected_tools(
        tmp_path,
        x_research_environment,
        x_research_expected,
    )
    assert expanded.returncode == 0, expanded.stderr
    assert json.loads(expanded.stdout) == x_research_expected

    unknown_actual = _derive_rollout_expected_tools(
        tmp_path,
        x_research_environment,
        [*x_research_expected, "unreviewed_tool"],
    )
    assert unknown_actual.returncode != 0
    assert "MCP tools/list differs from reviewed scope" in unknown_actual.stderr


def test_openclaw_manifest_documentation_matches_schema_five() -> None:
    build_image = (ROOT / "infra/openclaw/build-image.sh").read_text()
    documentation = "\n".join(
        [
            (ROOT / "infra/openclaw/README.md").read_text(),
            (ROOT / "docs/openclaw/golive_checklist.md").read_text(),
        ]
    )

    assert "schemaVersion:5" in build_image
    assert "schema-4" not in documentation
    assert documentation.count("schema-5") >= 2


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
        "callerIdentityPluginHookContract",
        "CALLER_IDENTITY_PLUGIN_PROBE",
        "callerClaimSecretEmptyFailsClosed",
        "slackTeamMalformedFailsClosed",
        "conversations.history",
        "ListFoundationModelsCommand",
        "docker",
        "export",
        "canonicalFsFreshExportsReproduce",
        "rawExportTarDigestClaimed",
        "actual-image-contract.json",
    ):
        assert required in source


def test_task_hardening_filter_and_release_boundary_do_not_claim_fargate_nnp() -> None:
    task_filter = TASK_FILTER.read_text()
    deploy = DEPLOY_HELPER.read_text()
    bundle_helper = BUNDLE_HELPER.read_text()
    contract = json.loads(TRUST_CONTRACT.read_text())
    assert "readonlyRootFilesystem" in task_filter
    assert 'drop: ["ALL"]' in task_filter
    assert "initProcessEnabled: true" in task_filter
    assert 'containerPath: "/tmp"' in task_filter
    assert 'containerPath: "/tmp/teamagent-openclaw/state"' in task_filter
    assert "dockerSecurityOptions" in task_filter
    assert "del(.entryPoint, .command, .dockerSecurityOptions)" in task_filter
    assert "expected exactly one container named openclaw; sidecars are forbidden" in task_filter
    assert "exact tmp and encrypted EFS state volumes are required" in task_filter
    assert "Slack team/DM environment contract is invalid" in task_filter
    assert "valid_slack_dm_allowlist" in task_filter
    assert r"^U[A-Z0-9]{8,}(,U[A-Z0-9]{8,}){0,99}$" in task_filter
    assert "apply_openclaw.sh is permanently disabled" in deploy
    assert "terraform_runtime_guard.sh" in deploy
    assert "register-task-definition" not in deploy
    assert "update-service" not in deploy
    assert "terraform apply" not in deploy
    assert ".stopTimeout = 120" in task_filter
    assert '"/nodejs/bin/node"' in task_filter
    assert contract["release"]["ready"] is True
    assert contract["release"]["blocked_reason"] == ""
    assert contract["bundle"]["interfaces"]["build"] == "infra/openclaw/build-bundle.sh"
    # The media subject was removed from the required set: it had no Dockerfile,
    # no image in any of its ECR repositories, and no reference in the tree.
    # Pinning the list (rather than just its length) keeps a silent re-expansion
    # from slipping in without a deliberate edit here.
    assert [subject["name"] for subject in contract["bundle"]["subjects"]] == ["core"]
    assert contract["bundle"]["contract_oci_label"] == ("io.teamagent.build.contract-sha256")
    # This used to pin the emitter's unconditional die.  The emitter is now
    # implemented, so the invariant it really protected is pinned directly: the
    # release gate must run before any external mutation, and nothing may reach
    # a registry or a builder ahead of it.
    assert bundle_helper.index("assert-release-ready") < bundle_helper.index("docker ")
    assert bundle_helper.index("assert-release-ready") < bundle_helper.index("aws ")
    assert "no-new-privileges" not in task_filter


def test_openclaw_startup_alarm_matches_both_exact_log_fixtures() -> None:
    terraform = CLOUDWATCH_FARGATE.read_text()
    events = [
        json.loads(line) for line in STARTUP_LOG_FIXTURE.read_text().splitlines() if line.strip()
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
    assert 'metric_name         = "OpenClawRolloutGateFailure"' not in terraform
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

    active_fixture = copy.deepcopy(fixture)
    active_slack = _active_slack_canary_evidence(active_fixture)
    active_fixture["slack"] = copy.deepcopy(active_slack)
    active_fixture["persistedResult"]["slack"] = copy.deepcopy(active_slack)
    _bind_rollout_fixture_result_hash(active_fixture)
    active_path = tmp_path / "rollout-active-slack.json"
    active_path.write_text(json.dumps(active_fixture))
    active_passed = subprocess.run(
        ["node", str(ROLLOUT_GATE), "--validate-fixture", str(active_path)],
        check=False,
        text=True,
        capture_output=True,
    )
    assert active_passed.returncode == 0, active_passed.stderr
    assert json.loads(active_passed.stdout)["passed"] is True

    mutations: list[tuple[str, dict[str, Any]]] = []
    changed = copy.deepcopy(fixture)
    changed["consumption"]["state"] = "APPLYING"
    mutations.append(("unconsumed intent", changed))
    changed = copy.deepcopy(fixture)
    changed["expected"]["previousTaskDefinition"] = fixture["expected"]["newTaskDefinition"]
    mutations.append(("same old/new revision", changed))
    changed = copy.deepcopy(fixture)
    changed["caller"]["Arn"] = (
        "arn:aws:sts::718959508629:assumed-role/"
        "teamagent-dev-terraform-runtime-automation/other-session"
    )
    mutations.append(("wrong automation role session", changed))
    changed = copy.deepcopy(fixture)
    changed["caller"]["UserId"] = "AROAFIXTURE123456789:other-session"
    mutations.append(("wrong automation role user id", changed))
    changed = copy.deepcopy(fixture)
    changed["service"]["services"][0]["deploymentConfiguration"]["deploymentCircuitBreaker"][
        "rollback"
    ] = False
    mutations.append(("circuit breaker", changed))
    changed = copy.deepcopy(fixture)
    changed["service"]["services"][0]["desiredCount"] = 2
    changed["service"]["services"][0]["runningCount"] = 2
    mutations.append(("unexpected multi-writer service", changed))
    changed = copy.deepcopy(fixture)
    changed["task"]["tasks"][0]["taskDefinitionArn"] = fixture["expected"]["previousTaskDefinition"]
    mutations.append(("wrong task", changed))
    changed = copy.deepcopy(fixture)
    changed["taskEvent"]["mcp"]["toolNamesSha256"] = "0" * 64
    mutations.append(("tool scope", changed))
    changed = copy.deepcopy(fixture)
    changed["taskEvent"]["bedrock"]["credentialSource"] = "AWS_ACCESS_KEY_ID"
    mutations.append(("static credentials", changed))
    changed = copy.deepcopy(fixture)
    changed["runningBefore"]["taskArns"].pop()
    mutations.append(("incomplete pre-Slack task enumeration", changed))
    changed = copy.deepcopy(fixture)
    changed["runningBefore"]["tasks"][0]["taskDefinitionArn"] = fixture["expected"][
        "previousTaskDefinition"
    ]
    mutations.append(("mixed pre-Slack running revision", changed))
    changed = copy.deepcopy(fixture)
    changed["runningAfter"]["tasks"][0]["taskDefinitionArn"] = fixture["expected"][
        "previousTaskDefinition"
    ]
    mutations.append(("mixed post-Slack running revision", changed))
    changed = copy.deepcopy(fixture)
    changed["slack"]["connected"] = True
    mutations.append(("skipped Slack canary claims a connection", changed))
    changed = copy.deepcopy(fixture)
    changed["slack"]["mentionReplyExact"] = True
    mutations.append(("skipped Slack canary claims an exact reply", changed))
    changed = copy.deepcopy(fixture)
    changed["slack"]["skipReasonCodes"].reverse()
    mutations.append(("skipped Slack canary reason substitution", changed))
    changed = copy.deepcopy(fixture)
    changed["slack"]["candidateLogCorrelation"] = copy.deepcopy(
        active_slack["candidateLogCorrelation"]
    )
    mutations.append(("skipped Slack canary claims a log correlation", changed))
    changed = copy.deepcopy(fixture)
    del changed["slack"]["candidateLogCorrelation"]
    mutations.append(("skipped Slack canary omits the explicit null correlation", changed))
    changed = copy.deepcopy(fixture)
    changed["slack"]["postedTs"] = active_slack["postedTs"]
    mutations.append(("skipped Slack canary carries active-only evidence", changed))
    changed = copy.deepcopy(fixture)
    changed["persistedResult"]["slack"]["mentionReplyExact"] = True
    _bind_rollout_fixture_result_hash(changed)
    mutations.append(("signed skipped Slack evidence claims a reply", changed))
    changed = copy.deepcopy(active_fixture)
    changed["slack"]["candidateLogCorrelation"]["taskArn"] = (
        "arn:aws:ecs:ap-northeast-1:718959508629:"
        "task/teamagent-dev/ffffffffffffffffffffffffffffffff"
    )
    mutations.append(("active Slack response from unlisted task", changed))
    changed = copy.deepcopy(active_fixture)
    changed["slack"]["responseTokenAbsentFromPrompt"] = False
    mutations.append(("active Slack prompt can satisfy log correlation", changed))
    changed = copy.deepcopy(active_fixture)
    changed["slack"]["candidateLogCorrelation"]["tokenSha256"] = "0" * 64
    mutations.append(("active Slack log token does not bind the reply", changed))
    changed = copy.deepcopy(active_fixture)
    changed["slack"]["candidateLogCorrelation"]["eventTimestamp"] = 1784420061201
    mutations.append(("active Slack log event falls outside the reply window", changed))
    changed = copy.deepcopy(active_fixture)
    changed["slack"]["skipped"] = "invalid"
    mutations.append(("active Slack canary uses an invalid skipped claim", changed))
    changed = copy.deepcopy(active_fixture)
    changed["slack"]["skipReasonCodes"] = SLACK_CANARY_SKIP_REASON_CODES
    mutations.append(("active Slack canary mixes skipped reason claims", changed))
    changed = copy.deepcopy(fixture)
    changed["rollbackAuthorization"]["one_use"] = False
    mutations.append(("reusable rollback authorization", changed))
    changed = copy.deepcopy(fixture)
    changed["rollbackAuthorization"]["previous_task_definition_arn"] = fixture["expected"][
        "newTaskDefinition"
    ]
    mutations.append(("rollback authorization revision swap", changed))
    changed = copy.deepcopy(fixture)
    changed["rollbackAuthorization"]["state"] = "CONSUMED"
    changed["rollbackAuthorization"]["consumed_at_epoch"] = 1784420001
    mutations.append(("replayed rollback authorization", changed))
    changed = copy.deepcopy(fixture)
    changed["persistedResult"]["automationRoleArn"] = (
        "arn:aws:sts::718959508629:assumed-role/"
        "teamagent-dev-terraform-runtime-automation/other-session"
    )
    mutations.append(("persisted result role substitution", changed))
    changed = copy.deepcopy(fixture)
    changed["persistedResult"]["runningTasksAfterSlack"]["tasks"][0]["taskDefinitionArn"] = fixture[
        "expected"
    ]["previousTaskDefinition"]
    mutations.append(("signed post-Slack task revision substitution", changed))
    changed = copy.deepcopy(active_fixture)
    changed["persistedResult"]["slack"]["candidateLogCorrelation"]["logStreamName"] = (
        "openclaw/openclaw/ffffffffffffffffffffffffffffffff"
    )
    _bind_rollout_fixture_result_hash(changed)
    mutations.append(("signed active Slack log substitution", changed))
    changed = copy.deepcopy(fixture)
    changed["immutableEvidence"]["resultVersionId"] = ""
    mutations.append(("missing exact result VersionId", changed))
    changed = copy.deepcopy(fixture)
    changed["immutableEvidence"]["resultVersionId"] = "null"
    mutations.append(("unversioned S3 result", changed))
    changed = copy.deepcopy(fixture)
    changed["immutableEvidence"]["resultObjectLockMode"] = "NONE"
    mutations.append(("invalid Object Lock mode", changed))
    changed = copy.deepcopy(fixture)
    changed["immutableEvidence"]["signatureObjectLockRetainUntil"] = "2026-07-19T00:00:01Z"
    mutations.append(("short signature retention", changed))
    changed = copy.deepcopy(fixture)
    changed["immutableEvidence"]["encryptionKmsAlias"] = "alias/untrusted"
    mutations.append(("wrong encryption KMS key", changed))
    changed = copy.deepcopy(fixture)
    changed["expected"]["signingKmsKeyArn"] = fixture["expected"]["encryptionKmsKeyArn"]
    mutations.append(("Terraform-state KMS key substitution", changed))
    changed = copy.deepcopy(fixture)
    changed["immutableEvidence"]["resultSha256"] = "0" * 64
    mutations.append(("result hash substitution", changed))
    changed = copy.deepcopy(fixture)
    changed["immutableEvidence"]["signatureValid"] = False
    mutations.append(("invalid KMS signature", changed))

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
        "expected-tool-names-json",
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
        "chat.postMessage",
        "conversations.replies",
        "mentionReplyExact",
        "responseTokenAbsentFromPrompt",
        "list-tasks",
        "describe-tasks",
        "filter-log-events",
        "candidateLogCorrelation",
        "openclaw-rollback#",
        "transact-write-items",
        "update-service",
        "services-stable",
        "--version-id",
        "--evidence-encryption-kms-key-arn",
        "--evidence-signing-kms-key-arn",
        "ObjectLockMode",
        "get-bucket-versioning",
        "get-object-lock-configuration",
        "get-bucket-encryption",
        "--if-none-match",
        "--expected-bucket-owner",
        'kms", "sign',
        'kms", "verify',
        "teamagent-dev-openclaw-rollout-evidence",
        "alias/teamagent-dev-openclaw-rollout-evidence",
        "alias/teamagent-dev-openclaw-rollout-signing",
        "deriveExpectedToolNames",
        "--mcp-task-definition",
    ):
        assert required in rollout
    for skipped_canary_contract in (
        *SLACK_CANARY_SKIP_REASON_CODES,
        "candidateLogCorrelation: null",
        "app_id A0B970DFU4S equals AiLa's api_app_id",
        "zero in both channels and DMs",
        "indistinguishable from prompt",
        "Operators perform the Slack round trip manually instead",
    ):
        assert skipped_canary_contract in rollout
    live_rollout = rollout[
        rollout.index("async function runLive") : rollout.index("async function runRestore")
    ]
    for removed_provider_call in (
        "verifySlackMentionReply(",
        "fetchSlackLogCorrelation(",
        '"secretsmanager", "get-secret-value"',
    ):
        assert removed_provider_call not in live_rollout
    assert rollout.count("verifySlackMentionReply(") == 1
    assert rollout.count("fetchSlackLogCorrelation(") == 1
    assert "names.length !== 12" not in rollout
    assert "process.env.ECS_SERVICE" not in rollout
    assert "process.env.ECS_CLUSTER" not in rollout
    assert "process.env.CANARY_SECRET" not in rollout

    guard = RUNTIME_GUARD.read_text()
    for slack_guard_claim in (
        ".slack.skipped == true",
        ".slack.skipped == false",
        ".slack.connected == false",
        ".slack.connected == true",
        ".slack.mentionReplyExact == false",
        ".slack.mentionReplyExact == true",
        ".slack.skipReasonCodes == [",
        'has("skipped") | not',
        'has("skipReasonCodes") | not',
        'has("candidateLogCorrelation")',
        ".slack.candidateLogCorrelation.matched ==",
        'has("postedTs") | not',
        'has("replyTs") | not',
        'has("tokenSha256") | not',
        'has("correlationSha256") | not',
        'has("responseTokenAbsentFromPrompt") | not',
        *SLACK_CANARY_SKIP_REASON_CODES,
    ):
        assert guard.count(slack_guard_claim) == 2
    assert '--slurpfile state_contract "$TMP_ROOT/plan-state-contract.json"' in guard
    assert ". == ($receipt[0].state_contract | del(.task_revisions))" in guard
    assert '"$stage/plan-state-contract.json"' in guard
    apply_case = guard[guard.index("  apply)") :]
    revision_gate = apply_case.index('if [ "$OPENCLAW_ROLLOUT_REQUIRED" = "false" ]; then')
    rollout_else = apply_case.index("\n    else\n", revision_gate)
    rollout_call = apply_case.index(
        'if ! node "$OPENCLAW_ROLLOUT_GATE"',
        rollout_else,
    )
    dm_qa_gate = apply_case.index(
        'if [ -n "$FORCED_ROLLBACK_DM_QA_DEADLINE_EPOCH" ]; then',
        rollout_call,
    )
    rollout_branch_end = apply_case.rindex(
        "\n    fi\n",
        rollout_call,
        dm_qa_gate,
    )
    dm_qa_call = apply_case.index("if run_forced_rollback_dm_qa", dm_qa_gate)
    dm_qa_binding = apply_case.index(". + {dmQa:$dm_qa[0]}", dm_qa_call)
    service_probe = apply_case.index("run_post_apply_service_probe")
    eventbridge_verified = apply_case.index(
        'python3 "$EVENTBRIDGE_APPLY_SAGA" verify',
        dm_qa_binding,
    )
    ecs_verified = apply_case.index(
        'python3 "$ECS_SERVICE_APPLY_SAGA" verify',
        eventbridge_verified,
    )
    heartbeat_stop_before_finalize = apply_case.index(
        "stop_gate_heartbeat",
        ecs_verified,
    )
    composite_finalize = apply_case.index(
        'python3 "$DEPLOYMENT_APPLY_FINALIZER" commit',
        heartbeat_stop_before_finalize,
    )
    assert (
        apply_case.index('"$APPLY_SUPERVISOR"')
        < service_probe
        < revision_gate
        < rollout_call
        < rollout_branch_end
        < dm_qa_gate
        < dm_qa_call
        < dm_qa_binding
        < eventbridge_verified
        < ecs_verified
        < heartbeat_stop_before_finalize
        < composite_finalize
    )
    unchanged_rollout_branch = apply_case[revision_gate:rollout_else]
    assert "task-definition-unchanged" in unchanged_rollout_branch
    assert "run_forced_rollback_dm_qa" not in unchanged_rollout_branch
    assert (
        "run_forced_rollback_dm_qa"
        not in apply_case[revision_gate : rollout_branch_end + len("\n    fi\n")]
    )
    assert "OPENCLAW_ROLLOUT_REQUIRED" not in apply_case[dm_qa_gate:dm_qa_call]
    assert apply_case.count("if run_forced_rollback_dm_qa") == 1
    assert apply_case.index("--restore-and-verify") < revision_gate < rollout_call
    assert apply_case.index('OPENCLAW_POST_APPLY_STARTED="true"') < rollout_call
    assert 'OPENCLAW_ROLLOUT_REQUIRED="$(' in apply_case
    assert '"aws_ecs_task_definition.openclaw[0]"' in apply_case
    assert "planned OpenClaw candidateがdistinct live revision" in apply_case
    assert "openclaw_rollout_evidence_key_arn" in apply_case
    assert "openclaw_rollout_signing_key_arn" in apply_case
    assert "MCP_NEW_TASK_DEFINITION" in apply_case
    assert '--mcp-task-definition "$MCP_NEW_TASK_DEFINITION"' in apply_case
    heartbeat_restart = apply_case.rfind("start_gate_heartbeat", 0, rollout_call)
    assert apply_case.index('"$APPLY_SUPERVISOR"') < heartbeat_restart < rollout_call
    ecs_begin = apply_case.index('python3 "$ECS_SERVICE_APPLY_SAGA" begin')
    assert ecs_begin < apply_case.index('"$APPLY_SUPERVISOR"')
    assert apply_case.index('rm -f "$STAGED_PLAN"') < ecs_begin
    assert "stop_gate_heartbeat" not in apply_case[heartbeat_restart:rollout_call]
    assert "release-deployment-lock" not in apply_case[heartbeat_restart:rollout_call]
    assert '.dmQa.result == "PASSED"' in apply_case
    assert ".dmQa.applyAttemptId == $attempt" in apply_case
    assert '.dmQa.locator.object_lock_mode == "COMPLIANCE"' in apply_case
    assert ".schema_version == 7" in apply_case
    assert "openclaw_rollout_result_sha256" in apply_case
    assert "post_apply_service_probe_sha256" in apply_case
    assert "ecs_service_saga_receipt_sha256" in apply_case
    assert "deployment_finalization_receipt_sha256" in apply_case
    assert "eventbridge_apply_saga_verification_receipt" in apply_case
    assert "build_scoped_release_live_contract" in apply_case
    assert "build_scoped_release_state_contract" in apply_case
    assert '--revision-id "$revision_id"' in apply_case
    assert "pre_live_contract:$pre_live_contract[0]" in apply_case
    assert "pre_state_contract:$pre_state_contract[0]" in apply_case
    # post_live_contract is rebuilt from the applied live state (images come from
    # $live[0]); only .resources is taken from the scoped contract wholesale.
    assert "post_live_contract:{" in apply_case
    assert "resources:$post_live_contract[0].resources" in apply_case
    assert "post_state_contract:$post_state_contract[0]" in apply_case
    assert "task_revisions(.pre_live_contract.resources)" in apply_case
    assert "task_revisions(.post_live_contract.resources)" in apply_case

    dm_qa_start = guard.index("run_forced_rollback_dm_qa() {")
    dm_qa = guard[dm_qa_start : guard.index("\nwrite_preflight_receipt()", dm_qa_start)]
    dm_qa_probe = FORCED_ROLLBACK_DM_QA_PROBE.read_text(encoding="utf-8")
    assert "FORCED_ROLLBACK_DM_QA_MAX_SECONDS=300" in guard
    assert "FORCED_ROLLBACK_DM_QA_RECOVERY_RESERVE_SECONDS=30" in guard
    assert ('FORCED_ROLLBACK_DM_QA_PROBE="$GUARD_JQ_DIR/forced_rollback_dm_qa_probe.py"') in guard
    assert 'assert_regular_nonwritable "$FORCED_ROLLBACK_DM_QA_PROBE"' in guard
    assert 'assert_git_tracked_clean "$FORCED_ROLLBACK_DM_QA_PROBE"' in guard
    assert guard.count('"$FORCED_ROLLBACK_DM_QA_PROBE"') == 4
    assert 'python3 "$FORCED_ROLLBACK_DM_QA_PROBE"' in dm_qa
    assert "python3 - \\" not in dm_qa
    assert '> "$probe_output"' in dm_qa
    assert "--forced-rollback-dm-qa-deadline-epoch)" in apply_case
    assert (
        "available=$((deadline_epoch - now - FORCED_ROLLBACK_DM_QA_RECOVERY_RESERVE_SECONDS))"
    ) in dm_qa
    assert ('if [ "$timeout_seconds" -gt "$FORCED_ROLLBACK_DM_QA_MAX_SECONDS" ]; then') in dm_qa
    assert 'timeout_seconds="$FORCED_ROLLBACK_DM_QA_MAX_SECONDS"' in dm_qa
    assert "deadline_monotonic = started_monotonic + int(timeout_seconds_raw)" in dm_qa_probe
    assert "timeout=remaining()" in dm_qa_probe
    assert "timeout=min(15.0, remaining())" in dm_qa_probe
    assert "time.sleep(min(3.0, remaining()))" in dm_qa_probe
    assert "time.sleep(min(2.0, remaining()))" in dm_qa_probe
    assert "return 124" in dm_qa
    assert "raise SystemExit(124)" in dm_qa_probe
    assert "raise SystemExit(24)" in dm_qa_probe
    assert "24|124)" in dm_qa
    assert 'return "$status"' in dm_qa
    assert "return 24" in dm_qa
    assert "os.link(sys.argv[1], sys.argv[2], follow_symlinks=False)" in dm_qa
    assert 'mv "$probe_output" "$output"' not in dm_qa
    assert 'if [ "$DM_QA_STATUS" -eq 124 ]; then' in apply_case
    assert "exit 124" in apply_case[dm_qa_call:eventbridge_verified]
    # The non-timeout DM QA failure must also stay fatal: a mutation that swallows
    # this branch survived the whole suite once, so pin the fail-closed exit here.
    assert "apply saga must not be finalized" in apply_case[dm_qa_call:eventbridge_verified]
    assert "exit 24" in apply_case[dm_qa_call:eventbridge_verified]
    assert '"--object-lock-mode",' in dm_qa_probe
    assert 'metadata.get("ObjectLockMode") != "COMPLIANCE"' in dm_qa_probe
    assert '"object_lock_mode": "COMPLIANCE"' in dm_qa_probe
    for required_dm_qa_result_binding in (
        "jq -s -e",
        "length == 1",
        '.locator.object_lock_mode == "COMPLIANCE"',
        ".locator.encryption_kms_key_arn == $encryption_kms",
        ".locator.signer.kms_key_arn == $signing_kms",
        ".locator.exact_version_redownload.requested_version_id ==",
        ".locator.exact_version_redownload.returned_version_id ==",
        ".locator.exact_version_redownload.bytes_match == true",
    ):
        assert required_dm_qa_result_binding in dm_qa
    assert "forced_rollback_drill_evidence_bucket" in apply_case
    assert "forced_rollback_drill_evidence_prefix" in apply_case
    assert '"forced-rollback-drills/"' in apply_case
    assert '"$OPENCLAW_SIGNING_KMS_KEY_ARN"; then' in apply_case[dm_qa_call:eventbridge_verified]
    assert 'f"{EVIDENCE_PREFIX}/{apply_attempt_id}/dm-qa/result.json"' in dm_qa_probe
    assert '"signing_kms_key_arn": signing_kms_key_arn' in dm_qa_probe
    assert '"signing_algorithm": SIGNING_ALGORITHM' in dm_qa_probe
    assert '"verify",' in dm_qa_probe
    assert 'page_arguments.extend(["--next-token", next_token])' in dm_qa_probe
    assert 'returned_token = response.get("nextToken")' in dm_qa_probe
    assert "seen_tokens.add(returned_token)" in dm_qa_probe
    assert "service_name=MCP_SERVICE" in dm_qa_probe
    assert '"mcp_running_tasks_before": mcp_running_before' in dm_qa_probe
    assert '"mcp_running_tasks_after": mcp_running_after' in dm_qa_probe
    for required_dm_qa_probe_contract in (
        'caller = aws_json("sts", "get-caller-identity", [])',
        'caller.get("Arn") != TRUSTED_AUTOMATION_ARN',
        'set(secret) != {"userToken", "channelId", "botUserId"}',
        're.fullmatch(r"xoxp-[A-Za-z0-9-]{20,}"',
        "nonce = os.urandom(12).hex()",
        "fragment_a =",
        "fragment_b =",
        "if response_token in prompt:",
        '"chat.postMessage"',
        '"conversations.replies"',
        'message.get("user") == secret["botUserId"]',
        'str(message.get("text", "")).strip() == response_token',
        '"--filter-pattern"',
        "if len(matched_streams) == 1:",
        '"token_sha256": sha256_bytes(response_token.encode())',
        "if running_before != running_after:",
        "if mcp_running_before != mcp_running_after:",
        "sys.stdout.buffer.write(canonical_bytes(value))",
    ):
        assert required_dm_qa_probe_contract in dm_qa_probe
    assert dm_qa_probe.count("sys.stdout.buffer.write") == 1
    assert dm_qa_probe.count("print(") == 2
    assert dm_qa_probe.count("file=sys.stderr") == 2
    assert "print(f" not in dm_qa_probe
    reserve_checks = apply_case.count("ensure_forced_rollback_dm_qa_recovery_reserve")
    assert reserve_checks == 3
    assert (
        apply_case.rindex(
            "ensure_forced_rollback_dm_qa_recovery_reserve",
            0,
            composite_finalize,
        )
        < composite_finalize
    )
    assert "$forced_dm_qa_required" in apply_case[dm_qa_binding:composite_finalize]

    cleanup = apply_case[
        apply_case.index("cleanup_apply_command()") : apply_case.index(
            "trap 'cleanup_apply_command' EXIT"
        )
    ]
    assert '[ "$OPENCLAW_ROLLOUT_REQUIRED" = "true" ]' in cleanup
    recovery_probe = cleanup.index("recover_committed_finalization")
    lambda_restore = cleanup.index("restore_lambda_dispatcher_baselines")
    eventbridge_restore = cleanup.index('python3 "$EVENTBRIDGE_APPLY_SAGA" finish')
    ecs_restore = cleanup.index('python3 "$ECS_SERVICE_APPLY_SAGA" finish')
    lock_cleanup = cleanup.index('bash "$IMAGE_GATE_RUNNER" release-deployment-lock')
    assert recovery_probe < ecs_restore < lock_cleanup
    assert recovery_probe < lambda_restore < eventbridge_restore < ecs_restore < lock_cleanup

    probe_start = guard.index("run_post_apply_service_probe()")
    probe_end = guard.index("\nrun_forced_rollback_dm_qa()", probe_start)
    probe = guard[probe_start:probe_end]
    for expected in (
        "http://teamagent-mcp.teamagent.internal:8787/healthz",
        "http://connect-web.teamagent.internal:8788/healthz",
        "EXPECTED_APP_VERSION_ID",
        "EXPECTED_APP_SHA256",
        "EXPECTED_APP_MANIFEST_SHA256",
        "EXPECTED_APP_BUILD_INPUTS_SHA256",
        "wait_task_and_record",
        "get-log-events",
        "all(. == true)",
    ):
        assert expected in probe

    evidence_tf = ROLLOUT_EVIDENCE_TF.read_text()
    runtime_evidence_tf = RUNTIME_EVIDENCE_TF.read_text()
    for required in (
        "object_lock_enabled = true",
        'mode = "GOVERNANCE"',
        "days = 3650",
        'key_usage                = "SIGN_VERIFY"',
        'customer_master_key_spec = "RSA_3072"',
        '"s3:DeleteObjectVersion"',
        '"s3:GetBucketObjectLockConfiguration"',
        '"s3:PutObjectRetention"',
        '"kms:Sign"',
        '"kms:Verify"',
        '"dynamodb:TransactWriteItems"',
    ):
        assert required in evidence_tf
    assert "aws_kms_key.openclaw_rollout_evidence.arn" in evidence_tf
    assert "aws_kms_key.openclaw_rollout_signing.arn" in evidence_tf
    assert '"kms:ResourceAliases"' not in evidence_tf
    assert '"arn:aws:kms:ap-northeast-1:718959508629:key/*"' not in evidence_tf
    assert "exact_rollout_kms_alias_scope" not in guard
    assert "not_resources = [aws_kms_key.openclaw_rollout_signing.arn]" in (runtime_evidence_tf)
    runbook = RUNBOOK.read_text()
    assert "teamagent/dev/openclaw/rollout-canary" in runbook
    assert "aws secretsmanager create-secret" in runbook
    assert "xoxp-" in runbook


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
                "properties": [{"name": "aquasecurity:trivy:ImageID", "value": image_id}],
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
    assert {(entry["path"], entry["type"]) for entry in inventory_json["entries"]} == {
        ("app", "directory"),
        ("app/current.js", "symlink"),
        ("app/index.js", "file"),
    }
    file_entry = next(
        entry for entry in inventory_json["entries"] if entry["path"] == "app/index.js"
    )
    assert file_entry["contentSha256"] == hashlib.sha256(b"runtime\n").hexdigest()
    assert "rootfsTarSha256" not in inventory_json["subject"]
    assert "RootfsTarSha256" not in sbom.read_text()
    assert "rootfsTarSha256" not in equivalence.read_text()
    assert equivalence_json["wholeFilesystemExactMatch"] is True
    assert equivalence_json["pathTypeModeOwnerSizeLinkContentMultisetExact"] is True

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


def test_canonical_filesystem_inventory_ignores_tar_metadata_and_order(
    tmp_path: Path,
) -> None:
    payload = b"same-content\n"
    tar_paths = [tmp_path / "export-a.tar", tmp_path / "export-b.tar"]
    for iteration, tar_path in enumerate(tar_paths):
        members: list[tuple[tarfile.TarInfo, io.BytesIO | None]] = []
        directory = tarfile.TarInfo("app")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        directory.uid = 65532
        directory.gid = 65532
        directory.mtime = 100 + iteration
        members.append((directory, None))
        regular = tarfile.TarInfo("app/index.js")
        regular.size = len(payload)
        regular.mode = 0o444
        regular.uid = 65532
        regular.gid = 65532
        regular.mtime = 200 + iteration
        members.append((regular, io.BytesIO(payload)))
        if iteration:
            members.reverse()
        with tarfile.open(tar_path, "w") as archive:
            for member, stream in members:
                archive.addfile(member, stream)

    image_id = f"sha256:{'a' * 64}"
    trivy = tmp_path / "trivy.json"
    trivy.write_text(json.dumps(_minimal_trivy_sbom(image_id)))
    inventory_hashes: list[str] = []
    for iteration, tar_path in enumerate(tar_paths):
        inventory = tmp_path / f"inventory-{iteration}.json"
        generated = subprocess.run(
            [
                "python3",
                str(FILESYSTEM_SBOM),
                "--rootfs-tar",
                str(tar_path),
                "--trivy-sbom",
                str(trivy),
                "--inventory-output",
                str(inventory),
                "--sbom-output",
                str(tmp_path / f"sbom-{iteration}.json"),
                "--equivalence-output",
                str(tmp_path / f"equivalence-{iteration}.json"),
                "--image-id",
                image_id,
                "--manifest-digest",
                f"sha256:{'b' * 64}",
                "--config-digest",
                f"sha256:{'c' * 64}",
            ],
            check=False,
            text=True,
            capture_output=True,
        )
        assert generated.returncode == 0, generated.stderr
        inventory_hashes.append(hashlib.sha256(inventory.read_bytes()).hexdigest())
    assert inventory_hashes[0] == inventory_hashes[1]
