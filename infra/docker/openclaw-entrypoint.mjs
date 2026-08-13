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
  "TEAMAGENT_CALLER_CLAIM_SECRET",
];
const OPENCLAW_VERSION = "2026.7.1";
const REQUIRED_PLUGINS = new Map([
  ["slack", ["/opt/teamagent/plugins/slack", "@openclaw/slack", OPENCLAW_VERSION]],
  [
    "amazon-bedrock",
    [
      "/opt/teamagent/plugins/amazon-bedrock",
      "@openclaw/amazon-bedrock-provider",
      OPENCLAW_VERSION,
    ],
  ],
  [
    "teamagent-caller-identity",
    [
      "/opt/teamagent/plugins/teamagent-caller-identity",
      "@teamagent/openclaw-caller-identity",
      "1.0.0",
    ],
  ],
]);
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

function parseSlackDmAccess(value) {
  if (value === undefined || value === "") {
    throw new Error(
      'SLACK_DM_ALLOWLIST is required; use "*" or 1-100 comma-separated Slack U IDs',
    );
  }
  if (value !== value.trim() || value.length > 2048) {
    throw new Error("SLACK_DM_ALLOWLIST is not in canonical form");
  }
  if (value === "*") {
    return { dmPolicy: "open", allowFrom: ["*"] };
  }

  const entries = value.split(",");
  if (
    entries.length === 0 ||
    entries.length > 100 ||
    entries.some((entry) => !/^U[A-Z0-9]{8,}$/.test(entry))
  ) {
    throw new Error(
      "SLACK_DM_ALLOWLIST must be 1-100 comma-separated Slack U IDs without spaces",
    );
  }
  if (new Set(entries).size !== entries.length) {
    throw new Error("SLACK_DM_ALLOWLIST must not contain duplicate Slack U IDs");
  }
  return { dmPolicy: "allowlist", allowFrom: entries };
}

function parseSlackTeamId(value) {
  if (typeof value !== "string" || !/^T[A-Z0-9]{8,}$/.test(value)) {
    throw new Error("SLACK_TEAM_ID is required and must be a canonical Slack T ID");
  }
  return value;
}

function assertConfig(config) {
  const slack = config?.channels?.slack;
  const allowFrom = slack?.allowFrom;
  if (!slack || !Array.isArray(allowFrom) || allowFrom.length === 0) {
    throw new Error("channels.slack.allowFrom must be a non-empty array");
  }
  if (
    slack.dmPolicy === "open" &&
    (allowFrom.length !== 1 || allowFrom[0] !== "*")
  ) {
    throw new Error('channels.slack.dmPolicy="open" requires exactly allowFrom=["*"]');
  }
  if (
    slack.dmPolicy === "allowlist" &&
    (allowFrom.length > 100 ||
      allowFrom.some(
        (entry) => typeof entry !== "string" || !/^U[A-Z0-9]{8,}$/.test(entry),
      ) ||
      new Set(allowFrom).size !== allowFrom.length)
  ) {
    throw new Error(
      'channels.slack.dmPolicy="allowlist" requires unique Slack U IDs',
    );
  }
  if (!["open", "allowlist"].includes(slack.dmPolicy)) {
    throw new Error("channels.slack.dmPolicy must be open or allowlist");
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
  copyDefined(process.env, childEnv, ["SLACK_DM_ALLOWLIST", "SLACK_TEAM_ID"]);
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
  const runtimeSecrets = new Map();
  for (const name of REQUIRED_SECRETS) {
    const value = process.env[name];
    if (!value || value.includes("${")) throw new Error(`required runtime secret is missing: ${name}`);
    if (
      name === "TEAMAGENT_CALLER_CLAIM_SECRET" &&
      Buffer.byteLength(value, "utf8") < 32
    ) {
      throw new Error("TEAMAGENT_CALLER_CLAIM_SECRET must contain at least 32 bytes");
    }
    runtimeSecrets.set(name, value);
  }
  if (
    runtimeSecrets.get("TEAMAGENT_CALLER_CLAIM_SECRET") ===
    runtimeSecrets.get("TEAMAGENT_MCP_BEARER")
  ) {
    throw new Error(
      "TEAMAGENT_CALLER_CLAIM_SECRET must differ from TEAMAGENT_MCP_BEARER",
    );
  }
  const slackDmAccess = parseSlackDmAccess(process.env.SLACK_DM_ALLOWLIST);
  parseSlackTeamId(process.env.SLACK_TEAM_ID);

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
  config.channels.slack.dmPolicy = slackDmAccess.dmPolicy;
  config.channels.slack.allowFrom = slackDmAccess.allowFrom;
  assertConfig(config);

  for (const [id, [path, packageName, expectedVersion]] of REQUIRED_PLUGINS) {
    const metadata = JSON.parse(await readFile(join(path, "package.json"), "utf8"));
    if (metadata.name !== packageName || metadata.version !== expectedVersion) {
      throw new Error(`plugin package mismatch: ${id}`);
    }
  }

  await writeFile(configPath, `${JSON.stringify(config, null, 2)}\n`, {
    encoding: "utf8",
    flag: "w",
    mode: 0o600,
  });
  await chmod(configPath, 0o600);

  // ペルソナ/名乗りは repo でレビューした seed が唯一の真実源なので**毎起動上書き**する。
  // かつては COPYFILE_EXCL（EEXIST 握り潰し）で初回だけ seed していたが、workspace は
  // EFS 上に永続するため、07-31 に初回 seed された TeamAgent 時代の SOUL.md が残り続け、
  // 2026-08-07 の NewsTV AI 改名がイメージを載せ替えても本番に一切届かなかった（実測:
  // config identity.name を変えても bot は「IDENTITY.md が未設定」と旧人格のまま名乗った）。
  // エージェントの長期記憶は MEMORY.md 系の別ファイルであり、この 3 枚は上書きしてよい。
  for (const file of ["SOUL.md", "HEARTBEAT.md", "IDENTITY.md"]) {
    const target = join(workspaceDir, file);
    // 上書き化で copyFile がリンク先へ追従し得るため、configPath/sqlite と同じく
    // 書き込み前に symlink を拒否する（EXCL 時代には不要だった検査）。
    await rejectSymlink(target);
    await copyFile(join("/opt/teamagent/workspace-seed", file), target);
    await chmod(target, 0o600);
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
