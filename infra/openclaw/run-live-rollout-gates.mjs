#!/usr/bin/env node

// Post-stable production rollout gates.  All target identities are constants:
// no environment variable or command-line option can retarget account, service,
// task family, log group, canary secret, or repository.

import { createHash, randomBytes } from "node:crypto";
import fs from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ACCOUNT = "718959508629";
const REGION = "ap-northeast-1";
const CLUSTER = "teamagent-dev";
const SERVICE = "teamagent-dev-openclaw";
const FAMILY = "teamagent-dev-openclaw";
const CONTAINER = "openclaw";
const LOG_GROUP = "/teamagent/dev/openclaw";
const CANARY_SECRET = "teamagent/dev/openclaw/rollout-canary";
const TASK_ARN_PATTERN =
  /^arn:aws:ecs:ap-northeast-1:718959508629:task-definition\/teamagent-dev-openclaw:[1-9][0-9]*$/u;
const RECEIPT_ID_PATTERN = /^[A-Za-z0-9._:-]{16,256}$/u;
const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const TOOL_SCOPE_PATH = resolve(SCRIPT_DIR, "effective-tool-scope.json");

function canonicalSha256(value) {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

function checkedJsonFile(path) {
  const stat = fs.lstatSync(path);
  if (!stat.isFile() || stat.isSymbolicLink()) {
    throw new Error(`JSON input is not a regular non-symlink file: ${path}`);
  }
  return JSON.parse(fs.readFileSync(path, "utf8"));
}

function fixedAwsEnvironment() {
  const environment = { ...process.env };
  for (const name of Object.keys(environment)) {
    if (
      name === "AWS_PROFILE" ||
      name === "AWS_DEFAULT_PROFILE" ||
      name === "AWS_REGION" ||
      name === "AWS_DEFAULT_REGION" ||
      name.startsWith("AWS_ENDPOINT_URL")
    ) {
      delete environment[name];
    }
  }
  environment.AWS_REGION = REGION;
  environment.AWS_DEFAULT_REGION = REGION;
  return environment;
}

function awsJson(service, operation, args = []) {
  const result = spawnSync(
    "aws",
    [
      service,
      operation,
      "--region",
      REGION,
      ...args,
      "--output",
      "json",
      "--no-cli-pager",
    ],
    {
      encoding: "utf8",
      env: fixedAwsEnvironment(),
      maxBuffer: 16 * 1024 * 1024,
    },
  );
  if (result.status !== 0) {
    const detail = (result.stderr || "").trim().slice(0, 1000);
    throw new Error(`AWS ${service} ${operation} failed: ${detail}`);
  }
  return JSON.parse(result.stdout);
}

function awsWait(service, waiter, args = []) {
  const result = spawnSync(
    "aws",
    [
      service,
      "wait",
      waiter,
      "--region",
      REGION,
      ...args,
      "--no-cli-pager",
    ],
    {
      encoding: "utf8",
      env: fixedAwsEnvironment(),
      maxBuffer: 4 * 1024 * 1024,
    },
  );
  if (result.status !== 0) {
    throw new Error(`AWS ${service} waiter ${waiter} failed`);
  }
}

export function validateConsumption(
  consumption,
  { newTaskDefinition, previousTaskDefinition },
) {
  if (
    consumption?.schemaVersion !== 1 ||
    consumption.verified !== true ||
    consumption.consumed !== true ||
    consumption.atomic !== true ||
    !RECEIPT_ID_PATTERN.test(consumption.receiptId || "") ||
    consumption.previousTaskDefinitionArn !== previousTaskDefinition ||
    consumption.newTaskDefinitionArn !== newTaskDefinition ||
    !TASK_ARN_PATTERN.test(newTaskDefinition) ||
    !TASK_ARN_PATTERN.test(previousTaskDefinition) ||
    !/^s3:\/\/teamagent-dev-raw-files\/trusted-release\/openclaw\/deployment-receipts\//u.test(
      consumption.durableReceipt?.uri || "",
    ) ||
    typeof consumption.durableReceipt?.versionId !== "string" ||
    consumption.durableReceipt.versionId.length === 0 ||
    !/^[0-9a-f]{64}$/u.test(consumption.durableReceipt?.sha256 || "")
  ) {
    throw new Error("receipt consumption does not bind durable rollback state");
  }
  return consumption.receiptId;
}

