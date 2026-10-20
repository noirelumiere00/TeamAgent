from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[2] / "infra/docker/openclaw-entrypoint.mjs"


@pytest.mark.parametrize(
    ("scenario", "catchup_env", "active"),
    [
        (scenario, {"DM_CATCHUP_ENABLED": "1", "DM_CATCHUP_ALLOWED_USERS": "*"}, True)
        for scenario in ["fresh", "existing", "directory_symlink", "journal_symlink"]
    ]
    + [
        ("fresh", {"DM_CATCHUP_ENABLED": "1", "DM_CATCHUP_ALLOWED_USERS": users}, True)
        for users in [
            "U01234567",
            "U01234567,U76543210",
            ",".join(f"U{index:08d}" for index in range(100)),
        ]
    ]
    + [
        (scenario, env, False)
        for scenario in ["fresh", "existing", "directory_symlink", "journal_symlink"]
        for env in [
            {},
            {"DM_CATCHUP_ENABLED": "0", "DM_CATCHUP_ALLOWED_USERS": "*"},
            {"DM_CATCHUP_ENABLED": "true", "DM_CATCHUP_ALLOWED_USERS": "*"},
            {"DM_CATCHUP_ENABLED": "1"},
            {"DM_CATCHUP_ENABLED": "1", "DM_CATCHUP_ALLOWED_USERS": ""},
            {"DM_CATCHUP_ENABLED": "1", "DM_CATCHUP_ALLOWED_USERS": "bad-user"},
            {"DM_CATCHUP_ENABLED": "1", "DM_CATCHUP_ALLOWED_USERS": "*,U01234567"},
            {"DM_CATCHUP_ENABLED": "1", "DM_CATCHUP_ALLOWED_USERS": " U01234567"},
            {"DM_CATCHUP_ENABLED": "1", "DM_CATCHUP_ALLOWED_USERS": "U01234567,U01234567"},
        ]
    ]
    + [
        ("fresh", {"DM_CATCHUP_ENABLED": "1", "DM_CATCHUP_ALLOWED_USERS": users}, False)
        for users in [",".join(f"U{index:08d}" for index in range(101)), "U" + "0" * 2048]
    ],
)
def test_entrypoint_prepares_private_catchup_state_and_rejects_symlinks(
    tmp_path: Path, scenario: str, catchup_env: dict[str, str], active: bool
) -> None:
    script = r"""
import assert from "node:assert/strict";
import * as fs from "node:fs/promises";
import { constants } from "node:fs";
import { join, resolve } from "node:path";
import vm from "node:vm";
const [entrypoint, root, scenario, catchupEnvJson, activeJson] = process.argv.slice(1);
const active = JSON.parse(activeJson), touched = [];
const canonicalRoot = "/tmp/teamagent-openclaw";
const local = path => join(root, path.slice(canonicalRoot.length));
const catchupDir = local(`${canonicalRoot}/state/slack-catchup`);
const journalPath = join(catchupDir, "journal.json");
const sqlitePath = local(`${canonicalRoot}/state/state/openclaw.sqlite`);
await fs.mkdir(join(root, "state/state"), { recursive: true });
await fs.writeFile(sqlitePath, "existing sqlite");
const seedPath = join(root, "sqlite-seed");
await fs.writeFile(seedPath, "replacement sqlite");
const outside = join(root, "outside");
await fs.mkdir(outside);
const outsideFile = join(outside, "untouched.json");
await fs.writeFile(outsideFile, "untouched");
if (scenario === "directory_symlink") {
  await fs.symlink(outside, catchupDir);
} else if (scenario === "journal_symlink") {
  await fs.mkdir(catchupDir);
  await fs.symlink(outsideFile, journalPath);
} else if (scenario === "existing") {
  await fs.mkdir(catchupDir, { mode: 0o755 });
  await fs.writeFile(journalPath, "existing journal", { mode: 0o600 });
}
const source = await fs.readFile(entrypoint, "utf8");
const start = source.indexOf("async function rejectSymlink(");
const end = source.indexOf("\nfunction normalizeCommand(", start);
assert.ok(start >= 0 && end > start, "exercise the real runtime preparation functions");
const stop = new Error("filesystem preparation complete");
const secrets = ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "OPENCLAW_GATEWAY_TOKEN",
  "TEAMAGENT_MCP_BEARER", "TEAMAGENT_CALLER_CLAIM_SECRET"];
const context = vm.createContext({
  Buffer, constants, join, resolve, REQUIRED_SECRETS: secrets,
  process: { env: {...Object.fromEntries(secrets.map(name => [name, name.repeat(3)])),
    ...JSON.parse(catchupEnvJson)} },
  parseSlackDmAccess: () => ({}), parseSlackTeamId: () => {},
  realpath: async path => path,
  lstat: path => {touched.push(path); return fs.lstat(local(path));},
  mkdir: (path, options) => {touched.push(path); return fs.mkdir(local(path), options);},
  chmod: (path, mode) => {touched.push(path); return fs.chmod(local(path), mode);},
  copyFile: (_source, target, flags) => fs.copyFile(seedPath, local(target), flags),
  readFile: async path => {
    assert.equal(path, "/opt/teamagent/openclaw.template.json");
    throw stop;
  },
});
vm.runInContext(source.slice(start, end), context);
const preparation = vm.runInContext("prepareRuntime()", context);
if (!active) {
  await assert.rejects(preparation, error => error === stop);
  assert.equal(touched.some(path => path.includes("/slack-catchup")), false,
    "disabled/empty/invalid canary scope must not inspect or modify catchup state");
  if (scenario === "fresh") await assert.rejects(fs.lstat(catchupDir), {code: "ENOENT"});
  if (scenario === "existing") assert.equal(await fs.readFile(journalPath, "utf8"), "existing journal");
} else if (scenario.endsWith("_symlink")) {
  const rejectedPath = scenario === "directory_symlink"
    ? `${canonicalRoot}/state/slack-catchup` : `${canonicalRoot}/state/slack-catchup/journal.json`;
  await assert.rejects(preparation, error =>
    error.message === `refusing symlink runtime path: ${rejectedPath}`);
} else {
  await assert.rejects(preparation, error => error === stop);
  assert.equal((await fs.stat(catchupDir)).mode & 0o777, 0o700);
  if (scenario === "existing") assert.equal(await fs.readFile(journalPath, "utf8"), "existing journal");
  else await assert.rejects(fs.lstat(journalPath), { code: "ENOENT" });
}
assert.equal(await fs.readFile(outsideFile, "utf8"), "untouched");
assert.equal(await fs.readFile(sqlitePath, "utf8"), "existing sqlite");
"""
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            str(ENTRYPOINT),
            str(tmp_path),
            scenario,
            json.dumps(catchup_env),
            json.dumps(active),
        ],
        env={"PATH": os.environ["PATH"]},
        check=False,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
