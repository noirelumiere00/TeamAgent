import { constants } from "node:fs";
import {
  chmod,
  copyFile,
  lstat,
  mkdir,
  readFile,
  realpath,
  writeFile,
} from "node:fs/promises";
import { spawn } from "node:child_process";
import { constants as osConstants } from "node:os";
import { isAbsolute, join, resolve } from "node:path";

const REQUIRED_SECRETS = [
  "SLACK_BOT_TOKEN",
  "SLACK_APP_TOKEN",
  "OPENCLAW_GATEWAY_TOKEN",
  "TEAMAGENT_MCP_BEARER",
];
const REQUIRED_PLUGINS = new Map([
  ["slack", ["/opt/teamagent/plugins/slack", "@openclaw/slack"]],
  [
    "amazon-bedrock",
    ["/opt/teamagent/plugins/amazon-bedrock", "@openclaw/amazon-bedrock-provider"],
  ],
]);
const OPENCLAW_VERSION = "2026.7.1";
const FIXED_PATH = "/nodejs/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin";
const PASSTHROUGH_ENV = [
  // ECS task-role credentials and container/task metadata.
  "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
  "AWS_CONTAINER_CREDENTIALS_FULL_URI",
  "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
  "AWS_EC2_METADATA_DISABLED",
  "AWS_EXECUTION_ENV",
  "ECS_AGENT_URI",
  "ECS_CONTAINER_METADATA_URI",
  "ECS_CONTAINER_METADATA_URI_V4",
  // Explicit trust-store controls.
  "AWS_CA_BUNDLE",
  "NODE_EXTRA_CA_CERTS",
  "SSL_CERT_DIR",
  "SSL_CERT_FILE",
  // Explicit proxy controls. NODE_OPTIONS is deliberately not accepted.
  "ALL_PROXY",
  "HTTPS_PROXY",
  "HTTP_PROXY",
  "NO_PROXY",
  "NODE_USE_ENV_PROXY",
  "all_proxy",
  "https_proxy",
  "http_proxy",
  "no_proxy",
];

function fail(message) {
  process.stderr.write(
    `${JSON.stringify({ event: "openclaw_entrypoint_error", level: "error", message })}\n`,
  );
  process.exit(78);
}

function parseAllowlist(value) {
  if (value === undefined || value.trim() === "") return null;
  const entries = value
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
  if (entries.length === 0 || entries.length > 100) {
    throw new Error("SLACK_DM_ALLOWLIST must contain between 1 and 100 entries");
  }
  for (const entry of entries) {
    if (entry !== "*" && !/^[UW][A-Z0-9]{8,}$/.test(entry)) {
      throw new Error("SLACK_DM_ALLOWLIST contains an invalid Slack member ID");
    }
  }
  return [...new Set(entries)];
}

function assertConfig(config) {
  const slack = config?.channels?.slack;
  const allowFrom = slack?.allowFrom;
  if (!slack || !Array.isArray(allowFrom) || allowFrom.length === 0) {
    throw new Error("channels.slack.allowFrom must be a non-empty array");
  }
  if (slack.dmPolicy === "open" && !allowFrom.includes("*")) {
    throw new Error('channels.slack.dmPolicy="open" requires allowFrom=["*"]');
  }

  const allowed = new Set(config?.plugins?.allow ?? []);
  const paths = new Set(config?.plugins?.load?.paths ?? []);
  for (const [id, [path]] of REQUIRED_PLUGINS) {
    if (!allowed.has(id) || !paths.has(path) || config?.plugins?.entries?.[id]?.enabled !== true) {
      throw new Error(`required plugin is not pinned and enabled: ${id}`);
    }
  }
}

function copyDefined(source, target, names) {
  for (const name of names) {
    if (source[name] !== undefined) target[name] = source[name];
  }
}

function buildChildEnvironment({
  runtimeRoot,
  stateDir,
  workspaceDir,
  homeDir,
  cacheDir,
  configPath,
}) {
  const region = process.env.AWS_REGION || "ap-northeast-1";
  if (!/^[a-z0-9]+(?:-[a-z0-9]+){2,4}$/.test(region)) {
    throw new Error("AWS_REGION has an invalid format");
  }

  const childEnv = {
    PATH: FIXED_PATH,
    NODE_ENV: "production",
    HOME: homeDir,
    TMPDIR: "/tmp",
    XDG_CACHE_HOME: cacheDir,
    NODE_COMPILE_CACHE: join(runtimeRoot, "node-compile-cache"),
    OPENCLAW_RUNTIME_DIR: runtimeRoot,
    OPENCLAW_STATE_DIR: stateDir,
    OPENCLAW_WORKSPACE_DIR: workspaceDir,
    OPENCLAW_CONFIG_PATH: configPath,
    AWS_REGION: region,
    AWS_DEFAULT_REGION: region,
  };
  copyDefined(process.env, childEnv, REQUIRED_SECRETS);
  copyDefined(process.env, childEnv, ["SLACK_DM_ALLOWLIST"]);
  copyDefined(process.env, childEnv, PASSTHROUGH_ENV);
  return childEnv;
}

async function rejectSymlink(path) {
  try {
    const stat = await lstat(path);
    if (stat.isSymbolicLink()) throw new Error(`refusing symlink runtime path: ${path}`);
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }
}