export function validateStableService(serviceDocument, newTaskDefinition) {
  const service = serviceDocument?.services?.[0];
  const primary = service?.deployments?.filter(
    (deployment) => deployment.status === "PRIMARY",
  );
  if (
    serviceDocument.failures?.length !== 0 ||
    serviceDocument.services?.length !== 1 ||
    service?.serviceName !== SERVICE ||
    !service.clusterArn?.endsWith(`/${CLUSTER}`) ||
    service.taskDefinition !== newTaskDefinition ||
    service.desiredCount < 1 ||
    service.runningCount !== service.desiredCount ||
    service.pendingCount !== 0 ||
    primary?.length !== 1 ||
    primary[0].taskDefinition !== newTaskDefinition ||
    primary[0].rolloutState !== "COMPLETED" ||
    service.deploymentConfiguration?.deploymentCircuitBreaker?.enable !== true ||
    service.deploymentConfiguration?.deploymentCircuitBreaker?.rollback !== true
  ) {
    throw new Error("ECS service is not stably running the exact candidate task");
  }
  const awsvpc = service.networkConfiguration?.awsvpcConfiguration;
  if (
    !awsvpc ||
    !Array.isArray(awsvpc.subnets) ||
    awsvpc.subnets.length === 0 ||
    !Array.isArray(awsvpc.securityGroups) ||
    awsvpc.securityGroups.length === 0 ||
    !["ENABLED", "DISABLED"].includes(awsvpc.assignPublicIp)
  ) {
    throw new Error("ECS service has no usable awsvpc canary configuration");
  }
  return awsvpc;
}

export function validateStoppedCanaryTask(
  taskDocument,
  { taskArn, newTaskDefinition },
) {
  const task = taskDocument?.tasks?.[0];
  const container = task?.containers?.find((entry) => entry.name === CONTAINER);
  if (
    taskDocument.failures?.length !== 0 ||
    taskDocument.tasks?.length !== 1 ||
    task?.taskArn !== taskArn ||
    task?.clusterArn?.endsWith(`/${CLUSTER}`) !== true ||
    task?.taskDefinitionArn !== newTaskDefinition ||
    task?.lastStatus !== "STOPPED" ||
    task?.stopCode !== "EssentialContainerExited" ||
    task?.containers?.length !== 1 ||
    container?.exitCode !== 0 ||
    container?.reason
  ) {
    throw new Error("one-off rollout task did not pass with one clean container");
  }
  return taskArn.split("/").at(-1);
}

export function validateTaskCanaryEvent(
  event,
  { receiptId, expectedToolNames },
) {
  const expectedNames = [...expectedToolNames].sort();
  if (
    event?.event !== "openclaw_rollout_task_canary" ||
    event.schemaVersion !== 1 ||
    event.receiptId !== receiptId ||
    event.platform !== "linux/arm64" ||
    event.passed !== true ||
    event.mcp?.toolCount !== expectedNames.length ||
    event.mcp?.toolNamesSha256 !== canonicalSha256(expectedNames) ||
    event.bedrock?.request !== "Converse" ||
    event.bedrock?.passed !== true ||
    ![
      "ECS_CONTAINER_CREDENTIALS_RELATIVE_URI",
      "ECS_CONTAINER_CREDENTIALS_FULL_URI",
    ].includes(event.bedrock?.credentialSource) ||
    typeof event.bedrock?.modelId !== "string" ||
    event.bedrock.modelId.length === 0
  ) {
    throw new Error("rollout task log does not prove exact MCP and Bedrock gates");
  }
  return true;
}

function validateSlackSecret(secret) {
  if (
    typeof secret !== "object" ||
    !/^xoxp-[A-Za-z0-9-]{20,}$/u.test(secret.userToken || "") ||
    !/^[CG][A-Z0-9]{8,}$/u.test(secret.channelId || "") ||
    !/^U[A-Z0-9]{8,}$/u.test(secret.botUserId || "") ||
    Object.keys(secret).sort().join(",") !==
      ["botUserId", "channelId", "userToken"].sort().join(",")
  ) {
    throw new Error("fixed Slack rollout secret has an invalid shape");
  }
  return secret;
}

