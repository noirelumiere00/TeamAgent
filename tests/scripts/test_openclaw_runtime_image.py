"""Executable contract test for a built OpenClaw linux/arm64 image.

The release helper invokes this file against the exact image digest.  Pytest
also exposes it when OPENCLAW_RUNTIME_TEST_IMAGE is explicitly set; otherwise
the image test is skipped instead of silently testing source text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_OPERATION_SMOKE = ROOT / "infra/openclaw/plugin-operation-smoke.mjs"
EXPECTED_CMD = [
    "/opt/teamagent/gateway-runtime.mjs",
    "gateway",
    "--bind",
    "loopback",
    "--port",
    "18789",
]
EXPECTED_ENTRYPOINT = ["/nodejs/bin/node", "/opt/teamagent/entrypoint.mjs"]
PLACEHOLDER_ENV = [
    "SLACK_BOT_TOKEN=xoxb-offline-contract",
    "SLACK_APP_TOKEN=xapp-offline-contract",
    "OPENCLAW_GATEWAY_TOKEN=offline-gateway-contract",
    "TEAMAGENT_MCP_BEARER=offline-mcp-contract",
    "SLACK_DM_ALLOWLIST=*",
    "AWS_EC2_METADATA_DISABLED=true",
]
GATEWAY_LAUNCHER = r"""
const fs = require("node:fs");
const configPath = process.env.OPENCLAW_CONFIG_PATH;
const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
config.channels.slack.enabled = false;
fs.writeFileSync(
  configPath,
  JSON.stringify(config, null, 2) + "\n",
  {mode: 0o600}
);
process.execve(
  process.execPath,
  [
    process.execPath,
    "/opt/teamagent/gateway-runtime.mjs",
    "gateway",
    "--bind",
    "loopback",
    "--port",
    "18789"
  ],
  process.env
);
"""
CONTROL_UI_HTTP_PROBE = r"""
const crypto = require("node:crypto");
const fs = require("node:fs");