async function prepareRuntime() {
  for (const name of REQUIRED_SECRETS) {
    const value = process.env[name];
    if (!value || value.includes("${")) throw new Error(`required runtime secret is missing: ${name}`);
  }

  const runtimeRoot = resolve(process.env.OPENCLAW_RUNTIME_DIR || "/tmp/teamagent-openclaw");
  if (runtimeRoot !== "/tmp/teamagent-openclaw") {
    throw new Error("OPENCLAW_RUNTIME_DIR is fixed to /tmp/teamagent-openclaw");
  }
  await rejectSymlink(runtimeRoot);
  await mkdir(runtimeRoot, { recursive: true, mode: 0o700 });
  await chmod(runtimeRoot, 0o700);
  const realRuntimeRoot = await realpath(runtimeRoot);
  if (realRuntimeRoot !== "/tmp" && !realRuntimeRoot.startsWith("/tmp/")) {
    throw new Error("resolved runtime directory escaped /tmp");
  }

  const stateDir = join(runtimeRoot, "state");
  const stateDatabaseDir = join(stateDir, "state");
  const workspaceDir = join(stateDir, "workspace");
  const homeDir = join(runtimeRoot, "home");
  const cacheDir = join(runtimeRoot, "cache");
  const configDir = join(runtimeRoot, "config");
  const configPath = join(configDir, "openclaw.json");
  for (const path of [
    stateDir,
    stateDatabaseDir,
    workspaceDir,
    homeDir,
    cacheDir,
    configDir,
  ]) {
    await rejectSymlink(path);
    await mkdir(path, { recursive: true, mode: 0o700 });
    await chmod(path, 0o700);
  }
  await rejectSymlink(configPath);

  const stateDatabasePath = join(stateDatabaseDir, "openclaw.sqlite");
  await rejectSymlink(stateDatabasePath);
  try {
    await copyFile(
      "/opt/teamagent/state-seed/openclaw.sqlite",
      stateDatabasePath,
      constants.COPYFILE_EXCL,
    );
    await chmod(stateDatabasePath, 0o600);
  } catch (error) {
    if (error?.code !== "EEXIST") throw error;
  }

  const templatePath = "/opt/teamagent/openclaw.template.json";
  const config = JSON.parse(await readFile(templatePath, "utf8"));
  const injectedAllowlist = parseAllowlist(process.env.SLACK_DM_ALLOWLIST);
  if (injectedAllowlist !== null) config.channels.slack.allowFrom = injectedAllowlist;
  assertConfig(config);

  for (const [id, [path, packageName]] of REQUIRED_PLUGINS) {
    const metadata = JSON.parse(await readFile(join(path, "package.json"), "utf8"));
    if (metadata.name !== packageName || metadata.version !== OPENCLAW_VERSION) {
      throw new Error(`plugin package mismatch: ${id}`);
    }
  }

  await writeFile(configPath, `${JSON.stringify(config, null, 2)}\n`, {
    encoding: "utf8",
    flag: "w",
    mode: 0o600,
  });
  await chmod(configPath, 0o600);

  for (const file of ["SOUL.md", "HEARTBEAT.md"]) {
    try {
      await copyFile(
        join("/opt/teamagent/workspace-seed", file),
        join(workspaceDir, file),
        constants.COPYFILE_EXCL,
      );
      await chmod(join(workspaceDir, file), 0o600);
    } catch (error) {
      if (error?.code !== "EEXIST") throw error;
    }
  }

  process.stderr.write(
    `${JSON.stringify({
      event: "openclaw_runtime_ready",
      buildCommit: process.env.TEAMAGENT_BUILD_COMMIT || "unknown",
      buildBranch: process.env.TEAMAGENT_BUILD_BRANCH || "unknown",
      dmPolicy: config.channels.slack.dmPolicy,
      allowFromCount: config.channels.slack.allowFrom.length,
      uid: process.getuid?.(),
    })}\n`,
  );
  return buildChildEnvironment({
    runtimeRoot,
    stateDir,
    workspaceDir,
    homeDir,
    cacheDir,
    configPath,
  });
}

function normalizeCommand(rawArgs) {
  const args = [...rawArgs];
  if (args.length === 0) {
    return ["/app/openclaw.mjs", "gateway", "--bind", "loopback", "--port", "18789"];
  }
  const explicitNode = ["node", "/nodejs/bin/node", process.execPath].includes(args[0]);
  if (explicitNode) args.shift();
  if (args.length === 0) throw new Error("runtime command is empty after removing node");
  if (["sh", "bash", "/bin/sh", "/bin/bash"].includes(args[0])) {
    throw new Error("shell commands are unavailable in the distroless runtime");
  }
  if (args[0] === "dist/index.js") args[0] = "/app/dist/index.js";
  if (!explicitNode && (args[0].startsWith("-") || !isAbsolute(args[0]))) {
    args.unshift("/app/openclaw.mjs");
  }
  return args;
}

async function runFallback(command, childEnv) {
  const child = spawn(process.execPath, command, {
    cwd: "/app",
    env: childEnv,
    stdio: "inherit",
  });

  const handlers = new Map();
  for (const signal of ["SIGTERM", "SIGINT", "SIGHUP"]) {
    const handler = () => {
      if (!child.killed) child.kill(signal);
    };
    handlers.set(signal, handler);
    process.on(signal, handler);
  }

  const exit = await new Promise((resolveExit, reject) => {
    child.once("error", reject);
    child.once("exit", (code, signal) => resolveExit({ code, signal }));
  });
  for (const [signal, handler] of handlers) process.off(signal, handler);
  if (exit.code !== null) process.exit(exit.code);
  const signalNumber = osConstants.signals[exit.signal] ?? 1;
  process.exit(128 + signalNumber);
}

async function run() {
  const childEnv = await prepareRuntime();
  const command = normalizeCommand(process.argv.slice(2));
  if (typeof process.execve === "function") {
    process.chdir("/app");
    process.execve(process.execPath, [process.execPath, ...command], childEnv);
    throw new Error("process.execve returned unexpectedly");
  }
  await runFallback(command, childEnv);
}

run().catch((error) => fail(error instanceof Error ? error.message : String(error)));