async function slackApi(method, token, body) {
  const response = await fetch(`https://slack.com/api/${method}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json; charset=utf-8",
    },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(15_000),
  });
  const payload = await response.json();
  if (!response.ok || payload.ok !== true) {
    throw new Error(`Slack ${method} failed: ${payload.error || response.status}`);
  }
  return payload;
}

async function verifySlackMentionReply(secret, receiptId) {
  const token = `OPENCLAW_CANARY_${randomBytes(12).toString("hex")}`;
  const posted = await slackApi("chat.postMessage", secret.userToken, {
    channel: secret.channelId,
    text: `<@${secret.botUserId}> deployment canary. Reply with exactly ${token}`,
  });
  try {
    const deadline = Date.now() + 120_000;
    while (Date.now() < deadline) {
      const thread = await slackApi(
        "conversations.replies",
        secret.userToken,
        {
          channel: secret.channelId,
          ts: posted.ts,
          limit: 100,
          inclusive: true,
        },
      );
      const matching = thread.messages?.find(
        (message) =>
          message.ts !== posted.ts &&
          message.user === secret.botUserId &&
          message.text?.trim() === token,
      );
      if (matching) {
        return {
          connected: true,
          mentionReplyExact: true,
          correlationSha256: createHash("sha256")
            .update(`${receiptId}:${token}`)
            .digest("hex"),
        };
      }
      await new Promise((resolveWait) => setTimeout(resolveWait, 3000));
    }
    throw new Error("Slack canary mention did not receive the exact bot reply");
  } finally {
    await slackApi("chat.delete", secret.userToken, {
      channel: secret.channelId,
      ts: posted.ts,
    }).catch(() => {});
  }
}

function readExpectedToolNames() {
  const scope = checkedJsonFile(TOOL_SCOPE_PATH);
  const names = scope.tools
    .filter((tool) => tool.defaultEnabledByTerraform === true)
    .map((tool) => tool.name)
    .sort();
  if (
    scope.schemaVersion !== 1 ||
    names.length !== 12 ||
    names.length !== new Set(names).size
  ) {
    throw new Error("reviewed default MCP tool scope is invalid");
  }
  return names;
}

function fetchTaskCanaryEvent(taskId, receiptId, expectedToolNames) {
  const streamName = `openclaw/${CONTAINER}/${taskId}`;
  for (let attempt = 0; attempt < 30; attempt += 1) {
    let logs;
    try {
      logs = awsJson("logs", "get-log-events", [
        "--log-group-name",
        LOG_GROUP,
        "--log-stream-name",
        streamName,
        "--start-from-head",
      ]);
    } catch (error) {
      if (attempt === 29) throw error;
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 2000);
      continue;
    }
    const candidates = [];
    for (const logEvent of logs.events || []) {
      try {
        const parsed = JSON.parse(logEvent.message);
        if (parsed.event === "openclaw_rollout_task_canary") {
          candidates.push(parsed);
        }
      } catch {
        // Non-JSON application lines are irrelevant; secrets are never copied.
      }
    }
    if (candidates.length === 1) {
      validateTaskCanaryEvent(candidates[0], { receiptId, expectedToolNames });
      return candidates[0];
    }
    if (candidates.length > 1) {
      throw new Error("rollout task emitted duplicate success events");
    }
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 2000);
  }
  throw new Error("rollout task success event was not delivered to CloudWatch Logs");
}

function writeOutput(path, value) {
  const absolute = resolve(path);
  try {
    if (fs.lstatSync(absolute).isSymbolicLink()) {
      throw new Error("rollout output must not be a symlink");
    }
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  const temporary = `${absolute}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, {
    mode: 0o600,
    flag: "wx",
  });
  fs.renameSync(temporary, absolute);
}