(async () => {
const report = JSON.parse(
  fs.readFileSync("/opt/teamagent/runtime-prune-report.json", "utf8")
);
const browser = report.browser;
const assetPrefix = "/app/dist/control-ui";
const base = new URL("http://127.0.0.1:18789/");
const sha256 = value =>
  crypto.createHash("sha256").update(value).digest("hex");
const toModuleHttpPath = candidate => {
  if (!candidate.startsWith(`${assetPrefix}/`)) {
    throw new Error(`Control UI asset escapes its root: ${candidate}`);
  }
  return candidate.slice(assetPrefix.length);
};

if (
  !Array.isArray(browser.controlUiReachableModuleAssets) ||
  browser.controlUiReachableModuleAssets.length === 0 ||
  browser.controlUiReachableModuleAssets.length !==
    browser.controlUiReachableModuleCount ||
  !Array.isArray(browser.controlUiServedAssets) ||
  browser.controlUiServedAssets.length === 0 ||
  browser.controlUiServedAssets.length !== browser.controlUiServedAssetCount
) {
  throw new Error("invalid Control UI asset inventory");
}
const expectedRoots = browser.controlUiGraphRoots.map(toModuleHttpPath).sort();
const rootResponse = await fetch(base);
const rootBody = Buffer.from(await rootResponse.arrayBuffer());
if (rootResponse.status !== 200) {
  throw new Error(`Control UI root returned ${rootResponse.status}`);
}
const html = rootBody.toString("utf8");
const moduleRoots = [];
for (const tag of html.match(/<script\b[^>]*>/giu) || []) {
  if (!/\btype\s*=\s*["']module["']/iu.test(tag)) continue;
  const source = tag.match(/\bsrc\s*=\s*["']([^"']+)["']/iu)?.[1];
  if (!source) throw new Error("Control UI module script has no src");
  moduleRoots.push(new URL(source, base).pathname);
}
moduleRoots.sort();
if (JSON.stringify(moduleRoots) !== JSON.stringify(expectedRoots)) {
  throw new Error(
    `served Control UI roots differ from prune report: ${JSON.stringify({
      expectedRoots,
      moduleRoots
    })}`
  );
}

const failures = [];
const served = [];
const diskAssets = [];
const secretValues = [
  process.env.SLACK_BOT_TOKEN,
  process.env.SLACK_APP_TOKEN,
  process.env.OPENCLAW_GATEWAY_TOKEN,
  process.env.TEAMAGENT_MCP_BEARER
].filter(Boolean);
const expectedHttpPaths = new Set();
for (const asset of browser.controlUiServedAssets) {
  if (
    !asset.path.startsWith(`${assetPrefix}/`) ||
    !asset.httpPath.startsWith("/") ||
    expectedHttpPaths.has(asset.httpPath)
  ) {
    throw new Error(`invalid or duplicate Control UI HTTP path: ${asset.httpPath}`);
  }
  expectedHttpPaths.add(asset.httpPath);
  const diskBody = fs.readFileSync(asset.path);
  const diskSha256 = sha256(diskBody);
  const diskLeaksSecret = secretValues.some(secret =>
    diskBody.includes(Buffer.from(secret))
  );
  if (
    diskSha256 !== asset.sha256 ||
    diskBody.length !== asset.size ||
    diskLeaksSecret
  ) {
    failures.push({
      path: asset.path,
      stage: "on-disk",
      expectedSha256: asset.sha256,
      actualSha256: diskSha256,
      expectedSize: asset.size,
      actualSize: diskBody.length,
      leaksSecret: diskLeaksSecret
    });
  }
  if (
    !/^[0-9a-f]{64}$/u.test(asset.servedSha256) ||
    !Number.isSafeInteger(asset.servedSize) ||
    asset.servedSize < 0 ||
    !["identity", 'insert data-openclaw-terminal-enabled="false" after <html']
      .includes(asset.httpTransform)
  ) {
    throw new Error(`invalid Control UI HTTP transform: ${asset.httpPath}`);
  }
  diskAssets.push({path: asset.path, sha256: diskSha256});
  const httpPath = asset.httpPath;
  const response = await fetch(new URL(httpPath, base));
  const body = Buffer.from(await response.arrayBuffer());
  const actualSha256 = sha256(body);
  const leaksSecret = secretValues.some(secret =>
    body.includes(Buffer.from(secret))
  );
  if (
    response.status !== 200 ||
    actualSha256 !== asset.servedSha256 ||
    body.length !== asset.servedSize ||
    leaksSecret
  ) {
    failures.push({
      path: httpPath,
      status: response.status,
      expectedSha256: asset.servedSha256,
      actualSha256,
      expectedSize: asset.servedSize,
      actualSize: body.length,
      leaksSecret
    });
  }
  served.push({path: httpPath, sha256: actualSha256});
}

const dynamicConfigPath = "/control-ui-config.json";
const unauthenticatedConfig = await fetch(new URL(dynamicConfigPath, base));
const unauthenticatedConfigBody = Buffer.from(
  await unauthenticatedConfig.arrayBuffer()
);
const unauthenticatedConfigJson = JSON.parse(
  unauthenticatedConfigBody.toString("utf8")
);
if (
  unauthenticatedConfig.status !== 401 ||
  JSON.stringify(unauthenticatedConfigJson) !==
    JSON.stringify({error: {message: "Unauthorized", type: "unauthorized"}}) ||
  secretValues.some(secret =>
    unauthenticatedConfigBody.includes(Buffer.from(secret))
  )
) {
  throw new Error("Control UI bootstrap config did not fail closed without auth");
}
const authenticatedConfig = await fetch(new URL(dynamicConfigPath, base), {
  headers: {Authorization: `Bearer ${process.env.OPENCLAW_GATEWAY_TOKEN}`}
});
const authenticatedConfigBody = Buffer.from(
  await authenticatedConfig.arrayBuffer()
);
const authenticatedConfigJson = JSON.parse(
  authenticatedConfigBody.toString("utf8")
);
const expectedAuthenticatedConfig = {
  basePath: "",
  assistantName: "TeamAgent",
  assistantAvatar: "🧭",
  assistantAvatarSource: null,
  assistantAvatarStatus: "none",
  assistantAvatarReason: "missing",
  assistantAgentId: "teamagent",
  serverVersion: "2026.7.1",
  localMediaPreviewRoots: [
    "/tmp/openclaw",
    "/tmp/teamagent-openclaw/state/media",
    "/tmp/teamagent-openclaw/state/canvas",
    "/tmp/teamagent-openclaw/state/workspace",
    "/tmp/teamagent-openclaw/state/sandboxes"
  ],
  embedSandbox: "scripts",
  allowExternalEmbedUrls: false,
  terminalEnabled: false
};
if (
  authenticatedConfig.status !== 200 ||
  JSON.stringify(authenticatedConfigJson) !==
    JSON.stringify(expectedAuthenticatedConfig) ||
  secretValues.some(secret =>
    authenticatedConfigBody.includes(Buffer.from(secret))
  )
) {
  throw new Error("Control UI authenticated bootstrap config contract failed");
}

const staticReferences = [
  ...html.matchAll(/\b(?:href|src)\s*=\s*["']([^"'#]+)["']/giu)
]
  .map(match => match[1])
  .filter(reference => !/^(?:[a-z]+:)?\/\//iu.test(reference));
const staticFailures = [];
for (const reference of [...new Set(staticReferences)].sort()) {
  const response = await fetch(new URL(reference, base));
  if (response.status !== 200) {
    staticFailures.push({reference, status: response.status});
  }
}
if (secretValues.some(secret => rootBody.includes(Buffer.from(secret)))) {
  failures.push({path: "/", status: 200, leaksSecret: true});
}
if (failures.length > 0 || staticFailures.length > 0) {
  throw new Error(
    `Control UI HTTP closure failed: ${JSON.stringify({
      failures,
      staticFailures
    })}`
  );
}

served.sort((left, right) => left.path.localeCompare(right.path));
diskAssets.sort((left, right) => left.path.localeCompare(right.path));
process.stdout.write(JSON.stringify({
  rootStatus: rootResponse.status,
  rootSha256: sha256(rootBody),
  moduleRoots,
  reachableModuleCount: browser.controlUiReachableModuleCount,
  reachableModuleAssetCount: browser.controlUiReachableModuleAssets.length,
  expectedServedAssetCount: browser.controlUiServedAssetCount,
  servedAssetCount: served.length,
  servedAssetInventorySha256: sha256(
    Buffer.from(JSON.stringify(served))
  ),
  onDiskAssetInventorySha256: sha256(
    Buffer.from(JSON.stringify(diskAssets))
  ),
  staticReferenceCount: new Set(staticReferences).size,
  dynamicRegistrations: [{
    path: dynamicConfigPath,
    unauthenticatedStatus: unauthenticatedConfig.status,
    authenticatedStatus: authenticatedConfig.status,
    authenticatedSha256: sha256(authenticatedConfigBody),
    authenticatedSize: authenticatedConfigBody.length,
    terminalEnabled: authenticatedConfigJson.terminalEnabled
  }],
  missingOrMismatchedAssets: 0,
  runtimeSecretLeak: false
}) + "\n");
})().catch(error => {
  console.error(error instanceof Error ? error.stack : String(error));
  process.exit(1);
});
"""


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        text=True,
        capture_output=True,
    )


def _runtime_env(slack_dm_allowlist: str) -> list[str]:
    return [
        (
            f"SLACK_DM_ALLOWLIST={slack_dm_allowlist}"
            if assignment.startswith("SLACK_DM_ALLOWLIST=")
            else assignment
        )
        for assignment in PLACEHOLDER_ENV
    ]


def _isolated_run_args(image: str) -> list[str]:
    args = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/arm64",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=128m",
    ]
    for assignment in PLACEHOLDER_ENV:
        args.extend(["-e", assignment])
    args.append(image)
    return args


def _gateway_lifecycle_contract(
    image: str,
    *,
    slack_dm_allowlist: str = "*",
    expected_dm_policy: str = "open",
    expected_allow_from: list[str] | None = None,
    verify_control_ui: bool = True,
) -> dict[str, Any]:
    if expected_allow_from is None:
        expected_allow_from = ["*"]
    runtime_env = _runtime_env(slack_dm_allowlist)
    run_args = [
        "docker",
        "run",
        "-d",
        "--platform",
        "linux/arm64",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=512m",
    ]
    for assignment in runtime_env:
        run_args.extend(["-e", assignment])
    run_args.extend(
        [
            image,
            "/nodejs/bin/node",
            "-e",
            GATEWAY_LAUNCHER,
        ]
    )
    container_id = _run(run_args).stdout.strip()
    assert container_id

    try:
        container = json.loads(_run(["docker", "inspect", container_id]).stdout)[0]
        host = container["HostConfig"]
        tmpfs = host["Tmpfs"]["/tmp"]
        assert host["ReadonlyRootfs"] is True
        assert "ALL" in host["CapDrop"]
        assert "no-new-privileges" in host["SecurityOpt"]
        assert host["NetworkMode"] == "none"
        assert "noexec" in tmpfs
        assert "nosuid" in tmpfs

        ready = False
        for _ in range(45):
            ready_probe = _run(
                [
                    "docker",
                    "exec",
                    container_id,
                    "/nodejs/bin/node",
                    "-e",
                    (
                        "fetch('http://127.0.0.1:18789/readyz')"
                        ".then(r=>process.exit(r.ok?0:1))"
                        ".catch(()=>process.exit(1))"
                    ),
                ],
                check=False,
            )
            if ready_probe.returncode == 0:
                ready = True
                break
            running = _run(
                [
                    "docker",
                    "inspect",
                    "-f",
                    "{{.State.Running}}",
                    container_id,
                ],
                check=False,
            )
            if running.returncode != 0 or running.stdout.strip() != "true":
                break
            time.sleep(1)

        startup_logs = _run(["docker", "logs", container_id], check=False)
        startup_output = startup_logs.stdout + startup_logs.stderr
        assert ready, startup_output

        dm_access_result = _run(
            [
                "docker",
                "exec",
                container_id,
                "/nodejs/bin/node",
                "-e",
                (
                    "const fs=require('node:fs');"
                    "const c=JSON.parse(fs.readFileSync("
                    "process.env.OPENCLAW_CONFIG_PATH,'utf8'));"
                    "process.stdout.write(JSON.stringify({"
                    "dmPolicy:c.channels.slack.dmPolicy,"
                    "allowFrom:c.channels.slack.allowFrom}));"
                ),
            ]
        )
        dm_access = json.loads(dm_access_result.stdout)
        assert dm_access == {
            "dmPolicy": expected_dm_policy,
            "allowFrom": expected_allow_from,
        }

        control_ui: dict[str, Any] | None = None
        if verify_control_ui:
            control_ui_result = _run(
                [
                    "docker",
                    "exec",
                    container_id,
                    "/nodejs/bin/node",
                    "-e",
                    CONTROL_UI_HTTP_PROBE,
                ],
                check=False,
            )
            assert control_ui_result.returncode == 0, (
                control_ui_result.stdout + control_ui_result.stderr
            )
            control_ui = json.loads(control_ui_result.stdout)
            assert control_ui["rootStatus"] == 200
            assert control_ui["reachableModuleAssetCount"] == control_ui["reachableModuleCount"]
            assert control_ui["servedAssetCount"] == control_ui["expectedServedAssetCount"]
            assert len(control_ui["dynamicRegistrations"]) == 1
            assert control_ui["dynamicRegistrations"][0]["path"] == ("/control-ui-config.json")
            assert control_ui["dynamicRegistrations"][0]["unauthenticatedStatus"] == 401
            assert control_ui["dynamicRegistrations"][0]["authenticatedStatus"] == 200
            assert control_ui["dynamicRegistrations"][0]["terminalEnabled"] is False
            assert len(control_ui["dynamicRegistrations"][0]["authenticatedSha256"]) == 64
            assert len(control_ui["onDiskAssetInventorySha256"]) == 64
            assert len(control_ui["servedAssetInventorySha256"]) == 64
            assert control_ui["missingOrMismatchedAssets"] == 0
            assert control_ui["runtimeSecretLeak"] is False

        children = _run(
            [
                "docker",
                "exec",
                container_id,
                "/nodejs/bin/node",
                "-e",
                (
                    "process.stdout.write("
                    "require('node:fs').readFileSync("
                    "'/proc/1/task/1/children','utf8').trim())"
                ),
            ]
        ).stdout.strip()
        assert children == ""

        stopped = _run(
            ["docker", "stop", "--time", "30", container_id],
            check=False,
        )
        assert stopped.returncode == 0, stopped.stderr

        final_inspect = json.loads(_run(["docker", "inspect", container_id]).stdout)[0]
        state = final_inspect["State"]
        final_logs = _run(["docker", "logs", container_id], check=False)
        log_output = final_logs.stdout + final_logs.stderr
        secret_values = [assignment.split("=", 1)[1] for assignment in runtime_env[:4]]
        assert not any(value in log_output for value in secret_values)
        assert not any(
            marker in log_output
            for marker in (
                "spawn npm",
                "Config observe anomaly",
                "auto-enabled plugins",
                "browser configured",
            )
        )
        assert state["ExitCode"] == 0, log_output
        assert state["OOMKilled"] is False
        assert state["Error"] == ""

        return {
            "ready": True,
            "pid1Children": children,
            "signal": "SIGTERM",
            "exitCode": state["ExitCode"],
            "oomKilled": state["OOMKilled"],
            "runtimeSecretLeak": False,
            "logSha256": hashlib.sha256(log_output.encode()).hexdigest(),
            "dmAccess": dm_access,
            "controlUi": control_ui,
        }
    finally:
        _run(["docker", "rm", "-f", container_id], check=False)


def _empty_slack_dm_allowlist_contract(image: str) -> dict[str, Any]:
    args = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/arm64",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
    ]
    for assignment in _runtime_env(""):
        args.extend(["-e", assignment])
    args.extend([image, "/nodejs/bin/node", "-e", "process.exit(0)"])
    result = _run(args, check=False)
    output = result.stdout + result.stderr
    assert result.returncode == 78, output
    assert '"event":"openclaw_entrypoint_error"' in output
    assert "SLACK_DM_ALLOWLIST is required" in output
    assert '"event":"openclaw_runtime_ready"' not in output
    return {
        "exitCode": result.returncode,
        "rejectedBeforeRuntimeReady": True,
        "logSha256": hashlib.sha256(output.encode()).hexdigest(),
    }


def _plugin_operation_contract(image: str) -> dict[str, Any]:
    result = _run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/arm64",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--mount",
            (
                f"type=bind,src={PLUGIN_OPERATION_SMOKE},"
                "dst=/opt/openclaw-plugin-operation-smoke.mjs,readonly"
            ),
            "--entrypoint",
            "/nodejs/bin/node",
            image,
            "/opt/openclaw-plugin-operation-smoke.mjs",
        ]
    )
    contract = json.loads(result.stdout)
    assert contract["schemaVersion"] == 1
    assert contract["network"] == "disabled-by-container"
    assert contract["passed"] is True
    assert contract["slack"] == {
        "module": "/opt/teamagent/plugins/slack/dist/api.js",
        "operations": ["conversations.history", "chat.update"],
        "providerCallsStubbed": True,
    }
    assert contract["bedrock"] == {
        "module": "/opt/teamagent/plugins/amazon-bedrock/dist/api.js",
        "operations": [
            "ListFoundationModelsCommand",
            "ListInferenceProfilesCommand",
        ],
        "providerCallsStubbed": True,
    }
    return contract


def verify_runtime_image(image: str) -> dict[str, Any]:
    inspect = json.loads(_run(["docker", "image", "inspect", image]).stdout)[0]
    config = inspect["Config"]
    assert inspect["Architecture"] == "arm64"
    assert inspect["Os"] == "linux"
    assert config["User"] == "65532:65532"
    assert config["Entrypoint"] == EXPECTED_ENTRYPOINT
    assert config["Cmd"] == EXPECTED_CMD
    assert config["Volumes"] == {"/tmp": {}}
    forbidden_env = {
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "OPENCLAW_GATEWAY_TOKEN",
        "TEAMAGENT_MCP_BEARER",
    }
    assert not (forbidden_env & {entry.split("=", 1)[0] for entry in config.get("Env", [])})

    node_probe = r"""
const fs = require("node:fs");
const path = require("node:path");
function writeProbe(candidate) {
  try {
    fs.writeFileSync(candidate, "contract", {flag: "wx"});
    fs.rmSync(candidate);
    return {writable: true, code: null};
  } catch (error) {
    return {writable: false, code: error.code || null};
  }
}
const status = Object.fromEntries(
  fs.readFileSync("/proc/self/status", "utf8")
    .trim().split("\n")
    .map(line => line.split(/:\s+/, 2))
);
let jitiResolvable = false;
try {
  require.resolve("jiti", {paths: ["/app"]});
  jitiResolvable = true;
} catch (error) {
  if (error.code !== "MODULE_NOT_FOUND") throw error;
}
const metadata = JSON.parse(
  fs.readFileSync("/app/dist/cli-startup-metadata.json", "utf8")
);
const prune = JSON.parse(
  fs.readFileSync("/opt/teamagent/runtime-prune-report.json", "utf8")
);
const result = {
  uid: process.getuid(),
  gid: process.getgid(),
  capEff: status.CapEff,
  capBnd: status.CapBnd,
  noNewPrivs: status.NoNewPrivs,
  seccomp: status.Seccomp,
  appWrite: writeProbe("/app/.openclaw-contract"),
  optWrite: writeProbe("/opt/teamagent/.openclaw-contract"),
  tmpWrite: writeProbe("/tmp/.openclaw-contract"),
  jitiResolvable,
  browserHelpMetadata: (
    Object.hasOwn(metadata, "browserHelpText") ||
    Object.hasOwn(metadata, "browserHelpSourceSignature")
  ),
  prune
};
console.log(JSON.stringify(result));
"""
    probe_result = _run([*_isolated_run_args(image), "/nodejs/bin/node", "-e", node_probe])
    process_contract = json.loads(probe_result.stdout)
    assert process_contract["uid"] == 65532
    assert process_contract["gid"] == 65532
    assert int(process_contract["capEff"], 16) == 0
    assert int(process_contract["capBnd"], 16) == 0
    assert process_contract["noNewPrivs"] == "1"
    assert process_contract["appWrite"] == {
        "writable": False,
        "code": "EROFS",
    }
    assert process_contract["optWrite"] == {
        "writable": False,
        "code": "EROFS",
    }
    assert process_contract["tmpWrite"]["writable"] is True
    assert process_contract["jitiResolvable"] is False
    assert process_contract["browserHelpMetadata"] is False
    assert process_contract["prune"]["schemaVersion"] == 2
    assert process_contract["prune"]["browser"]["reachableRegistrationChunks"] == 0
    assert process_contract["prune"]["browser"]["residualUnreachableBrowserCandidates"] == 0
    assert process_contract["prune"]["browser"]["reachableBrowserNamedPayloadCount"] > 0
    assert process_contract["prune"]["browser"]["reachableBrowserPayloadZero"] is False
    assert process_contract["prune"]["browser"]["reachableBrowserImplementationModules"] == 0
    assert process_contract["prune"]["browser"]["browserCliCommandRegistered"] is False
    assert process_contract["prune"]["browser"]["genericOpenClawCliRetained"] is True
    assert process_contract["prune"]["browser"]["browserExecutableOrPlaywrightPresent"] is False
    assert process_contract["prune"]["browser"]["usableBrowserControlPath"] is False
    assert len(process_contract["prune"]["browser"]["retainedFailClosedFacade"]) == 1
    assert process_contract["prune"]["browser"]["controlUiMissingLocalImports"] == 0
    assert process_contract["prune"]["browser"]["controlUiReachableModuleCount"] == len(
        process_contract["prune"]["browser"]["controlUiReachableModuleAssets"]
    )
    assert process_contract["prune"]["browser"]["controlUiServedAssetCount"] == len(
        process_contract["prune"]["browser"]["controlUiServedAssets"]
    )
    assert (
        process_contract["prune"]["browser"]["controlUiServedAssetCount"]
        > (process_contract["prune"]["browser"]["controlUiReachableModuleCount"])
    )
    root_assets = [
        asset
        for asset in process_contract["prune"]["browser"]["controlUiServedAssets"]
        if asset["httpPath"] == "/"
    ]
    assert len(root_assets) == 1
    assert root_assets[0]["httpTransform"] == (
        'insert data-openclaw-terminal-enabled="false" after <html'
    )
    assert root_assets[0]["servedSize"] > root_assets[0]["size"]
    assert root_assets[0]["servedSha256"] != root_assets[0]["sha256"]
    assert all(
        len(asset["sha256"]) == 64 and len(asset["servedSha256"]) == 64 and asset["servedSize"] >= 0
        for asset in process_contract["prune"]["browser"]["controlUiServedAssets"]
    )
    control_ui_http_paths = {
        asset["httpPath"] for asset in process_contract["prune"]["browser"]["controlUiServedAssets"]
    }
    assert control_ui_http_paths >= {
        "/",
        "/sw.js",
        "/manifest.webmanifest",
        "/favicon.ico",
        "/favicon.svg",
        "/favicon-32.png",
        "/apple-touch-icon.png",
        "/provider-icons/ProviderIcon-bedrock.svg",
    }
    assert any(
        path.startswith("/assets/") and path.endswith(".css") for path in control_ui_http_paths
    )
    assert any(
        path.startswith("/provider-icons/") and path.endswith(".svg")
        for path in control_ui_http_paths
    )
    preserved_control_ui_browser_chunks = process_contract["prune"]["browser"][
        "preservedControlUiBrowserChunks"
    ]
    assert preserved_control_ui_browser_chunks
    assert all(
        candidate["path"].startswith("/app/dist/control-ui/")
        and candidate["implementationSignals"] == []
        for candidate in preserved_control_ui_browser_chunks
    )
    assert process_contract["prune"]["packages"]["residualForbidden"] == 0
    assert process_contract["prune"]["packages"]["closureComputedBeforeMetadataRewrite"] is True
    assert process_contract["prune"]["packages"]["prePruneProductionClosure"]
    assert (
        process_contract["prune"]["packages"]["jitiExtensionSourceTransformFacade"][
            "sourceTransformLoaderFailClosed"
        ]
        is True
    )
    assert (
        process_contract["prune"]["packages"]["typeScriptCodeModeCompilerFacade"][
            "compilerLoaderFailClosed"
        ]
        is True
    )
    assert process_contract["prune"]["packages"]["typeScriptCodeModeCompilerFacade"][
        "advertisedLanguages"
    ] == ["javascript"]
    assert (
        process_contract["prune"]["pluginOperations"]["closureComputedBeforeMetadataRewrite"]
        is True
    )
    assert process_contract["prune"]["pluginOperations"]["postPruneClosureExactMatch"] is True
    assert process_contract["prune"]["pluginOperations"]["unresolvedImports"] == []
    assert process_contract["prune"]["pluginOperations"]["unresolvedComputedImports"] == []
    assert process_contract["prune"]["developmentPayload"]["residualPathCount"] == 0

    missing_secrets = _run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/arm64",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            image,
        ],
        check=False,
    )
    assert missing_secrets.returncode == 78

    exit_42 = _run(
        [
            *_isolated_run_args(image),
            "/nodejs/bin/node",
            "-e",
            "process.exit(42)",
        ],
        check=False,
    )
    assert exit_42.returncode == 42

    browser_help = _run(
        [
            *_isolated_run_args(image),
            "/app/openclaw.mjs",
            "browser",
            "--help",
        ],
        check=False,
    )
    browser_output = f"{browser_help.stdout}\n{browser_help.stderr}"
    assert browser_help.returncode != 0
    assert "Manage OpenClaw's dedicated browser" not in browser_output
    assert "Playwright" not in browser_output

    generic_cli_help = _run(
        [
            *_isolated_run_args(image),
            "/app/openclaw.mjs",
            "--help",
        ],
        check=False,
    )
    generic_cli_output = f"{generic_cli_help.stdout}\n{generic_cli_help.stderr}"
    assert generic_cli_help.returncode == 0, generic_cli_output
    assert "OpenClaw" in generic_cli_output
    assert "Manage OpenClaw's dedicated browser" not in generic_cli_output

    browser_bridge_probe = r"""
import fs from "node:fs";
import { pathToFileURL } from "node:url";

const report = JSON.parse(
  fs.readFileSync("/opt/teamagent/runtime-prune-report.json", "utf8")
);
const candidates = report.browser.sharedReachableChunks.filter(
  candidate => candidate.includes("/browser-bridges-")
);
if (candidates.length !== 1) {
  throw new Error(`expected one shared browser bridge chunk, found ${candidates.length}`);
}
const sharedPath = candidates[0];
const source = fs.readFileSync(sharedPath, "utf8");
const alias = source.match(
  /startBrowserBridgeServer as ([A-Za-z_$][A-Za-z0-9_$]*)/u
)?.[1];
if (!alias) throw new Error("startBrowserBridgeServer export alias is missing");
const implementationSignals = {
  browserRegistration: (
    source.includes("//#region extensions/browser/") ||
    source.includes("function registerBrowserPlugin(") ||
    source.includes("registerBrowserCli(program") ||
    source.includes("createBrowserPluginService(")
  ),
  playwright: /playwright|pw-ai/iu.test(source),
  chromeMcp: /chrome-mcp/iu.test(source),
  cdpControl: /cdp-target|cdp\.helpers|CDPSession/iu.test(source)
};
const genericChildProcessPrimitives = (
  source.includes('from "node:child_process"') &&
  /\bspawn\s*\(/u.test(source)
);
const namespace = await import(pathToFileURL(sharedPath).href);
if (typeof namespace[alias] !== "function") {
  throw new Error("startBrowserBridgeServer export is not callable");
}
let result;
try {
  await namespace[alias]({});
  result = { failClosed: false, error: null };
} catch (error) {
  result = {
    failClosed: true,
    error: error instanceof Error ? error.message : String(error)
  };
}
fs.writeFileSync(1, JSON.stringify({
  sharedPath,
  exportAlias: alias,
  implementationSignals,
  genericChildProcessPrimitives,
  ...result
}) + "\n");
"""
    browser_bridge_result = _run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/arm64",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--entrypoint",
            "/nodejs/bin/node",
            image,
            "--input-type=module",
            "-e",
            browser_bridge_probe,
        ]
    )
    browser_bridge_contract = json.loads(browser_bridge_result.stdout)
    assert not any(browser_bridge_contract["implementationSignals"].values())
    assert browser_bridge_contract["genericChildProcessPrimitives"] is True
    assert browser_bridge_contract["failClosed"] is True
    assert "public surface access blocked" in browser_bridge_contract["error"]
    assert "no bundled plugin manifest found for browser" in browser_bridge_contract["error"]
    plugin_operation_contract = _plugin_operation_contract(image)
    gateway_lifecycle = _gateway_lifecycle_contract(image)
    exact_user_gateway_lifecycle = _gateway_lifecycle_contract(
        image,
        slack_dm_allowlist="U09CX1CCBLN,U0123456789",
        expected_dm_policy="allowlist",
        expected_allow_from=["U09CX1CCBLN", "U0123456789"],
        verify_control_ui=False,
    )
    empty_slack_dm_allowlist = _empty_slack_dm_allowlist_contract(image)

    return {
        "schemaVersion": 1,
        "image": image,
        "imageId": inspect["Id"],
        "platform": "linux/arm64",
        "checks": {
            "canonicalEntrypointAndCmd": True,
            "nonrootUidGid": True,
            "readOnlyAppAndOpt": True,
            "writableTmp": True,
            "localDockerCapDropAll": True,
            "localDockerNoNewPrivileges": True,
            "requiredSecretsFailClosed": True,
            "childExitCodePropagation": True,
            "browserCliUnavailable": True,
            "genericOpenClawCliRetained": True,
            "browserBridgeFacadeFailClosed": True,
            "browserNamedSharedPayloadHonestlyReported": True,
            "browserReachabilityReport": True,
            "jitiUnavailable": True,
            "developmentPayloadAbsent": True,
            "pluginOperationModulesLoadWithStubbedProviders": (
                plugin_operation_contract["passed"] is True
            ),
            "gatewayIsPid1": gateway_lifecycle["pid1Children"] == "",
            "gatewayReady": gateway_lifecycle["ready"],
            "gatewaySigtermExitZero": gateway_lifecycle["exitCode"] == 0,
            "gatewayRuntimeSecretLeakAbsent": (gateway_lifecycle["runtimeSecretLeak"] is False),
            "slackDmWildcardOpen": gateway_lifecycle["dmAccess"]
            == {"dmPolicy": "open", "allowFrom": ["*"]},
            "slackDmExactUserAllowlist": (
                exact_user_gateway_lifecycle["ready"] is True
                and exact_user_gateway_lifecycle["dmAccess"]
                == {
                    "dmPolicy": "allowlist",
                    "allowFrom": ["U09CX1CCBLN", "U0123456789"],
                }
            ),
            "slackDmEmptyFailsClosed": (
                empty_slack_dm_allowlist["exitCode"] == 78
                and empty_slack_dm_allowlist["rejectedBeforeRuntimeReady"] is True
            ),
            "controlUiAssetClosureServed": (
                gateway_lifecycle["controlUi"]["missingOrMismatchedAssets"] == 0
            ),
            "controlUiDynamicRegistrationAuthenticatedAndHashed": (
                gateway_lifecycle["controlUi"]["dynamicRegistrations"][0]["authenticatedStatus"]
                == 200
            ),
            "controlUiRuntimeSecretLeakAbsent": (
                gateway_lifecycle["controlUi"]["runtimeSecretLeak"] is False
            ),
        },
        "process": process_contract,
        "browserBridge": browser_bridge_contract,
        "pluginOperations": plugin_operation_contract,
        "gatewayLifecycle": gateway_lifecycle,
        "exactUserGatewayLifecycle": exact_user_gateway_lifecycle,
        "emptySlackDmAllowlist": empty_slack_dm_allowlist,
    }


def test_runtime_image_contract_from_environment(tmp_path: Path) -> None:
    import pytest

    image = os.environ.get("OPENCLAW_RUNTIME_TEST_IMAGE")
    if not image:
        pytest.skip("set OPENCLAW_RUNTIME_TEST_IMAGE to run the built-image contract")
    report = verify_runtime_image(image)
    (tmp_path / "actual-image-contract.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = verify_runtime_image(args.image)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
