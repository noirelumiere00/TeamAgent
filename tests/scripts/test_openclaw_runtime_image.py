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


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        text=True,
        capture_output=True,
    )


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


def _gateway_lifecycle_contract(image: str) -> dict[str, Any]:
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
    for assignment in PLACEHOLDER_ENV:
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
        secret_values = [assignment.split("=", 1)[1] for assignment in PLACEHOLDER_ENV[:4]]
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
        }
    finally:
        _run(["docker", "rm", "-f", container_id], check=False)


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
    assert process_contract["prune"]["browser"]["reachableRegistrationChunks"] == 0
    assert process_contract["prune"]["browser"]["residualUnreachableBrowserCandidates"] == 0
    assert process_contract["prune"]["packages"]["residualForbidden"] == 0
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
    assert browser_bridge_contract["failClosed"] is True
    assert "public surface access blocked" in browser_bridge_contract["error"]
    assert "no bundled plugin manifest found for browser" in browser_bridge_contract["error"]
    gateway_lifecycle = _gateway_lifecycle_contract(image)

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
            "browserBridgeFacadeFailClosed": True,
            "browserReachabilityReport": True,
            "jitiUnavailable": True,
            "developmentPayloadAbsent": True,
            "gatewayIsPid1": gateway_lifecycle["pid1Children"] == "",
            "gatewayReady": gateway_lifecycle["ready"],
            "gatewaySigtermExitZero": gateway_lifecycle["exitCode"] == 0,
            "gatewayRuntimeSecretLeakAbsent": (gateway_lifecycle["runtimeSecretLeak"] is False),
        },
        "process": process_contract,
        "browserBridge": browser_bridge_contract,
        "gatewayLifecycle": gateway_lifecycle,
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