async function runLive(args) {
  const values = new Map();
  for (let index = 0; index < args.length; index += 2) {
    if (!args[index]?.startsWith("--") || args[index + 1] === undefined) {
      throw new Error("rollout gate arguments must be exact --name value pairs");
    }
    if (values.has(args[index])) throw new Error(`duplicate argument: ${args[index]}`);
    values.set(args[index], args[index + 1]);
  }
  const allowed = [
    "--new-task-definition",
    "--previous-task-definition",
    "--receipt-consumption",
    "--output",
  ];
  if (
    values.size !== allowed.length ||
    allowed.some((name) => !values.has(name)) ||
    [...values.keys()].some((name) => !allowed.includes(name))
  ) {
    throw new Error(
      "usage: run-live-rollout-gates.mjs --new-task-definition <arn> --previous-task-definition <arn> --receipt-consumption <json> --output <json>",
    );
  }
  const newTaskDefinition = values.get("--new-task-definition");
  const previousTaskDefinition = values.get("--previous-task-definition");
  const consumption = checkedJsonFile(values.get("--receipt-consumption"));
  const receiptId = validateConsumption(consumption, {
    newTaskDefinition,
    previousTaskDefinition,
  });
  const caller = awsJson("sts", "get-caller-identity");
  if (caller.Account !== ACCOUNT) throw new Error("AWS caller account is not fixed target");

  const serviceDocument = awsJson("ecs", "describe-services", [
    "--cluster",
    CLUSTER,
    "--services",
    SERVICE,
  ]);
  const awsvpc = validateStableService(serviceDocument, newTaskDefinition);
  const expectedToolNames = readExpectedToolNames();
  const overrides = {
    containerOverrides: [
      {
        name: CONTAINER,
        command: [
          "/opt/teamagent/rollout-task-canary.mjs",
          "--receipt-id",
          receiptId,
        ],
      },
    ],
  };
  const runTask = awsJson("ecs", "run-task", [
    "--cluster",
    CLUSTER,
    "--task-definition",
    newTaskDefinition,
    "--launch-type",
    "FARGATE",
    "--count",
    "1",
    "--network-configuration",
    JSON.stringify({ awsvpcConfiguration: awsvpc }),
    "--overrides",
    JSON.stringify(overrides),
    "--started-by",
    `openclaw-canary-${createHash("sha256").update(receiptId).digest("hex").slice(0, 16)}`,
  ]);
  if (runTask.failures?.length !== 0 || runTask.tasks?.length !== 1) {
    throw new Error("ECS rejected the one-off rollout canary task");
  }
  const taskArn = runTask.tasks[0].taskArn;
  awsWait("ecs", "tasks-stopped", ["--cluster", CLUSTER, "--tasks", taskArn]);
  const taskDocument = awsJson("ecs", "describe-tasks", [
    "--cluster",
    CLUSTER,
    "--tasks",
    taskArn,
  ]);
  const taskId = validateStoppedCanaryTask(taskDocument, {
    taskArn,
    newTaskDefinition,
  });
  const taskEvent = fetchTaskCanaryEvent(taskId, receiptId, expectedToolNames);

  const secretResult = awsJson("secretsmanager", "get-secret-value", [
    "--secret-id",
    CANARY_SECRET,
    "--version-stage",
    "AWSCURRENT",
  ]);
  if (typeof secretResult.SecretString !== "string") {
    throw new Error("Slack rollout secret is not a JSON SecretString");
  }
  const slack = await verifySlackMentionReply(
    validateSlackSecret(JSON.parse(secretResult.SecretString)),
    receiptId,
  );
  const report = {
    schemaVersion: 1,
    receiptId,
    account: ACCOUNT,
    region: REGION,
    cluster: CLUSTER,
    service: SERVICE,
    taskFamily: FAMILY,
    previousTaskDefinitionArn: previousTaskDefinition,
    newTaskDefinitionArn: newTaskDefinition,
    ecsServiceStable: true,
    circuitBreakerRollbackEnabled: true,
    oneOffTask: {
      taskArn,
      exactTaskDefinition: true,
      exitCode: 0,
    },
    mcp: taskEvent.mcp,
    bedrock: taskEvent.bedrock,
    slack,
    passed: true,
  };
  writeOutput(values.get("--output"), report);
}

function validateFixture(path) {
  const fixture = checkedJsonFile(path);
  const receiptId = validateConsumption(fixture.consumption, fixture.expected);
  validateStableService(fixture.service, fixture.expected.newTaskDefinition);
  const taskId = validateStoppedCanaryTask(fixture.task, {
    taskArn: fixture.task.tasks[0].taskArn,
    newTaskDefinition: fixture.expected.newTaskDefinition,
  });
  validateTaskCanaryEvent(fixture.taskEvent, {
    receiptId,
    expectedToolNames: fixture.expected.toolNames,
  });
  return { passed: true, taskId, receiptId };
}

const isMain =
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href;
if (isMain) {
  if (process.argv[2] === "--validate-fixture" && process.argv.length === 4) {
    try {
      process.stdout.write(`${JSON.stringify(validateFixture(process.argv[3]))}\n`);
    } catch (error) {
      process.stderr.write(
        `${error instanceof Error ? error.message : String(error)}\n`,
      );
      process.exitCode = 1;
    }
  } else {
    runLive(process.argv.slice(2)).catch((error) => {
      process.stderr.write(
        `${JSON.stringify({
          event: "openclaw_rollout_gate_error",
          level: "error",
          message: error instanceof Error ? error.message : String(error),
        })}\n`,
      );
      process.exitCode = 1;
    });
  }
}
