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
SLACK_CANARY_TOKEN_SHA256 = "34fc25aac72a3608fc4fe8c0914f128c51aa767b93d2d4ff4915d35aa9415e19"  # gitleaks:allow トークンの sha256 ハッシュ（秘密そのものではない）


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
    assert lock["runtime"]["nodeVersion"].startswith("26.")
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


def test_dockerfile_uses_exact_arm64_children_and_chainguard_final() -> None:
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
    assert _docker_arg_values(dockerfile, "RUNTIME_ARM64_DIGEST") == [
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
        "cgr.dev/chainguard/node:latest@${RUNTIME_ARM64_DIGEST} AS runtime",
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
        r'^ENTRYPOINT \["/usr/bin/node", "/opt/teamagent/entrypoint.mjs"\]$',
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
    # 診断 env は allowlist に載っていないと execve で黙って捨てられる（2026-09-04 実測）。
    # TEAMAGENT_CALLER_IDENTITY_TRACE がここから漏れていたため、タスク定義へ注入しても
    # plugin の traceEnabled が常に false になり、トレース行が 14 日間 1 行も出なかった。
    diagnostic = set(_js_string_array(entrypoint, "DIAGNOSTIC_ENV"))
    assert diagnostic == {
        "TEAMAGENT_CALLER_IDENTITY_TRACE",
        "CONNECT_ADMIN_NAME",
    }
    # 秘密値の受け皿にしない（allowlist の意味が消える）。
    assert diagnostic.isdisjoint(passthrough)
    assert diagnostic.isdisjoint(required_secrets)
    assert "copyDefined(process.env, childEnv, DIAGNOSTIC_ENV)" in entrypoint
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
    assert container["healthCheck"]["command"][1] == "/usr/bin/node"
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
    assert len(inventory_names) == len(set(inventory_names)) == 38
    assert set(inventory_names) == set(included)
    assert {
        "chitchat",
        "recommend",
        "proposal_campaign",
        "mail_constraints",
        "workspace_search",
        "proposal_deck",
        "proposal_builder",
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
    proposal_activation = {
        "kind": "envAllTrue",
        "names": ["USE_PROPOSAL_BUILDER_TOOLS"],
    }
    assert activation_by_name["proposal_builder_submit"] == proposal_activation
    assert activation_by_name["proposal_builder_status"] == proposal_activation
    assert activation_by_name["x_voice_search"] == {
        "kind": "envAllTrue",
        "names": ["USE_X_RESEARCH_TOOLS"],
    }
    assert activation_by_name["video_approval"] == {"kind": "never"}
    # ☑️確認済みボタンの押下処理。既定 OFF（default_enabled にも入らない）で、
    # 解禁は USE_DIGEST_ACK_TOOL 1 本に束ねる＝描画フラグ側だけ ON にしても発火しない。
    assert activation_by_name["digest_ack"] == {
        "kind": "envAllTrue",
        "names": ["USE_DIGEST_ACK_TOOL"],
    }
    assert activation_by_name["slack_summary"] == {
        "kind": "envAllTrue",
        "names": ["USE_SLACK_SUMMARY_TOOL"],
    }
    assert activation_by_name["attachment_assist"] == {
        "kind": "envAllTrue",
        "names": ["USE_ATTACHMENT_TOOLS"],
    }
    assert activation_by_name["video_capture"] == {
        "kind": "envAllTrue",
        "names": ["USE_VIDEO_CAPTURE_TOOL"],
    }
    assert activation_by_name["web_research"] == {
        "kind": "envAllTrue",
        "names": ["USE_WEB_RESEARCH_TOOL"],
    }
    effects = {tool["effect"] for tool in scope["tools"]}
    assert "gmail-draft-write-no-send" in effects
    assert "calendar-write-no-invite" in effects
    assert "calendar-freebusy-read-only" in effects
    assert "external-job-submit-s3-write" in effects
    assert "slack-thread-channel-read-analysis" in effects
    assert "slack-file-read-analysis" in effects
    assert "external-video-read-slack-file-delivery" in effects
    assert "external-web-search-read-only" in effects
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
    assert "s3-write" in tools_by_name["proposal_builder_submit"]["effect"]
    # clientkarte は Drive バイナリ DL + Slack file upload を行う（**既定 ON** の env gate 付き）。
    assert "slack-file-delivery" in tools_by_name["clientkarte"]["effect"]
    karte_gate = tools_by_name["clientkarte"]["sideEffectGate"]
    assert "KARTE_ATTACH_DOCS" in karte_gate
    # 台帳が運用実態と食い違わないことを固定する（2026-08-19 レビュー H2）:
    # mcp task definition の env は validate_plan(mode=sync) が live と完全一致を要求するため、
    # この変数は「素の apply で即止まる kill switch」ではない。台帳がそう読める文言に
    # 戻ったら落とす。
    assert "signed release" not in karte_gate.lower().replace("-", " ") or (
        "not a no-release kill" in karte_gate
    ), "sideEffectGate が『署名リリース無しで止まる』と読める文言に戻っている"
    assert "migration" in karte_gate, "env を倒すのに mode=migration 経路が要ることを台帳に書くこと"
    # 2026-08-19 レビュー 要修正3 の実測結果（2026-08-20）:
    # ClientKarteSkill を作る runtime は mcp ECS と runtime/slack_bot.py の 2 つあるが、
    # 後者を載せていた EC2 systemd worker は停止中（teamagent-dev-worker / 2026-08-03 以降）で、
    # ECS には slack_bot を動かすサービスが無い。台帳が「EC2 側も現役の第 2 経路」と
    # 読める書き方に戻ったら落とす。
    assert "stopped since" in karte_gate, "EC2 slack_bot 経路が現役でない実測を台帳に残すこと"
    # 2026-08-20 ユーザー裁定 A: 資料名・Drive リンクも DM 限定。台帳が「バイトだけ DM」に
    # 戻ったら落とす（旧文言 "File bytes go to the requester's DM only" が復活したら赤）。
    assert "ruling A" in karte_gate
    assert "never a document name" in karte_gate, (
        "チャンネルには資料名を出さない（件数だけ）ことを台帳に書くこと"
    )
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
        "enable_omiyage_report",
        "USE_OMIYAGE_REPORT_TOOLS",
        "enable_x_research",
        "USE_SLACK_SUMMARY_TOOL",
        "USE_ATTACHMENT_TOOLS",
        "USE_VIDEO_CAPTURE_TOOL",
        "USE_WEB_RESEARCH_TOOL",
    ):
        assert gate in terraform
    # カルテの関連資料機能は「本番で止められる状態」を terraform 側に持つのが条件
    # （既定 ON。倒すには mode=migration/kind=runtime の apply が要る＝素の apply では
    # env parity guard に落とされる。それでも変数が結線されていること自体は前提条件）。
    # ここを上の素の部分文字列ループに混ぜてはいけない: ``KARTE_ATTACH_DOCS`` は
    # 兄弟 env ``KARTE_ATTACH_DOCS_MAX`` の部分文字列なので、kill switch の行を丸ごと
    # 消してもループは緑のまま通る（変異テストで実測）。env の結線と fail-safe 既定値を
    # 行単位で固定する。
    assert (
        '{ name = "KARTE_ATTACH_DOCS", value = var.karte_attach_docs ? "true" : "false" },'
        in fargate
    ), "カルテ添付の kill switch env が mcp task definition に結線されていない"
    karte_var_start = terraform.index('variable "karte_attach_docs" {')
    karte_var_block = terraform[karte_var_start : terraform.index("\n}", karte_var_start)]
    assert "default     = true" in karte_var_block, (
        "karte_attach_docs の既定は true（カルテと資料を一緒に出すのが要求そのもの）"
    )
    assert "proposal_builder_sync_runtime_verified" not in terraform
    table_start = terraform.index('resource "aws_dynamodb_table" "proposal_builder_jobs"')
    table_end = terraform.index("\nresource ", table_start + 1)
    proposal_table = terraform[table_start:table_end]
    for table_contract in (
        'billing_mode = "PAY_PER_REQUEST"',
        'hash_key     = "job_id"',
        'name = "job_id"',
        "server_side_encryption {",
        "point_in_time_recovery {",
        'attribute_name = "expires_at"',
        "prevent_destroy = true",
    ):
        assert table_contract in proposal_table
    ledger_start = fargate.index('sid       = "ProposalBuilderJobLedger"')
    ledger_end = fargate.index("\n  statement {", ledger_start)
    proposal_ledger = fargate[ledger_start:ledger_end]
    for action in ("dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"):
        assert action in proposal_ledger
    assert "aws_dynamodb_table.proposal_builder_jobs.arn" in proposal_ledger
    assert "dynamodb:Scan" not in proposal_ledger
    assert "dynamodb:DeleteItem" not in proposal_ledger
    assert (
        '{ name = "PROPOSAL_JOBS_TABLE", value = aws_dynamodb_table.proposal_builder_jobs.name }'
    ) in fargate
    for timing_env, value in (
        ("PROPOSAL_JOB_HEARTBEAT_SECONDS", "30"),
        ("PROPOSAL_JOB_STALE_SECONDS", "180"),
        ("PROPOSAL_JOB_RETRY_AFTER_SECONDS", "30"),
    ):
        assert f'{{ name = "{timing_env}", value = "{value}" }}' in fargate
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
    assert '"/usr/bin/node"' in task_filter
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


CALLER_IDENTITY_PROBE = Path(__file__).resolve().parent / "openclaw_caller_identity_probe.mjs"


def _caller_identity_report() -> dict[str, Any]:
    """caller-identity プラグインの実物を本番形状の ctx で駆動した結果を返す。

    2026-08-07: チャンネル経路は 07-31 の導入以来ずっと全断していたのに、
    契約テスト(test_openclaw_runtime_image.py)は OPENCLAW_RUNTIME_TEST_IMAGE 未設定で
    スキップされ、CodeBuild のビルド経路(build-bundle.sh)からも呼ばれないため
    7日間「緑のまま」だった。ここは環境変数もイメージも要らない形で常に走らせる。
    """
    completed = subprocess.run(
        ["node", str(CALLER_IDENTITY_PROBE)],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_channel_mention_binds_the_conversation_despite_the_thread_suffix() -> None:
    """チャンネルの app_mention でツール呼び出しが通ること。

    上流はセッション鍵由来の会話 id を渡すため、チャンネルでは
    `c0b0pqd83n2:thread:<ts>` の形になる。末尾アンカーの正規表現では解決できず、
    本番では `missing=[channelId]` でツールが全ブロックされていた。
    """
    channel = _caller_identity_report()["channel_threaded"]
    assert channel["inboundAccepted"] is True
    assert channel["blocked"] is False, channel["blockReason"]
    assert channel["claimChannel"] == "C0B0PQD83N2"
    assert channel["warnings"] == []


def test_direct_message_binding_is_unchanged_by_the_thread_suffix_handling() -> None:
    """DM は従来どおり peer user id の別名で通ること（回帰なしの確認）。"""
    direct = _caller_identity_report()["direct_message"]
    assert direct["blocked"] is False, direct["blockReason"]
    assert direct["claimChannel"] == "DM:U09CX1CCBLN"


def test_unknown_conversation_suffix_still_fails_closed() -> None:
    """会話 id を取り出せない値は従来どおり拒否されること（緩めていないことの証明）。"""
    malformed = _caller_identity_report()["channel_malformed_suffix"]
    assert malformed["blocked"] is True
    assert malformed["claimChannel"] is None
    # 診断ログは候補ごとの可否だけを出し、生値は含めない。
    assert any("missing=[channelId]" in warning for warning in malformed["warnings"])
    assert all("c0b0pqd83n2" not in warning for warning in malformed["warnings"])


def test_thread_suffix_removal_stays_narrow_and_fails_closed() -> None:
    """`:thread:<ts>` 以外へ除去を広げていないこと（除去を緩める変異を赤くする）。

    - `:reply:<ts>`   … 貪欲な正規表現にすると誤って通る
    - `:thread:`(空)  … `[^:]+` を `[^:]*` にすると誤って通る
    - DM + `:thread:` … DM ブランチを除去後の値で照合すると誤って通る
    いずれも上流が作らない形なので、拒否のままであることが入力面の広がりを防ぐ。
    """
    report = _caller_identity_report()
    for case in ("channel_unknown_suffix", "channel_empty_thread", "dm_with_thread_suffix"):
        assert report[case]["blocked"] is True, case
        assert report[case]["claimChannel"] is None, case


# ── 第3層防御: 連携 URL 捏造の封鎖 ──────────────────────────────────────
# 本番実測 2026-08-31: 利用者の「連携」に対し、Aico が 0 tool call のまま
# https://connect.openclaw.ai/oauth/google?user_id=... を捏造して返した。
# MCP 境界の決定論分岐(_maybe_redirect_to_connect)は tool 呼び出しが発生して
# 初めて効くため、この経路には届かない。before_agent_finalize が最後の砦になる。


def test_fabricated_connect_url_forces_another_pass_that_calls_the_tool() -> None:
    """0 tool call で捏造 URL を返そうとしたら、送信させず再パスを要求すること。

    定型文へ置換するのではなく `oauth_connect` を実際に呼ばせる形にしてある。
    上流の再パス前置き(embedded-agent:1773)は「明示的に要求されない限りツールを
    再実行するな」と指示するため、instruction 側で明示要求しないと握り潰される。
    """
    guarded = _caller_identity_report()["connect_fabricated_zero_tool"]
    assert guarded["intervened"] is True
    assert guarded["action"] == "revise"
    assert "明示的にツール実行を要求します" in guarded["instruction"]
    assert "oauth_connect" in guarded["instruction"]
    assert guarded["idempotencyKey"] == "connect-url-fabrication"
    assert guarded["maxAttempts"] == 1


def test_plugin_never_writes_a_url_itself() -> None:
    """plugin 自身は URL を書かない(#352 と同じ規律)。

    instruction は「ツールを呼べ」であって、リンクの代替提示ではない。
    """
    guarded = _caller_identity_report()["connect_fabricated_zero_tool"]
    assert "http://" not in guarded["instruction"]
    assert "https://" not in guarded["instruction"]
    assert "http" not in (guarded["reason"] or "")


def test_ordinary_replies_are_not_touched() -> None:
    """判定は intent ではなく出力。連携 URL を含まない応答には触れないこと。

    intent 判定を主軸にすると「〇〇社との連携について提案書を」まで奪う
    (connect_intent.py の残差法が禁じた誤爆)。出力検証はこれを構造的に回避する。
    """
    report = _caller_identity_report()
    assert report["connect_no_url_zero_tool"]["intervened"] is False
    assert report["connect_other_tool_generic_url"]["intervened"] is False


def test_runs_that_actually_called_the_tool_are_excluded() -> None:
    """oauth_connect を呼んだ run の URL は正規発行。介入しないこと。"""
    assert _caller_identity_report()["connect_after_oauth_connect"]["intervened"] is False


def test_intervention_budget_stops_the_loop_without_relying_on_upstream() -> None:
    """同一 run の 2 回目は自前予算で打ち切ること。

    上流にも予算はある(runId x idempotencyKey・既定 1 回)が、ループ不在を
    上流挙動に依存させない。
    """
    budget = _caller_identity_report()["connect_budget_exhausted"]
    assert budget["firstIntervened"] is True
    assert budget["intervened"] is False


def test_recovered_run_is_not_intervened_again() -> None:
    """再パスで oauth_connect が呼ばれた run には再介入しないこと。

    ループ不在を上流の clientToolCalls ゲートではなく、自前カウンタ(条件 a)で担保する。
    """
    recovered = _caller_identity_report()["connect_recovered_after_tool_call"]
    assert recovered["firstIntervened"] is True
    assert recovered["recoveredIntervened"] is False


def test_missing_assistant_message_fails_open() -> None:
    """本文が無ければ利用者にも届かない。触らないこと。"""
    assert _caller_identity_report()["connect_missing_message"]["intervened"] is False


def test_authoritative_run_binding_is_enforced_on_finalize() -> None:
    """event と ctx の runId が食い違う finalize は不介入。

    signToolCall と同じ「権威 run 束縛」の規律を、この hook でも緩めていないこと。
    """
    report = _caller_identity_report()
    assert report["connect_run_mismatch"]["intervened"] is False
    # 別 run 自体が 0 tool call なら見逃さない(カウンタが run 単位である証明)。
    assert report["connect_other_run_zero_tool"]["intervened"] is True


def test_connect_url_classification_matches_the_single_source_of_truth() -> None:
    """URL 境界は tests/fixtures/connect_url_patterns.json が単一正本であること。

    実装と期待値が別々に育つとドリフトする。fixture を変えたら必ずここが動く。
    """
    matrix = _caller_identity_report()["connect_url_pattern_matrix"]
    assert matrix, "fixture が空"
    mismatched = [row for row in matrix if row["expectMatch"] != row["matched"]]
    assert not mismatched, f"fixture と実装が食い違う: {mismatched}"


def test_intervention_log_keeps_the_g7_discipline() -> None:
    """ログに本文・URL 実体・Slack 識別子を載せないこと(G7)。

    捏造 URL には user_id が埋まっていた実績があるため、URL の素通しは
    それ自体が G7 違反になる。
    """
    guarded = _caller_identity_report()["connect_fabricated_zero_tool"]
    blocked = [log for log in guarded["logs"] if "connect_url_fabrication_blocked" in log]
    assert blocked, "介入ログが出ていない"
    for log in blocked:
        assert "http" not in log
        assert "openclaw.ai" not in log
        assert "U09MBDFQ16J" not in log
        assert "連携リンク" not in log
        assert "kinds=" in log
        assert "outcome=revised" in log


def test_connect_guard_ledger_is_bounded_without_agent_end() -> None:
    """agent_end が発火しない run を大量に流しても台帳が無制限に育たないこと。

    掃除を agent_end(releaseAgentRun)だけに任せると、abort/crash/timeout 経路の
    run が残留し、長寿命プロセスでリークする(2026-08-31 レビュー指摘)。
    TTL 掃除に加えて上限超過時に最古から捨てるため、最初の run の予算記録は
    やがて落ちて再び介入できるようになる。台帳が無制限に育つ実装では
    `firstEvicted` が False のままになる。
    """
    bound = _caller_identity_report()["connect_ledger_bound"]
    assert bound["firstIntervened"] is True
    assert bound["secondBlockedByBudget"] is True, "自前予算が効いていない"
    assert bound["threw"] is None, bound["threw"]
    assert bound["firstEvicted"] is True, "上限退避が効かず台帳が無制限に育つ"


def test_connect_guard_ledger_entries_expire_by_ttl() -> None:
    """TTL 超過の run 記録が掃除されること。

    上限退避は「上限を超えたとき」しか効かないため、少数 run が長時間残る
    ケースはこの TTL が守る。上限退避だけでは緑のままになる穴を塞ぐ。
    """
    ttl = _caller_identity_report()["connect_ledger_ttl"]
    assert ttl["firstIntervened"] is True
    assert ttl["blockedWhileFresh"] is True, "TTL 内なのに予算が効いていない"
    assert ttl["expiredIntervened"] is True, "TTL 超過の記録が掃除されていない"


# ── 連携依頼の 3 層防御（2026-09-03） ─────────────────────────────────────
# 本番実測 2026-09-03: 利用者が DM で「連携」と送っても Aico がツールを一度も呼ばず
# 「未登録／管理者に問い合わせ」と自作回答する事故が同一 DM で 5 回以上続いた。
# mcp 側には一切届いていない（mcp_connect_intent がゼロ）ので、MCP 境界の決定論分岐も
# 上の URL 検出（応答に URL が無い）も効かない。3 層で塞ぐ:
#   層1: before_agent_reply で短い連携依頼を検出し、モデルを通さず oauth_connect を呼ぶ。
#   層2: before_agent_finalize で「0 tool call × 短い連携依頼」を revise で再パスさせる。
#   層3: 再パス後も 0 tool call なら reply_payload_sending で定型文に置換する。

CONNECT_ZERO_TOOL_INSTRUCTION = (
    "利用者は Google/Slack 連携を依頼しています。`oauth_connect` ツールを必ず呼び、"
    "その戻り値の message とリンクを一字も変えずに提示してください。"
    "自分で原因を推測したり、管理者への問い合わせを案内したりしてはいけません。"
)
CONNECT_DIAGNOSTIC_RE = re.compile(
    r"診断: CONNECT-Z01 \d{4}-\d{2}-\d{2} \d{2}:\d{2} JST U09CX1CCBLN$"
)


def test_short_connect_request_is_answered_by_the_tool_without_the_model() -> None:
    """層1: 短い連携依頼は before_agent_reply で handled され、oauth_connect が 1 回呼ばれること。

    {handled:true, reply} を返すとハーネスは reply をそのまま返してモデルを起動しない
    （openclaw 2026.7.1 get-reply:5620-5623）。戻り値 message はそのまま Slack へ出る。
    """
    guarded = _caller_identity_report()["connect_l1_short_request"]
    assert guarded["handled"] is True
    assert guarded["toolCallCount"] == 1
    assert guarded["toolName"] == "oauth_connect"
    assert guarded["methods"] == ["initialize", "notifications/initialized", "tools/call"]
    assert guarded["replyText"].startswith("以下のリンクから連携してください")
    assert guarded["sessionHeader"] == "sess-1"
    assert guarded["bearerHeader"].startswith("Bearer ")


def test_deterministic_path_reuses_the_existing_signed_claim_contract() -> None:
    """層1 の tools/call は before_tool_call 経由と同じ claim 契約で署名されること。

    mcp 側（caller_claim.py）の検証をそのまま通す＝新しい信頼境界を作らない。
    channel は mcp が要求する実 Slack 会話 id（^[CDG]…）で、DM の内部別名 DM:U… ではない。
    """
    guarded = _caller_identity_report()["connect_l1_short_request"]
    claim = guarded["claimPayload"]
    assert claim["v"] == 2
    assert claim["iss"] == "teamagent-openclaw"
    assert claim["aud"] == "teamagent-mcp"
    assert claim["tool"] == "oauth_connect"
    assert claim["sub"] == "U09CX1CCBLN"
    assert claim["team"] == "T07MU5P2PBR"
    assert re.fullmatch(r"[CDG][A-Z0-9]{8,}", claim["channel"]), claim["channel"]
    assert claim["run_id"].startswith("connect-l1-")
    assert claim["tool_call_id"] == claim["run_id"]
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", claim["run_id"])
    assert claim["exp"] - claim["iat"] == 60
    declared = guarded["declaredContext"]
    assert declared["slack_user_id"] == claim["sub"]
    assert declared["slack_team_id"] == claim["team"]
    assert declared["channel_id"] == claim["channel"]
    assert "thread_ts" not in declared


def test_long_connect_mention_does_not_take_the_deterministic_path() -> None:
    """層1 は「〇〇社との連携について提案書を」を通さず通常処理へ流すこと（誤爆しない）。"""
    report = _caller_identity_report()
    assert report["connect_l1_long_request"]["handled"] is False
    assert report["connect_l1_long_request"]["toolCallCount"] == 0
    assert report["connect_l1_non_user_trigger"]["handled"] is False
    assert report["connect_l1_non_user_trigger"]["toolCallCount"] == 0


def test_deterministic_path_mints_once_per_inbound_and_finds_bound_runs() -> None:
    """同じ受信に 2 度は鋳造しない。message_received が runId を伴い先に束縛されても見つける。"""
    report = _caller_identity_report()
    assert report["connect_l1_repeat_same_message"]["toolCallCount"] == 1
    assert report["connect_l1_repeat_same_message"]["handled"] is False
    assert report["connect_l1_bound_run"]["handled"] is True
    assert report["connect_l1_bound_run"]["toolCallCount"] == 1


def test_deterministic_path_failures_fall_to_the_next_layer() -> None:
    """層1 の「到達できなかった」失敗は fail-open ではなく fail-to-next-layer であること。

    fetch 失敗 / HTTP 5xx / JSON-RPC error / message 欠落 / bearer 無し /
    正準 channel 無し のいずれでも、同じ受信がモデル経路へ進んだとき
    層2（0 tool call × 短い連携依頼）が revise を返す。

    ⚠️ mcp が **利用者向けに整形した失敗文面** を返してきたケース（`{"error": …}`）は
    ここに含めない。それは「到達できなかった」ではなく「到達して答えが返った」であり、
    2026-09-04 以降は利用者へそのまま届ける（下の test_... _user_error 参照）。
    """
    failures = _caller_identity_report()["connect_l1_failures"]
    expected_reason = {
        "fetch_throws": "fetch_failed",
        "http_500": "mcp_http_500",
        "rpc_error": "mcp_rpc_error",
        "no_message": "mcp_invalid_result",
        "no_bearer": "no_mcp_bearer",
        "no_canonical_channel": "no_canonical_channel",
    }
    assert set(failures) == set(expected_reason)
    for case, reason in expected_reason.items():
        outcome = failures[case]
        assert outcome["l1Handled"] is False, case
        assert outcome["fallthroughReason"] == reason, case
        assert outcome["layer2Intervened"] is True, case
        assert outcome["layer2Key"] == "connect-zero-tool", case


def test_mcp_user_facing_error_is_delivered_verbatim_not_discarded() -> None:
    """mcp が返す利用者向けの失敗文面を、捨てずにそのまま届けること（(E) の本体）。

    実測の代表例が新規ユーザーの CONNECT-I02（Slack プロフィールに会社メールが無い）。
    mcp は失敗も成功と同じ TextContent の JSON で返し（server.py:442-445,819）、
    その `error` 文面は既に「何をすればよいか」＋転送用の診断行まで整形済み
    （skill.py:228-236 / connect_diagnostics.py:260-277）。

    旧実装はこれを一律 `mcp_tool_error` に潰してモデル経路へ落としていたため、
    新規ユーザーには「無言」かモデルの自作回答（「管理者へお問い合わせください」）
    しか届かなかった。これが「誰でも連携できる」を破っていた中心的な穴。
    """
    outcome = _caller_identity_report()["connect_l1_user_facing_error"]
    # 層1 が handled で答える＝モデルは起動しない。
    assert outcome["handled"] is True
    assert outcome["toolCallCount"] == 1
    # 文面は mcp の error をそのまま（要約も言い換えもしない）。
    assert outcome["replyText"] == outcome["mcpErrorText"]
    assert "CONNECT-I02" in outcome["replyText"]
    assert "Slack プロフィール" in outcome["replyText"]
    # 観測ログには結果の種別だけを出す（本文・識別子は載せない＝G7）。
    assert any("result=user_error" in line for line in outcome["infos"])


def test_deterministic_path_logs_keep_the_g7_discipline() -> None:
    """層1 のログに本文・URL・Slack 識別子・bearer を載せないこと。"""
    report = _caller_identity_report()
    logs = list(report["connect_l1_short_request"]["infos"])
    for outcome in report["connect_l1_failures"].values():
        logs.extend(outcome["logs"])
    assert logs
    for log in logs:
        assert "U09CX1CCBLN" not in log
        # reason=mcp_http_500 のような理由コードは許す。URL 実体だけを禁じる。
        assert "http://" not in log
        assert "https://" not in log
        assert "連携" not in log
        assert "b" * 48 not in log


def test_zero_tool_reply_to_short_connect_request_forces_another_pass() -> None:
    """層2: 0 tool call × 短い連携依頼 → revise（固定 instruction・maxAttempts 1）。"""
    guarded = _caller_identity_report()["connect_zero_tool_short_request"]
    assert guarded["intervened"] is True
    assert guarded["instruction"] == CONNECT_ZERO_TOOL_INSTRUCTION
    assert guarded["idempotencyKey"] == "connect-zero-tool"
    assert guarded["maxAttempts"] == 1
    assert "http" not in guarded["instruction"]


def test_zero_tool_rule_does_not_fire_on_long_requests_or_after_a_tool_call() -> None:
    """層2 は長文で誤爆せず、oauth_connect を呼んだ run には介入しないこと。"""
    report = _caller_identity_report()
    assert report["connect_zero_tool_long_request"]["intervened"] is False
    assert report["connect_zero_tool_with_tool_call"]["toolBlocked"] is False
    assert report["connect_zero_tool_with_tool_call"]["intervened"] is False


def test_exhausted_zero_tool_run_is_replaced_with_the_fixed_text() -> None:
    """層3: 再パス後も 0 tool call なら送信直前に定型文へ置換し、2 通目は取り消すこと。

    診断行は「CONNECT-Z01 <JST時刻> <Slack user id>」で、URL も秘匿値も含まない。
    """
    guarded = _caller_identity_report()["connect_zero_tool_fallback"]
    assert guarded["firstIntervened"] is True
    assert guarded["intervened"] is False, "予算切れ後に revise を返している"
    first, second = guarded["deliveries"]
    text = first["payload"]["text"]
    assert text.startswith("連携リンクの発行に失敗しました。もう一度『連携』と送ってください。")
    assert "管理者（小俣）" in text
    assert CONNECT_DIAGNOSTIC_RE.search(text), text
    assert "http" not in text
    assert "未登録" not in text
    assert second["cancel"] is True
    armed = [log for log in guarded["logs"] if "outcome=fallback_armed" in log]
    replaced = [log for log in guarded["logs"] if "outcome=replaced" in log]
    assert armed and replaced
    for log in guarded["logs"]:
        assert "U09CX1CCBLN" not in log
        assert "未登録" not in log


def test_fixed_text_replacement_is_bound_to_the_run() -> None:
    """層3 は runId が食い違う配信・武装していない run の配信には触らないこと。"""
    report = _caller_identity_report()
    assert report["connect_zero_tool_fallback_run_mismatch"]["deliveries"] == [None]
    assert report["connect_zero_tool_fallback_not_armed"]["deliveries"] == [None]


def test_short_connect_request_phrases_match_the_single_source_of_truth() -> None:
    """短い連携依頼の境界は tests/fixtures/connect_request_phrases.json が単一正本であること。

    must_match は全件で oauth_connect が 1 回呼ばれ 0 tool の応答が出ない。
    must_not_match は層1 を通らない（tools/call 0 件）。
    """
    fixture = json.loads((ROOT / "tests/fixtures/connect_request_phrases.json").read_text())
    assert len(fixture["must_match"]) >= 20
    matrix = _caller_identity_report()["connect_phrase_matrix"]
    assert len(matrix) == len(fixture["must_match"]) + len(fixture["must_not_match"])
    mismatched = [
        row
        for row in matrix
        if row["expectMatch"] != row["handled"]
        or (row["expectMatch"] and (row["toolCallCount"] != 1 or not row["replyIsToolMessage"]))
        or (not row["expectMatch"] and row["toolCallCount"] != 0)
    ]
    assert not mismatched, f"fixture と実装が食い違う: {mismatched}"


def test_deterministic_path_consumes_the_inbound_so_the_next_message_still_binds() -> None:
    """層1 が handled で返した受信を pending に残さないこと（2026-09-03 レビュー指摘 1）。

    handled で返すとモデルが起動せず before_model_resolve が走らない。受信を残すと
    同じ DM の次の受信で bindAgentRun が candidates=2 で run を拒否し、以後 10 分間
    すべてのツールが「trusted Slack run identity is missing or stale」でブロックされる。
    """
    outcome = _caller_identity_report()["connect_l1_next_message_tools_ok"]
    assert outcome["l1Handled"] is True
    assert outcome["nextRunRejected"] is False, "層1 の受信が pending に残り次の run が拒否された"
    assert outcome["nextToolBlocked"] is False


def test_deterministic_path_answers_again_within_the_ttl() -> None:
    """層1 成功の 30 秒後に再度「連携」が来ても、層1 が再び handled になること（指摘 2）。

    受信が残っていると候補 2 件で不発になり、モデル経路でも run 拒否で oauth_connect が
    block され層2/3 も届かない＝「必ず」が破れる。不発になる場合も無言にはせず
    reason=ambiguous_ingress で観測できる。
    """
    outcome = _caller_identity_report()["connect_l1_repeat_after_30s"]
    assert outcome["firstHandled"] is True
    assert outcome["secondHandled"] is True
    assert outcome["toolCallCount"] == 2
    assert outcome["ambiguous"] is False


def test_deterministic_path_never_uses_another_senders_inbound() -> None:
    """スレッドで別送信者 B の「連携」が pending でも、A の before_agent_reply は不発であること（指摘 3）。

    senderId 照合を外すと A に B の claim で B 専用リンクが返る。tools/call は 0 件。
    """
    outcome = _caller_identity_report()["connect_l1_other_sender_pending"]
    assert outcome["handled"] is False
    assert outcome["toolCallCount"] == 0


# ── ツール引数の二重包みを剥がす（2026-09-03） ──────────────────────────────
# 本番実測（OpenClaw の EFS 上のセッション記録を読み取り専用の Fargate プローブで集計）:
#   セッション記録 166 ファイル・tool call 363 件のうち 83 件（23%）が before_tool_call で
#   block（toolResult details.status="blocked" / deniedReason="plugin-before-tool-call"）。
#   内訳は `_user_context must be a plain object` 72 / `trusted Slack run identity is missing
#   or stale` 9 / `declared channel_id does not match the bound ingress` 2。
#   引数の包み形は {"arguments":{"_user_context":{…}}} が 74 件、
#   {"name":"teamagent__oauth_connect","arguments":{…}} が 2 件。
#   ツールを問わず発生（oauth_connect 105 / search 95 / calendar_event 19 / tiktok_* 31 …）で、
#   その run の tool call が全部 block された「全滅セッション」が 7 本以上あった。
#   モデル（Bedrock jp.anthropic.claude-haiku-4-5-20251001-v1:0）はブロックされた toolResult を
#   見て「技術的な問題」「管理者へお問い合わせ」と自作回答するため、利用者には
#   「連携できない」としか見えていなかった。


def test_double_wrapped_tool_arguments_reach_the_tool_in_canonical_form() -> None:
    """{"arguments":{…}} の二重包みが block されず、mcp へ正規形で届くこと。

    実測 74 件の主犯。剥がしたあとも `_user_context` は authoritative な署名済み値で
    上書きされるので、信頼境界は動かない（下の spoof テストで固定）。
    """
    wrapped = _caller_identity_report()["unwrap"]["single_arguments"]
    assert wrapped["blocked"] is False, wrapped["blockReason"]
    # 包みが剥がれ、ツール本来の引数が top に戻っていること。
    assert wrapped["signedTop"] == {"query": "q"}
    assert wrapped["signedKeys"] == ["_user_context", "query"]
    # `_user_context` は plugin が鋳造した authoritative 値。
    assert wrapped["signedContextKeys"] == [
        "caller_claim",
        "channel_id",
        "slack_team_id",
        "slack_user_id",
    ]
    assert wrapped["signedUserId"] == "U09CX1CCBLN"
    assert wrapped["claimUser"] == "U09CX1CCBLN"


def test_name_and_arguments_wrapper_is_unwrapped_for_any_tool() -> None:
    """{"name":…,"arguments":{…}} 形も剥がれ、oauth_connect 以外でも効くこと。

    実測 2 件は oauth_connect だったが、block は全ツールに散っている
    （search 95 / calendar_event 19 / mail_summary 14 …）ので unwrap も全ツール共通。
    """
    report = _caller_identity_report()["unwrap"]
    for case in ("name_and_arguments", "name_and_arguments_oauth"):
        assert report[case]["blocked"] is False, (case, report[case]["blockReason"])
        assert report[case]["signedUserId"] == "U09CX1CCBLN", case
    assert report["name_and_arguments"]["signedTop"] == {"query": "q"}
    # 2 段包みも剥がす（上限は 2 段）。
    assert report["double_arguments"]["blocked"] is False
    assert report["double_arguments"]["signedTop"] == {"query": "q"}


def test_triple_wrapped_arguments_still_fail_closed_with_the_p06_diagnostic() -> None:
    """3 段以上の包みは剥がさず従来どおり block（診断 P06）であること。

    剥がす段数に上限を置かないと、入力面が「任意の深さの任意の構造」まで広がる。
    """
    triple = _caller_identity_report()["unwrap"]["triple_arguments"]
    assert triple["blocked"] is True
    assert triple["diagCode"] == "CONNECT-P06"


def test_unwrap_does_not_move_the_trust_boundary() -> None:
    """包みの中で別人を騙っても、包まない場合と同じく拒否されること。

    unwrap が直すのは「どの階層を検査するか」だけで、検査そのものは 1 つも緩めない。
    ここが緑のままでないと、二重包みが検査回避の抜け道になる。
    """
    report = _caller_identity_report()["unwrap"]
    assert report["single_arguments_spoofed"]["blocked"] is True
    assert report["plain_spoofed"]["blocked"] is True
    # 包みの有無で拒否理由が変わらないこと（回避経路が生えていない証明）。
    assert (
        report["single_arguments_spoofed"]["diagCode"]
        == report["plain_spoofed"]["diagCode"]
        == "CONNECT-P05"
    )


def test_ordinary_tool_arguments_pass_through_the_unwrap_byte_identical() -> None:
    """通常の引数は unwrap 前後でバイト同一（同一参照）であること。

    正常系に触れないことが、この変更を「観測性の追加」に留める根拠になる。
    """
    unit = _caller_identity_report()["unwrap"]["unit"]
    assert unit["plain_identical"] is True
    assert unit["plain_depth"] == 0
    assert unit["plain_shape"] is None
    # 包みに見えるが包みでないものを剥がさないこと。
    assert unit["wrong_name_identical"] is True, "別ツール名の arguments を横流ししている"
    assert unit["extra_key_identical"] is True, "`arguments` 以外のキーが同居しても剥がしている"
    assert unit["non_object_identical"] is True, "値がオブジェクトでないのに剥がしている"
    assert unit["nested3_identical"] is True
    assert unit["nested3_depth"] == 0
    # 剥がす側の性質。
    assert unit["twice_depth"] == 2
    assert unit["twice_keys"] == ["_user_context", "query"]
    assert unit["bare_name_depth"] == 1
    assert unit["bare_name_shape"] == "name_arguments"


# ── 拒否の観測性（2026-09-03） ─────────────────────────────────────────────
# 14 日間、この plugin の warn は CloudWatch に 1 行も出ていなかった。
# 原因は register が before_tool_call にだけ api.logger を渡しておらず、
# signToolCall の block 経路が 1 行も書いていなかったこと。
# 上流 logger は既定では console.warn へ届くが、no-op logger に差し替わる経路
# （bundled-capability-runtime）と consoleLevel の設定で消えうるため、console にも直接書く。
# 一次検証は docs/design/connect_third_layer_defense.md §11（file:line つき）。

_BLOCK_DIAG_CASES = {
    "native_tool": "CONNECT-P01",
    "tool_name_mismatch": "CONNECT-P02",
    "run_binding": "CONNECT-P03",
    "invocation_binding": "CONNECT-P04",
    "session_binding": "CONNECT-P05",
    "stale_run_identity": "CONNECT-P03",
}


def test_every_block_path_writes_exactly_one_console_line() -> None:
    """block したら必ず 1 行、console に出ること（logger 設定に依存しない）。"""
    report = _caller_identity_report()["block_diagnostics"]
    for case, code in _BLOCK_DIAG_CASES.items():
        outcome = report[case]
        assert outcome["blocked"] is True, case
        assert outcome["consoleWarnCount"] == 1, (case, outcome["console"])
        line = outcome["console"][0]["text"]
        assert f"diagnostic={code}" in line, (case, line)


def test_block_logs_carry_id_shape_but_no_identifiers() -> None:
    """拒否ログには「形」だけを載せ、識別子の実値は載せないこと（G7）。

    Enterprise Grid の `W…` user id など想定外の形を、値を見ずに切り分けるための情報。
    """
    report = _caller_identity_report()["block_diagnostics"]
    for case in _BLOCK_DIAG_CASES:
        line = report[case]["console"][0]["text"]
        assert "id_shape=" in line, case
        assert "sender:" in line and "channel:" in line and "session:" in line, case
        # 実値（Slack user id / channel id / ts）が漏れていないこと。
        assert "U09CX1CCBLN" not in line, case
        assert "C0B0PQD83N2" not in line, case
        assert "1785206176.940189" not in line, case


def test_block_reasons_carry_a_forwardable_diagnostic_line() -> None:
    """利用者に届く block 文の末尾に、そのまま転送できる診断行が付くこと。

    SOUL(#380) が「診断: 行は一字も変えず提示」を規定しているので、利用者の
    スクリーンショット 1 枚から原因コードが判る。user id は載せない（G7）。
    """
    report = _caller_identity_report()["block_diagnostics"]
    for case, code in _BLOCK_DIAG_CASES.items():
        reason = report[case]["blockReason"]
        lines = reason.split("\n")
        assert len(lines) == 3, (case, reason)
        assert lines[0].startswith("teamagent-caller-identity: "), case
        assert lines[1] == "解決しない場合は、次の 1 行をそのまま管理者（小俣）へ送ってください:", (
            case
        )
        assert re.fullmatch(
            rf"診断: {code} \d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}} JST", lines[2]
        ), (case, lines[2])
        assert "U09CX1CCBLN" not in reason, case


def test_successful_tool_calls_stay_quiet() -> None:
    """正常系ではログが 1 行も増えないこと（拒否と unwrap だけを観測する）。"""
    quiet = _caller_identity_report()["block_quiet_on_success"]
    assert quiet["blocked"] is False
    assert quiet["consoleLineCount"] == 0
    assert quiet["warningCount"] == 0
    assert quiet["totalConsoleDelta"] == 0


# ── 層1 の脱出経路の観測（2026-09-03 の事故） ────────────────────────────────
# OC TD:43（dev 8a1560b・#381 の層1 入り）着地直後、DM の「連携」で層1 が発火せず
# モデル経路になった（OC ログに `[agents/tool-policy] tool policy removed 26 tool(s)`
# ＝モデル起動。層1 が handled を返していればモデルは起動しない）。
# plugin のログは CloudWatch に 1 行も無く、どの条件で落ちたか判別できなかった。
# 原因は fallthrough() を通る 3 経路以外がすべて無言の `return undefined` だったこと。

_LAYER1_SKIP_REASONS = {
    "on_not_slack_provider": "not_slack_provider",
    "on_trigger_not_user": "trigger_not_user",
    "on_missing_fields": "missing_session_or_sender_or_channel",
    "on_no_candidate_ingress": "no_candidate_ingress",
    "on_not_connect_request": "not_connect_request",
}


def test_layer1_hook_entry_is_observable_under_trace() -> None:
    """trace ON なら「hook が呼ばれた事実」が 1 行出ること。

    この 1 行が出ないなら before_agent_reply 自体が呼ばれていないと確定でき、
    plugin 側の条件分岐か上流側の hook 未発火かを切り分けられる。
    """
    report = _caller_identity_report()["layer1_trace"]
    assert report["on_answered"]["entered"] is True
    assert report["on_answered"]["enteredLine"] is not None
    # provider / trigger は識別子ではないので実値を出す（切り分けに要る）。
    assert "provider=slack" in report["on_answered"]["enteredLine"]
    assert "trigger=user" in report["on_answered"]["enteredLine"]
    assert report["on_answered"]["handled"] is True


def test_every_silent_layer1_exit_now_reports_a_reason() -> None:
    """無言で return していた 6 経路すべてが理由つきで観測できること。"""
    report = _caller_identity_report()["layer1_trace"]
    for case, reason in _LAYER1_SKIP_REASONS.items():
        assert report[case]["skippedReason"] == reason, (case, report[case]["skippedLine"])
        assert report[case]["handled"] is False, case
    # 6 番目（同じ受信への 2 度目の鋳造）。
    assert report["on_already_attempted"]["skippedReason"] == "already_attempted"
    # 切り分けに要る付帯情報。値そのものは出さない。
    assert "trigger=heartbeat" in report["on_trigger_not_user"]["skippedLine"]
    assert "missing=[sessionKey,senderId]" in report["on_missing_fields"]["skippedLine"]
    assert "id_shape=" in report["on_missing_fields"]["skippedLine"]
    # 受信を記録できていないのか照合が外れたのかを、件数で切り分けられること。
    assert "pending=0 bound=0" in report["on_no_candidate_ingress"]["skippedLine"]
    # 語彙不一致は本文を出さず正規化後の文字数だけ（G7）。
    assert "normalized_len=5 content_len=5" in report["on_not_connect_request"]["skippedLine"]


def test_inbound_recording_is_observable_under_trace() -> None:
    """受信を記録した事実も観測できること（層1 の no_candidate_ingress の切り分け）。

    `inbound recorded` が出ていて層1 が `no_candidate_ingress` なら照合の問題、
    出ていなければ受信そのものが記録できていない（`inbound rejected` を見る）。
    """
    report = _caller_identity_report()["layer1_trace"]
    line = report["on_answered"]["inboundLine"]
    assert line is not None
    assert "connect_request=true" in line
    assert "normalized_len=2" in line
    assert "pending=1" in line
    assert "id_shape=" in line
    # 語彙不一致の受信は connect_request=false で記録される。
    assert "connect_request=false" in report["on_not_connect_request"]["inboundLine"]
    # 本文も Slack 識別子も載せない（G7）。
    assert "こんにちは" not in report["on_not_connect_request"]["inboundLine"]
    assert "U09CX1CCBLN" not in line


def test_trace_is_off_by_default_and_does_not_change_behaviour() -> None:
    """既定（trace OFF）では詳細を出さないが、挙動は同じであること。

    通常の会話 1 通ごとに not_connect_request が出るとノイズになるため既定 OFF。
    切り分け中だけ OC のタスク定義で TEAMAGENT_CALLER_IDENTITY_TRACE=1 を注入する。
    """
    report = _caller_identity_report()["layer1_trace"]
    # 挙動は env に依存しない。
    assert report["off_answered"]["handled"] == report["on_answered"]["handled"] is True
    # 詳細は出ない。
    assert report["off_answered"]["entered"] is False
    assert report["off_answered"]["inboundRecorded"] is False
    assert report["off_not_connect_request"]["skippedReason"] is None
    assert report["off_no_candidate_ingress"]["skippedReason"] is None


def test_fallthrough_is_always_logged_even_without_trace() -> None:
    """層1 に入ったのに実行できなかった場合は、trace OFF でも必ず 1 行出ること。

    ここを trace に隠すと「層1 が動いたのに連携できない」事故が既定で無言に戻る。
    """
    outcome = _caller_identity_report()["layer1_trace"]["off_fallthrough"]
    assert outcome["fallthroughReason"] == "no_mcp_bearer"
    assert outcome["lineCount"] == 1, outcome["lines"]


# ── bind_agent_run / inbound rejected の G7（2026-09-03 レビュー指摘） ───────
# 旧実装は会話 id の実値を両側とも出していた:
#   `runChannelId=C0B0PQD83N2 pendingChannelIds=[DM:U09CX1CCBLN]`
# DM では resolveSlackChannel が `DM:<senderId>` に解決するため、これは
# **Slack user id そのもの**が stderr → CloudWatch に落ちることを意味する。
# 本 PR で emitPluginLog が console へ二重書きするようになり、上流のログレベル
# 抑制も効かなくなったため、実値を出す面をここで塞ぐ。
# 既存の G7 テストは `before_tool_call blocked` 行しか見ていなかった。


def test_bind_agent_run_log_never_emits_conversation_ids() -> None:
    """run と pending の会話 id が食い違う拒否ログに、実値が 1 つも出ないこと。

    DM の `DM:U…` は Slack user id そのものなので、形（先頭 1 文字）と件数だけにする。
    診断能力は落とさない: `matchChannelId=0` と形の不一致で切り分けられる。
    """
    outcome = _caller_identity_report()["g7"]["bind_run_channel_mismatch"]
    line = outcome["line"]
    assert line is not None, outcome["console"]
    assert "reason=no_unique_binding" in line
    # 実値が 1 つも無いこと。
    assert "U09CX1CCBLN" not in line, line
    assert "C0B0PQD83N2" not in line, line
    assert "DM:" not in line, line
    # 旧フィールド名が復活していないこと（変異の再発検知）。
    assert "runChannelId=" not in line, line
    assert "pendingChannelIds=" not in line, line
    # 形と件数は残っていること（切り分け能力を落としていない証明）。
    assert "matchChannelId=0" in line, line
    assert "runChannelShape=C" in line, line
    assert "pendingChannelShapes=[U]" in line, line
    assert "pendingChannelDistinct=1" in line, line


def test_inbound_rejection_log_never_emits_the_team_id() -> None:
    """他ワークスペースからの受信拒否ログに、team id の実値が出ないこと。"""
    outcome = _caller_identity_report()["g7"]["inbound_foreign_team"]
    line = outcome["line"]
    assert line is not None
    assert "reason=incomplete_or_foreign" in line
    assert outcome["foreignTeam"] not in line, line
    assert "T07MU5P2PBR" not in line, line
    assert "foreignTeam=" not in line, line
    # 真偽と形だけで「他ワークスペースから来た」と判ること。
    assert "foreign_team=true" in line, line
    assert "team:mismatch" in line, line
    # 送信者 id も出さない。
    assert "U09CX1CCBLN" not in line, line


def test_no_skill_declares_an_input_field_named_arguments() -> None:
    """`arguments` という入力フィールドを持つ skill が現れないこと（2026-09-03）。

    plugin の unwrap 規則 (a) は「トップのキーが `arguments` 1 つだけで、その値が
    プレーンオブジェクト」なら剥がす。`arguments` という名前のオブジェクト引数を
    唯一の必須項目に持つ skill が将来 1 個でも増えると、その正当な呼び出しが
    誤って剥がされ、`_user_context` を失って静かに P06 で落ちる。
    本 PR 時点では 45 skill すべてに存在しない。増えたらここで赤にして気付く。
    """
    import importlib
    import pkgutil

    import teamagent.skills as skills_pkg
    from teamagent.skills.base import SkillRegistry

    # 全 skill モジュールを import して registry を埋める（@register は import 時に走る）。
    for module in pkgutil.walk_packages(skills_pkg.__path__, f"{skills_pkg.__name__}."):
        importlib.import_module(module.name)

    names = SkillRegistry.list_all()
    # 検出器が空振りしていないこと（registry が空なら vacuous に緑になる）。
    assert len(names) >= 40, f"skill registry が {len(names)} 件しか埋まっていない"

    offenders = []
    for name in names:
        schema = SkillRegistry.get(name).input_schema.model_json_schema()
        if "arguments" in schema.get("properties", {}):
            offenders.append(name)
    assert offenders == [], (
        f"入力フィールド `arguments` を持つ skill: {offenders}。"
        " plugin の単一キー unwrap (a) が誤発火して P06 で静かに落ちる。"
        " skill 側で改名するか、unwrap 側にツール名の除外を入れること。"
    )


def test_unwrap_failure_is_converted_to_a_block_not_propagated() -> None:
    """unwrap が throw しても、上流へ委ねず block へ変換すること（fail-closed）。

    JSON 由来の params は getter を持てないので本番で throw は起きないが、
    unwrap の呼び出しが既存 try の外に出た瞬間にここが赤くなる。
    署名経路の例外はすべて block に落ちる、という規律を機械で固定する。
    """
    outcome = _caller_identity_report()["g7"]["unwrap_throws_fail_closed"]
    assert outcome["threw"] is None, outcome["threw"]
    assert outcome["blocked"] is True
    assert outcome["diagCode"] == "CONNECT-P06"


# ══ (D)(E) 保証経路: 「連携」と言ったら必ず何かが届く（2026-09-04） ═══════════
# ゴール: 新規／既存／過去のテストユーザーを問わず、「連携して」と言ったら
# 漏れなく連携リンク（または次の一手が分かる診断つき案内）が届くこと。
#
# 設計の核心（上流実物での比較・docs/design/connect_third_layer_defense.md §12）:
#   保証は `message_received`（非 conversation hook）に載せる。非 bundled plugin でも
#   `hooks.allowConversationAccess` の可否に依存せず登録される。層1 が載っている
#   `before_agent_reply` は conversation hook で、設定が 1 つ欠けるだけで
#   **診断も出ないまま黙って登録が捨てられる**（registry-D1_pYg_a.js:4224-4235）ため、
#   「必ず」の土台にはできない。


def test_connect_guarantee_never_goes_silent_for_any_user_state() -> None:
    """利用者状態 × 言い回し の全組み合わせで、必ず 1 通届くこと。

    無言になる組み合わせが 1 つでもあれば赤。これが「誰でも連携できる」の本体。
    """
    rows = _caller_identity_report()["guarantee_matrix"]
    # 7 状態 × fixture の must_match 全件。
    assert len(rows) >= 200, len(rows)
    states = {row["state"] for row in rows}
    assert states == {
        "new_user_without_email",
        "new_user_with_email",
        "existing_fully_connected",
        "existing_partially_connected",
        "mcp_unreachable",
        "mcp_http_500",
        "mcp_invalid_result",
    }
    silent = [(row["state"], row["text"]) for row in rows if not row["delivered"]]
    assert silent == [], f"無言になった組み合わせ: {silent}"
    # 二重投稿もしない（同じ受信に対しては 1 通）。
    duplicated = [
        (row["state"], row["text"], row["postCount"]) for row in rows if row["postCount"] != 1
    ]
    assert duplicated == [], duplicated


def test_connect_guarantee_states_deliver_the_right_kind_of_message() -> None:
    """状態ごとに「届く中身」が正しいこと（リンク／連携済み／診断つき案内）。"""
    rows = _caller_identity_report()["guarantee_matrix"]
    by_state: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        by_state.setdefault(str(row["state"]), []).append(row)

    # 新規（メール無し）は mcp の CONNECT-I02 文面がそのまま届く。
    for row in by_state["new_user_without_email"]:
        assert "CONNECT-I02" in str(row["postText"]), row["text"]
        assert "Slack プロフィール" in str(row["postText"]), row["text"]
    # 新規（メール有り）はリンクが届く。
    for row in by_state["new_user_with_email"]:
        assert "https://connect.newstv.co.jp/" in str(row["postText"]), row["text"]
    # 既に両方連携済みならその旨が届く（リンクは出さない）。
    for row in by_state["existing_fully_connected"]:
        assert "既に" in str(row["postText"]), row["text"]
    # 片方だけなら残りのリンクだけが届く。
    for row in by_state["existing_partially_connected"]:
        assert "省略しています" in str(row["postText"]), row["text"]
        assert "slack/oauth/start" in str(row["postText"]), row["text"]
    # mcp へ届かなかった場合は、必ず転送用の診断行つきの案内が届く（無言にしない）。
    for state in ("mcp_unreachable", "mcp_http_500", "mcp_invalid_result"):
        for row in by_state[state]:
            assert row["hasDiagnostic"] is True, (state, row["text"])
            assert "CONNECT-Z02" in str(row["postText"]), (state, row["text"])
            assert "もう一度" in str(row["postText"]), (state, row["text"])


def test_connect_guarantee_does_not_fire_on_non_connect_phrases() -> None:
    """短い連携依頼でない文言では保証経路を起動しないこと（誤爆しない）。

    「連携解除」「連携できない」「〇〇社との連携について提案書を」等で
    勝手にリンクを投げ始めたら、保証は成立しても製品として壊れる。
    """
    rows = _caller_identity_report()["guarantee_negative_matrix"]
    assert rows
    fired = [row["text"] for row in rows if row["postCount"] != 0]
    assert fired == [], f"誤爆した文言: {fired}"


def test_connect_guarantee_is_independent_of_the_broken_layers() -> None:
    """run 束縛切れ・channel 申告不一致・履歴汚染のいずれでも保証経路は影響を受けないこと。"""
    cases = _caller_identity_report()["guarantee_cases"]
    # run が束縛できずツールが block されても、投稿は既に済んでいる。
    assert cases["run_binding_lost"]["toolBlocked"] is True
    assert cases["run_binding_lost"]["postCount"] == 1
    # 二重包み（過去のテストユーザーの履歴汚染）でも同じ。
    assert cases["polluted_history_double_wrapped"]["blocked"] is False
    assert cases["polluted_history_double_wrapped"]["postCount"] == 1


def test_declared_conversation_fields_are_discarded_not_blocked() -> None:
    """(C2) モデルの `channel_id` 申告違いで落とさず、authoritative 値で続行すること。

    実測 2 件の `declared channel_id does not match the bound ingress` がこれ。
    mintCallerClaim は `_user_context` を authoritative 値で丸ごと置き換えてから
    署名するので、申告値は元々 100% 捨てられている。拒否は 1 ビットも安全を稼がず、
    純粋な失敗モードだった。
    """
    case = _caller_identity_report()["guarantee_cases"]["declared_channel_mismatch"]
    assert case["blocked"] is False
    # 申告値は 1 文字も残らない（捨てて上書き）。
    assert case["declaredValueSurvived"] is False
    # 捨てたことは必ず観測できる（黙って捨てない）。
    assert case["discardedLogged"] is True


def test_connect_guarantee_never_dies_silently_when_slack_rejects() -> None:
    """Slack への投稿自体が失敗したときも、理由を必ず 1 行残すこと（無言終了ゼロ）。"""
    cases = _caller_identity_report()["guarantee_cases"]
    for name in ("slack_post_fails", "slack_open_fails"):
        assert cases[name]["postCount"] == 0, name
        assert cases[name]["postFailedLogged"] is True, name


def test_connect_guarantee_posts_once_and_layer1_stands_down() -> None:
    """同じ受信に 2 回投稿しないこと・保証経路が答えたら層1 は降りること。

    一回性の旗は「実際に配信を試みる」と決めた後にだけ立てる。手前で立てると、
    保証経路が使えない環境で層1 まで降りてしまい **誰も答えない** 穴ができる。
    """
    cases = _caller_identity_report()["guarantee_cases"]
    assert cases["once_per_inbound"]["postCount"] == 1
    assert cases["layer1_stands_down"]["guaranteeDelivered"] is True
    assert cases["layer1_stands_down"]["layer1Handled"] is False
    assert cases["layer1_stands_down"]["postCount"] == 1


def test_connect_guarantee_uses_a_canonical_slack_conversation_id() -> None:
    """DM でも mcp が要求する正準 `D…` で claim を鋳造すること。

    mcp の caller_claim は `^[CDG][A-Z0-9]{8,}$` を要求する
    （caller_claim.py:39,385）。受信側は DM を内部別名 `DM:U…` にしか解決できないため、
    conversations.open で正準 id を得てから署名する。
    チャンネルは既に正準なので open を呼ばない（余計な API を叩かない）。
    """
    cases = _caller_identity_report()["guarantee_cases"]
    dm = cases["dm_canonical_channel"]
    assert dm["toolName"] == "oauth_connect"
    assert re.fullmatch(r"[CDG][A-Z0-9]{8,}", str(dm["claimChannel"])), dm["claimChannel"]
    assert dm["postChannel"] == dm["claimChannel"]
    thread = cases["channel_thread"]
    assert thread["openCount"] == 0
    assert re.fullmatch(r"C[A-Z0-9]{8,}", str(thread["channel"])), thread["channel"]
    # スレッドで訊かれたらスレッドへ返す（会話面を移さない）。
    assert thread["threadTs"] is not None


def test_run_binding_prefers_the_newest_inbound_in_the_same_conversation() -> None:
    """(C1) 同じ会話の候補が複数でも run を落とさないこと。

    実測 9 件の `trusted Slack run identity is missing or stale` の源。従来は
    `candidates !== 1` で run を拒否し、rejectedRuns に 10 分間登録していたため、
    その run の **すべてのツール** が block された。連続してメッセージを送るだけで再現する。

    安全側は崩れない: matchesConversation は sessionKey・senderId・channel・TTL を
    すべて満たしたものだけを残すので、候補は全員「同じ人の同じ会話」。曖昧なのは
    「どのメッセージか」だけで「誰か」ではない。
    """
    report = _caller_identity_report()["bind_newest"]
    # ① 連続 2 通のあとの run でもツールが通る。
    assert report["consecutive"]["toolBlocked"] is False
    assert report["consecutive"]["rejected"] is False
    # 曖昧化したことは必ず観測できる（黙って選ばない）。
    assert report["consecutive"]["disambiguated"] is True
    assert "rule=newest_in_conversation" in str(report["consecutive"]["disambiguatedLine"])
    # ② 最新が選ばれる。
    assert report["newestWins"]["blocked"] is False
    assert report["newestWins"]["claimMessage"] == "1785206306.000006"
    # ③ 安全側: より新しい **別送信者** の受信があっても掴まない。
    assert report["otherSender"]["blocked"] is False
    assert report["otherSender"]["claimUser"] == "U09CX1CCBLN"
    assert report["otherSender"]["claimMessage"] == "1785206303.000003"


def test_every_registered_hook_reports_its_first_invocation() -> None:
    """本番でどのフックが実際に呼ばれるかを、ログだけで列挙できること。

    2 便かけて「層1 が発火しているのか」を判別できなかった原因は、発火の事実を
    出す行が TRACE 依存で、その TRACE が entrypoint の env allowlist に落とされて
    いたこと（openclaw-entrypoint.mjs の DIAGNOSTIC_ENV で解決）。
    以後、register の 1 行と各フック初回の 1 行は TRACE と無関係に必ず出す。
    バナー（登録要求）と first_fired（実際に呼ばれた）の差分が、そのまま
    「登録はしたが本番では呼ばれないフック」の一覧になる。
    """
    report = _caller_identity_report()["hook_observability"]
    assert report["registeredHooks"] == [
        "inbound_claim",
        "message_received",
        "before_agent_reply",
        "before_model_resolve",
        "before_tool_call",
        "before_agent_finalize",
        "reply_payload_sending",
        "agent_end",
    ]
    banner = str(report["bannerLine"])
    assert "registered hooks=[" in banner
    for hook in report["registeredHooks"]:
        assert hook in banner, hook
    # 呼んだフックだけが first_fired を出す。
    assert report["firstFired"] == [
        "message_received",
        "before_model_resolve",
        "before_tool_call",
    ]
    # 初回だけ（2 回目以降は 1 行も増えない＝会話ごとの騒音にならない）。
    assert report["secondPassConsoleDelta"] == 0


def test_plugin_diagnostics_never_touch_stdout() -> None:
    """プラグインの診断行は必ず stderr へ出すこと（stdout はデータ面）。

    このプラグインは `node -e` で読み込まれ、同じプロセスが
    `process.stdout.write(JSON.stringify(...))` の結果を返すことがある
    （tests/test_mcp_gateway_caller_claim.py の 3 本のハーネス）。
    診断行を stdout に混ぜるとその JSON が壊れる。実際、register バナーを
    console.info（= stdout）で出した瞬間に 3 本が赤くなった（2026-09-04 実測）。

    CloudWatch は stdout/stderr を同じロググループへ入れるので、到達性は変わらない。
    """
    levels = _caller_identity_report()["hook_observability"]["consoleLevels"]
    # console.warn / console.error だけが stderr。log / info は stdout。
    assert levels == ["warn"], levels
