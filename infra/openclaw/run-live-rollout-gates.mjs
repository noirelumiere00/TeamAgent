#!/usr/bin/env node

// Canonical post-apply production rollout gates for OpenClaw. Every AWS target
// is fixed here. The Terraform runtime guard invokes this process while it
// still owns both deployment locks and before it records the intent APPLIED.

import { createHash, randomBytes } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ACCOUNT = "718959508629";
const REGION = "ap-northeast-1";
const CLUSTER = "teamagent-dev";
const SERVICE = "teamagent-dev-openclaw";
const FAMILY = "teamagent-dev-openclaw";
const CONTAINER = "openclaw";
const MCP_FAMILY = "teamagent-dev-mcp";
const MCP_CONTAINER = "teamagent-mcp";
const LOG_GROUP = "/teamagent/dev/openclaw";
const CANARY_SECRET = "teamagent/dev/openclaw/rollout-canary";
const BEDROCK_MODEL =
  "jp.anthropic.claude-haiku-4-5-20251001-v1:0";
const AUTOMATION_ROLE_ARN =
  "arn:aws:sts::718959508629:assumed-role/teamagent-dev-terraform-runtime-automation/teamagent-terraform-worker";
const AUTOMATION_SESSION_SUFFIX = ":teamagent-terraform-worker";
const DEPLOYMENT_LEDGER = "teamagent-dev-image-deployment-intents";
const DEPLOYMENT_LOCK_RECORD_ID = "lock#teamagent/terraform.tfstate";
const EVIDENCE_BUCKET = "teamagent-dev-openclaw-rollout-evidence";
const EVIDENCE_ENCRYPTION_ALIAS =
  "alias/teamagent-dev-openclaw-rollout-evidence";
const EVIDENCE_SIGNING_ALIAS =
  "alias/teamagent-dev-openclaw-rollout-signing";
const SIGNING_ALGORITHM = "RSASSA_PSS_SHA_256";
const RESULT_PREFIX = "rollout-results";
const RETENTION_DAYS = 3650;
const TASK_ARN_PATTERN =
  /^arn:aws:ecs:ap-northeast-1:718959508629:task-definition\/teamagent-dev-openclaw:[1-9][0-9]*$/u;
const MCP_TASK_ARN_PATTERN =
  /^arn:aws:ecs:ap-northeast-1:718959508629:task-definition\/teamagent-dev-mcp:[1-9][0-9]*$/u;
const RUNNING_TASK_ARN_PATTERN =
  /^arn:aws:ecs:ap-northeast-1:718959508629:task\/teamagent-dev\/[0-9a-f]{32}$/u;
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;
const SHA256_PATTERN = /^[0-9a-f]{64}$/u;
const S3_VERSION_PATTERN = /^[A-Za-z0-9._~+/=-]{1,1024}$/u;
const KMS_ARN_PATTERN =
  /^arn:aws:kms:ap-northeast-1:718959508629:key\/[0-9a-f-]{36}$/u;
const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const TOOL_SCOPE_PATH = resolve(SCRIPT_DIR, "effective-tool-scope.json");

function canonicalJson(value) {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  }
  return `{${Object.keys(value)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
    .join(",")}}`;
}

function canonicalBytes(value) {
  return Buffer.from(`${canonicalJson(value)}\n`, "utf8");
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function canonicalSha256(value) {
  return sha256(canonicalBytes(value));
}

function compactJsonSha256(value) {
  return sha256(Buffer.from(JSON.stringify(value), "utf8"));
}

function validS3VersionId(value) {
  return (
    S3_VERSION_PATTERN.test(value || "") &&
    value !== "null" &&
    value !== "None"
  );
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
  environment.AWS_PAGER = "";
  environment.AWS_IGNORE_CONFIGURED_ENDPOINT_URLS = "true";
  return environment;
}

function runAws(service, operation, args, { json = true } = {}) {
  const command = [
    service,
    operation,
    "--region",
    REGION,
    ...args,
    "--no-cli-pager",
    "--no-paginate",
  ];
  if (json) command.push("--output", "json");
  const result = spawnSync("aws", command, {
    encoding: "utf8",
    env: fixedAwsEnvironment(),
    maxBuffer: 32 * 1024 * 1024,
  });
  if (result.status !== 0) {
    const detail = (result.stderr || "").trim().slice(0, 1000);
    throw new Error(`AWS ${service} ${operation} failed: ${detail}`);
  }
  if (!json) return undefined;
  return JSON.parse(result.stdout || "{}");
}

function awsJson(service, operation, args = []) {
  return runAws(service, operation, args);
}

function awsWait(service, waiter, args = []) {
  runAws(service, "wait", [waiter, ...args], { json: false });
}

function awsDownload({ bucket, key, versionId, output }) {
  return awsJson("s3api", "get-object", [
    "--bucket",
    bucket,
    "--key",
    key,
    "--version-id",
    versionId,
    "--expected-bucket-owner",
    ACCOUNT,
    output,
  ]);
}

function assertAutomationCaller(caller) {
  if (
    caller?.Account !== ACCOUNT ||
    caller?.Arn !== AUTOMATION_ROLE_ARN ||
    typeof caller?.UserId !== "string" ||
    !caller.UserId.endsWith(AUTOMATION_SESSION_SUFFIX)
  ) {
    throw new Error(
      "rollout gates require the exact trusted Terraform automation role session",
    );
  }
  return caller.Arn;
}

function validateDistinctRevisions(previousTaskDefinition, newTaskDefinition) {
  if (
    !TASK_ARN_PATTERN.test(previousTaskDefinition || "") ||
    !TASK_ARN_PATTERN.test(newTaskDefinition || "") ||
    previousTaskDefinition === newTaskDefinition
  ) {
    throw new Error(
      "rollout requires distinct fixed-family previous and candidate task revisions",
    );
  }
}

export function validateConsumption(
  consumption,
  { applyAttemptId, planSha256 },
) {
  if (
    consumption?.record_type !== "teamagent.image-deployment-intent" ||
    consumption.schema_version !== 1 ||
    consumption.state !== "CONSUMED" ||
    !UUID_PATTERN.test(consumption.intent_id || "") ||
    consumption.record_id !== `intent#${consumption.intent_id}` ||
    consumption.apply_attempt_id !== applyAttemptId ||
    consumption.plan_sha256 !== planSha256 ||
    !UUID_PATTERN.test(applyAttemptId || "") ||
    !SHA256_PATTERN.test(planSha256 || "") ||
    consumption.intent_id === applyAttemptId ||
    !SHA256_PATTERN.test(consumption.deployment_context_sha256 || "") ||
    !SHA256_PATTERN.test(consumption.receipt_claims_sha256 || "") ||
    !SHA256_PATTERN.test(consumption.gate_query_sha256 || "") ||
    typeof consumption.consumed_at !== "string"
  ) {
    throw new Error(
      "consumed one-use deployment intent does not bind this apply attempt",
    );
  }
  return consumption.intent_id;
}

function decodeDynamoAttribute(attribute) {
  if (
    attribute &&
    Object.keys(attribute).length === 1 &&
    typeof attribute.S === "string"
  ) {
    return attribute.S;
  }
  if (
    attribute &&
    Object.keys(attribute).length === 1 &&
    typeof attribute.N === "string" &&
    /^(0|[1-9][0-9]*)$/u.test(attribute.N)
  ) {
    return Number(attribute.N);
  }
  if (
    attribute &&
    Object.keys(attribute).length === 1 &&
    typeof attribute.BOOL === "boolean"
  ) {
    return attribute.BOOL;
  }
  throw new Error("deployment ledger contains an unsupported attribute");
}

function decodeDynamoItem(item) {
  if (!item || typeof item !== "object" || Array.isArray(item)) {
    throw new Error("deployment ledger item is missing");
  }
  return Object.fromEntries(
    Object.entries(item).map(([key, value]) => [
      key,
      decodeDynamoAttribute(value),
    ]),
  );
}

function dynamoAttribute(value) {
  if (typeof value === "string" && value.length > 0) return { S: value };
  if (Number.isInteger(value) && value >= 0) return { N: String(value) };
  if (typeof value === "boolean") return { BOOL: value };
  throw new Error("rollback authorization attribute is invalid");
}

function dynamoItem(value) {
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => [
      key,
      dynamoAttribute(item),
    ]),
  );
}

function getLedgerItem(recordId) {
  const response = awsJson("dynamodb", "get-item", [
    "--table-name",
    DEPLOYMENT_LEDGER,
    "--key",
    JSON.stringify({ record_id: { S: recordId } }),
    "--consistent-read",
  ]);
  return response.Item ? decodeDynamoItem(response.Item) : null;
}

function validateLiveConsumption(consumption, context) {
  const live = getLedgerItem(consumption.record_id);
  const lock = getLedgerItem(DEPLOYMENT_LOCK_RECORD_ID);
  if (
    !live ||
    live.state !== "CONSUMED" ||
    live.intent_id !== consumption.intent_id ||
    live.apply_attempt_id !== context.applyAttemptId ||
    live.plan_sha256 !== context.planSha256 ||
    live.receipt_claims_sha256 !== consumption.receipt_claims_sha256 ||
    live.gate_query_sha256 !== consumption.gate_query_sha256 ||
    !lock ||
    lock.record_type !== "teamagent.image-release-apply-lock" ||
    lock.state !== "LOCKED" ||
    lock.intent_id !== consumption.intent_id ||
    lock.apply_attempt_id !== context.applyAttemptId ||
    lock.plan_sha256 !== context.planSha256
  ) {
    throw new Error(
      "live one-use deployment intent/shared lock no longer binds rollout",
    );
  }
  return { intent: live, lock };
}

function rollbackRecordId(applyAttemptId) {
  return `openclaw-rollback#${applyAttemptId}`;
}

function rollbackBinding(context) {
  return {
    record_id: rollbackRecordId(context.applyAttemptId),
    record_type: "teamagent.openclaw-post-apply-rollback-authorization",
    schema_version: 1,
    intent_id: context.intentId,
    plan_sha256: context.planSha256,
    apply_attempt_id: context.applyAttemptId,
    previous_task_definition_arn: context.previousTaskDefinition,
    new_task_definition_arn: context.newTaskDefinition,
    one_use: true,
  };
}

export function validateRollbackAuthorization(record, context, states) {
  const binding = rollbackBinding(context);
  const expectedKeys = new Set([
    ...Object.keys(binding),
    "state",
    "authorized_at_epoch",
    "expires_at_epoch",
    ...(record?.state === "CONSUMED" ? ["consumed_at_epoch"] : []),
  ]);
  if (
    !record ||
    Object.keys(record).some((key) => !expectedKeys.has(key)) ||
    [...expectedKeys].some((key) => !(key in record)) ||
    Object.entries(binding).some(([key, value]) => record[key] !== value) ||
    !states.includes(record.state) ||
    !Number.isInteger(record.authorized_at_epoch) ||
    !Number.isInteger(record.expires_at_epoch) ||
    record.expires_at_epoch <= record.authorized_at_epoch ||
    (record.state === "AUTHORIZED" &&
      record.expires_at_epoch <= Math.floor(Date.now() / 1000)) ||
    (record.state === "CONSUMED" &&
      (!Number.isInteger(record.consumed_at_epoch) ||
        record.consumed_at_epoch < record.authorized_at_epoch))
  ) {
    throw new Error(
      "one-use rollback authorization does not exactly bind old/new revisions",
    );
  }
  return record;
}

function transactionToken(applyAttemptId, phase) {
  return `${phase}-${sha256(`${phase}:${applyAttemptId}`).slice(0, 20)}`;
}

function prepareRollbackAuthorization(context) {
  const existing = getLedgerItem(rollbackRecordId(context.applyAttemptId));
  if (existing) {
    return validateRollbackAuthorization(existing, context, [
      "AUTHORIZED",
      "CONSUMED",
    ]);
  }
  const now = Math.floor(Date.now() / 1000);
  const item = {
    ...rollbackBinding(context),
    state: "AUTHORIZED",
    authorized_at_epoch: now,
    expires_at_epoch: now + 3600,
  };
  const transaction = [
    {
      ConditionCheck: {
        TableName: DEPLOYMENT_LEDGER,
        Key: dynamoItem({ record_id: `intent#${context.intentId}` }),
        ConditionExpression:
          "#state = :consumed AND apply_attempt_id = :attempt AND plan_sha256 = :plan",
        ExpressionAttributeNames: { "#state": "state" },
        ExpressionAttributeValues: dynamoItem({
          ":consumed": "CONSUMED",
          ":attempt": context.applyAttemptId,
          ":plan": context.planSha256,
        }),
      },
    },
    {
      ConditionCheck: {
        TableName: DEPLOYMENT_LEDGER,
        Key: dynamoItem({ record_id: DEPLOYMENT_LOCK_RECORD_ID }),
        ConditionExpression:
          "#state = :locked AND intent_id = :intent AND apply_attempt_id = :attempt AND plan_sha256 = :plan",
        ExpressionAttributeNames: { "#state": "state" },
        ExpressionAttributeValues: dynamoItem({
          ":locked": "LOCKED",
          ":intent": context.intentId,
          ":attempt": context.applyAttemptId,
          ":plan": context.planSha256,
        }),
      },
    },
    {
      Put: {
        TableName: DEPLOYMENT_LEDGER,
        Item: dynamoItem(item),
        ConditionExpression: "attribute_not_exists(record_id)",
      },
    },
  ];
  try {
    awsJson("dynamodb", "transact-write-items", [
      "--transact-items",
      JSON.stringify(transaction),
      "--client-request-token",
      transactionToken(context.applyAttemptId, "authorize"),
      "--return-consumed-capacity",
      "NONE",
    ]);
  } catch (error) {
    const raced = getLedgerItem(rollbackRecordId(context.applyAttemptId));
    if (!raced) throw error;
    return validateRollbackAuthorization(raced, context, [
      "AUTHORIZED",
      "CONSUMED",
    ]);
  }
  return validateRollbackAuthorization(
    getLedgerItem(rollbackRecordId(context.applyAttemptId)),
    context,
    ["AUTHORIZED"],
  );
}

function consumeRollbackAuthorization(context) {
  const current = prepareRollbackAuthorization(context);
  if (current.state === "CONSUMED") return current;
  const now = Math.floor(Date.now() / 1000);
  const transaction = [
    {
      ConditionCheck: {
        TableName: DEPLOYMENT_LEDGER,
        Key: dynamoItem({ record_id: `intent#${context.intentId}` }),
        ConditionExpression:
          "#state = :consumed AND apply_attempt_id = :attempt AND plan_sha256 = :plan",
        ExpressionAttributeNames: { "#state": "state" },
        ExpressionAttributeValues: dynamoItem({
          ":consumed": "CONSUMED",
          ":attempt": context.applyAttemptId,
          ":plan": context.planSha256,
        }),
      },
    },
    {
      ConditionCheck: {
        TableName: DEPLOYMENT_LEDGER,
        Key: dynamoItem({ record_id: DEPLOYMENT_LOCK_RECORD_ID }),
        ConditionExpression:
          "#state = :locked AND intent_id = :intent AND apply_attempt_id = :attempt AND plan_sha256 = :plan",
        ExpressionAttributeNames: { "#state": "state" },
        ExpressionAttributeValues: dynamoItem({
          ":locked": "LOCKED",
          ":intent": context.intentId,
          ":attempt": context.applyAttemptId,
          ":plan": context.planSha256,
        }),
      },
    },
    {
      Update: {
        TableName: DEPLOYMENT_LEDGER,
        Key: dynamoItem({
          record_id: rollbackRecordId(context.applyAttemptId),
        }),
        UpdateExpression:
          "SET #state = :consumed, consumed_at_epoch = :consumed_at",
        ConditionExpression:
          "#state = :authorized AND record_type = :record_type " +
          "AND schema_version = :schema AND one_use = :one_use " +
          "AND intent_id = :intent AND apply_attempt_id = :attempt " +
          "AND plan_sha256 = :plan " +
          "AND previous_task_definition_arn = :previous " +
          "AND new_task_definition_arn = :new AND expires_at_epoch > :now",
        ExpressionAttributeNames: { "#state": "state" },
        ExpressionAttributeValues: dynamoItem({
          ":authorized": "AUTHORIZED",
          ":consumed": "CONSUMED",
          ":consumed_at": now,
          ":record_type":
            "teamagent.openclaw-post-apply-rollback-authorization",
          ":schema": 1,
          ":one_use": true,
          ":intent": context.intentId,
          ":attempt": context.applyAttemptId,
          ":plan": context.planSha256,
          ":previous": context.previousTaskDefinition,
          ":new": context.newTaskDefinition,
          ":now": now,
        }),
      },
    },
  ];
  try {
    awsJson("dynamodb", "transact-write-items", [
      "--transact-items",
      JSON.stringify(transaction),
      "--client-request-token",
      transactionToken(context.applyAttemptId, "consume"),
      "--return-consumed-capacity",
      "NONE",
    ]);
  } catch (error) {
    const consumed = getLedgerItem(rollbackRecordId(context.applyAttemptId));
    if (!consumed || consumed.state !== "CONSUMED") throw error;
  }
  return validateRollbackAuthorization(
    getLedgerItem(rollbackRecordId(context.applyAttemptId)),
    context,
    ["CONSUMED"],
  );
}

export function validateStableService(serviceDocument, taskDefinition) {
  const service = serviceDocument?.services?.[0];
  const primary = service?.deployments?.filter(
    (deployment) => deployment.status === "PRIMARY",
  );
  if (
    serviceDocument.failures?.length !== 0 ||
    serviceDocument.services?.length !== 1 ||
    service?.serviceName !== SERVICE ||
    !service.clusterArn?.endsWith(`/${CLUSTER}`) ||
    service.taskDefinition !== taskDefinition ||
    service.desiredCount !== 1 ||
    service.runningCount !== service.desiredCount ||
    service.pendingCount !== 0 ||
    service.deployments?.length !== 1 ||
    primary?.length !== 1 ||
    primary[0].taskDefinition !== taskDefinition ||
    primary[0].rolloutState !== "COMPLETED" ||
    service.deploymentConfiguration?.deploymentCircuitBreaker?.enable !==
      true ||
    service.deploymentConfiguration?.deploymentCircuitBreaker?.rollback !==
      true
  ) {
    throw new Error("ECS service is not stably running the exact task revision");
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
  return { awsvpc, desiredCount: service.desiredCount };
}

function describeService() {
  return awsJson("ecs", "describe-services", [
    "--cluster",
    CLUSTER,
    "--services",
    SERVICE,
  ]);
}

function taskId(taskArn) {
  return taskArn.split("/").at(-1);
}

function taskLogStream(taskArn) {
  return `openclaw/${CONTAINER}/${taskId(taskArn)}`;
}

export function validateRunningTaskInventory(
  inventory,
  { taskDefinition, desiredCount },
) {
  if (
    inventory?.complete !== true ||
    !Array.isArray(inventory.taskArns) ||
    !Array.isArray(inventory.tasks) ||
    inventory.taskArns.length !== desiredCount ||
    inventory.tasks.length !== desiredCount ||
    new Set(inventory.taskArns).size !== desiredCount ||
    new Set(inventory.tasks.map((task) => task.taskArn)).size !== desiredCount
  ) {
    throw new Error("running ECS task enumeration is incomplete or duplicated");
  }
  const listed = [...inventory.taskArns].sort();
  const described = inventory.tasks.map((task) => task.taskArn).sort();
  if (canonicalJson(listed) !== canonicalJson(described)) {
    throw new Error("described ECS tasks differ from complete running task list");
  }
  for (const task of inventory.tasks) {
    const container = task.containers?.find((entry) => entry.name === CONTAINER);
    if (
      !RUNNING_TASK_ARN_PATTERN.test(task.taskArn || "") ||
      !task.clusterArn?.endsWith(`/${CLUSTER}`) ||
      task.group !== `service:${SERVICE}` ||
      task.taskDefinitionArn !== taskDefinition ||
      task.desiredStatus !== "RUNNING" ||
      task.lastStatus !== "RUNNING" ||
      task.containers?.length !== 1 ||
      container?.taskArn !== task.taskArn ||
      container?.lastStatus !== "RUNNING" ||
      container?.exitCode !== undefined ||
      container?.reason
    ) {
      throw new Error(
        "a running service task does not use the exact candidate revision",
      );
    }
  }
  return {
    complete: true,
    taskArns: listed,
    logStreams: inventory.tasks
      .map((task) => taskLogStream(task.taskArn))
      .sort(),
  };
}

function enumerateRunningServiceTasks(taskDefinition, desiredCount) {
  const taskArns = [];
  let nextToken;
  do {
    const args = [
      "--cluster",
      CLUSTER,
      "--service-name",
      SERVICE,
      "--desired-status",
      "RUNNING",
      "--max-results",
      "100",
    ];
    if (nextToken) args.push("--next-token", nextToken);
    const page = awsJson("ecs", "list-tasks", args);
    if (!Array.isArray(page.taskArns)) {
      throw new Error("ECS running task list is malformed");
    }
    taskArns.push(...page.taskArns);
    nextToken = page.nextToken;
  } while (nextToken);

  const tasks = [];
  for (let offset = 0; offset < taskArns.length; offset += 100) {
    const page = awsJson("ecs", "describe-tasks", [
      "--cluster",
      CLUSTER,
      "--tasks",
      ...taskArns.slice(offset, offset + 100),
    ]);
    if (page.failures?.length !== 0 || !Array.isArray(page.tasks)) {
      throw new Error("ECS could not describe every running service task");
    }
    tasks.push(...page.tasks);
  }
  const inventory = { complete: true, taskArns, tasks };
  validateRunningTaskInventory(inventory, { taskDefinition, desiredCount });
  return inventory;
}

function runningTaskEvidence(inventory) {
  return inventory.tasks
    .map((task) => ({
      taskArn: task.taskArn,
      taskDefinitionArn: task.taskDefinitionArn,
      logStreamName: taskLogStream(task.taskArn),
    }))
    .sort((left, right) => left.taskArn.localeCompare(right.taskArn));
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
    !task.clusterArn?.endsWith(`/${CLUSTER}`) ||
    task.taskDefinitionArn !== newTaskDefinition ||
    task.lastStatus !== "STOPPED" ||
    task.stopCode !== "EssentialContainerExited" ||
    task.containers?.length !== 1 ||
    container?.exitCode !== 0 ||
    container?.reason
  ) {
    throw new Error("one-off rollout task did not pass with one clean container");
  }
  return taskId(taskArn);
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
    event.mcp?.protocolVersion !== "2025-03-26" ||
    event.mcp?.toolCount !== expectedNames.length ||
    event.mcp?.toolNamesSha256 !== compactJsonSha256(expectedNames) ||
    event.bedrock?.request !== "Converse" ||
    event.bedrock?.passed !== true ||
    ![
      "ECS_CONTAINER_CREDENTIALS_RELATIVE_URI",
      "ECS_CONTAINER_CREDENTIALS_FULL_URI",
    ].includes(event.bedrock?.credentialSource) ||
    event.bedrock?.modelId !== BEDROCK_MODEL
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
  const nonce = randomBytes(12).toString("hex");
  const firstFragment = `OPENCLAW_CANARY_${nonce.slice(0, 12)}`;
  const secondFragment = nonce.slice(12);
  const token = `${firstFragment}${secondFragment}`;
  const prompt =
    `<@${secret.botUserId}> deployment canary. Reply with only the ` +
    "concatenation of fragment A and fragment B, with no separator or other " +
    `text. Fragment A: ${firstFragment}; fragment B: ${secondFragment}`;
  if (prompt.includes(token)) {
    throw new Error("Slack response token must not appear in the canary prompt");
  }
  const posted = await slackApi("chat.postMessage", secret.userToken, {
    channel: secret.channelId,
    text: prompt,
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
          token,
          publicResult: {
            connected: true,
            mentionReplyExact: true,
            postedTs: posted.ts,
            replyTs: matching.ts,
            tokenSha256: sha256(token),
            correlationSha256: sha256(`${receiptId}:${token}:${matching.ts}`),
            responseTokenAbsentFromPrompt: true,
          },
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

export function validateSlackLogCorrelation(
  correlation,
  { runningInventory, slack },
) {
  const candidate = runningInventory.tasks.find(
    (task) => task.taskArn === correlation?.taskArn,
  );
  const postedMs = Math.floor(Number(slack?.postedTs) * 1000);
  const replyMs = Math.floor(Number(slack?.replyTs) * 1000);
  if (
    !candidate ||
    slack?.connected !== true ||
    slack.mentionReplyExact !== true ||
    slack.responseTokenAbsentFromPrompt !== true ||
    !SHA256_PATTERN.test(slack.tokenSha256 || "") ||
    correlation?.matched !== true ||
    correlation.logStreamName !== taskLogStream(candidate.taskArn) ||
    correlation.tokenSha256 !== slack.tokenSha256 ||
    typeof correlation.eventId !== "string" ||
    correlation.eventId.length === 0 ||
    !Number.isInteger(correlation.eventTimestamp) ||
    !Number.isFinite(postedMs) ||
    !Number.isFinite(replyMs) ||
    replyMs < postedMs ||
    correlation.eventTimestamp < replyMs - 5000 ||
    correlation.eventTimestamp > replyMs + 60_000
  ) {
    throw new Error(
      "Slack reply is not correlated to a candidate service task log stream",
    );
  }
  return true;
}

function fetchSlackLogCorrelation({ token, slack, runningInventory }) {
  const streams = runningInventory.tasks.map((task) =>
    taskLogStream(task.taskArn),
  );
  const streamToTask = new Map(
    runningInventory.tasks.map((task) => [
      taskLogStream(task.taskArn),
      task.taskArn,
    ]),
  );
  const startTime = Math.floor(Number(slack.replyTs) * 1000) - 5000;
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    const events = [];
    let nextToken;
    do {
      const args = [
        "--log-group-name",
        LOG_GROUP,
        "--log-stream-names",
        ...streams,
        "--start-time",
        String(startTime),
        "--end-time",
        String(Date.now() + 5000),
        "--filter-pattern",
        `"${token}"`,
      ];
      if (nextToken) args.push("--next-token", nextToken);
      const page = awsJson("logs", "filter-log-events", args);
      if (!Array.isArray(page.events)) {
        throw new Error("CloudWatch Slack correlation result is malformed");
      }
      events.push(...page.events);
      const following = page.nextToken;
      if (!following || following === nextToken) break;
      nextToken = following;
    } while (nextToken);

    const matching = events.filter(
      (event) =>
        streamToTask.has(event.logStreamName) &&
        typeof event.message === "string" &&
        event.message.includes(token),
    );
    const matchedStreams = [...new Set(matching.map((event) => event.logStreamName))];
    if (matchedStreams.length === 1) {
      const event = matching
        .filter((entry) => entry.logStreamName === matchedStreams[0])
        .sort((left, right) => left.timestamp - right.timestamp)[0];
      const result = {
        matched: true,
        taskArn: streamToTask.get(event.logStreamName),
        logStreamName: event.logStreamName,
        eventId: event.eventId,
        eventTimestamp: event.timestamp,
        tokenSha256: sha256(token),
      };
      validateSlackLogCorrelation(result, {
        runningInventory,
        slack,
      });
      return result;
    }
    if (matchedStreams.length > 1) {
      throw new Error(
        "Slack canary token appeared in multiple candidate task log streams",
      );
    }
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 2000);
  }
  throw new Error(
    "Slack reply was not observed in a candidate CloudWatch log stream",
  );
}

function isEnabledEnvironmentValue(value) {
  return value === "1" || value === "true";
}

export function deriveExpectedToolNames(scope, environment) {
  if (
    scope?.schemaVersion !== 2 ||
    !Array.isArray(scope.tools) ||
    !environment ||
    typeof environment !== "object" ||
    Array.isArray(environment)
  ) {
    throw new Error("reviewed MCP tool scope or task environment is invalid");
  }
  const reviewedNames = new Set();
  const names = [];
  for (const tool of scope.tools) {
    const activation = tool?.enabledBy;
    if (
      typeof tool?.name !== "string" ||
      tool.name.length === 0 ||
      reviewedNames.has(tool.name) ||
      !activation ||
      typeof activation !== "object" ||
      Array.isArray(activation)
    ) {
      throw new Error("reviewed MCP tool scope contains an invalid tool entry");
    }
    reviewedNames.add(tool.name);
    if (
      activation.kind === "always" &&
      Object.keys(activation).length === 1
    ) {
      names.push(tool.name);
      continue;
    }
    if (
      activation.kind === "never" &&
      Object.keys(activation).length === 1
    ) {
      continue;
    }
    if (
      activation.kind === "envAllTrue" &&
      Object.keys(activation).length === 2 &&
      Array.isArray(activation.names) &&
      activation.names.length > 0 &&
      activation.names.every(
        (name) =>
          typeof name === "string" && /^[A-Z][A-Z0-9_]*$/u.test(name),
      ) &&
      activation.names.length === new Set(activation.names).size
    ) {
      if (
        activation.names.every((name) =>
          isEnabledEnvironmentValue(environment[name]),
        )
      ) {
        names.push(tool.name);
      }
      continue;
    }
    throw new Error("reviewed MCP tool scope contains an invalid activation rule");
  }
  if (reviewedNames.size === 0 || names.length === 0) {
    throw new Error("derived MCP expected tool set is empty");
  }
  return names.sort();
}

export function readExpectedToolNames(
  mcpTaskDefinition,
  expectedTaskDefinitionArn,
) {
  const scope = checkedJsonFile(TOOL_SCOPE_PATH);
  const task = mcpTaskDefinition?.taskDefinition;
  const containers = task?.containerDefinitions?.filter(
    (container) => container?.name === MCP_CONTAINER,
  );
  if (
    task?.taskDefinitionArn === undefined ||
    !MCP_TASK_ARN_PATTERN.test(task.taskDefinitionArn) ||
    (expectedTaskDefinitionArn !== undefined &&
      task.taskDefinitionArn !== expectedTaskDefinitionArn) ||
    task.family !== MCP_FAMILY ||
    task.status !== "ACTIVE" ||
    containers?.length !== 1 ||
    !Array.isArray(containers[0].environment)
  ) {
    throw new Error("candidate MCP task definition is invalid");
  }
  const environment = {};
  for (const entry of containers[0].environment) {
    if (
      typeof entry?.name !== "string" ||
      typeof entry?.value !== "string" ||
      Object.hasOwn(environment, entry.name)
    ) {
      throw new Error("candidate MCP task definition environment is invalid");
    }
    environment[entry.name] = entry.value;
  }
  return deriveExpectedToolNames(scope, environment);
}

function fetchTaskCanaryEvent(task, receiptId, expectedToolNames) {
  const streamName = taskLogStream(task);
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
      validateTaskCanaryEvent(candidates[0], {
        receiptId,
        expectedToolNames,
      });
      return candidates[0];
    }
    if (candidates.length > 1) {
      throw new Error("rollout task emitted duplicate success events");
    }
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 2000);
  }
  throw new Error("rollout task success event was not delivered to CloudWatch Logs");
}

function validateDurablePreviousTaskDefinition(document, previous) {
  const task = document?.taskDefinition;
  if (
    task?.taskDefinitionArn !== previous ||
    task.family !== FAMILY ||
    task.status !== "ACTIVE" ||
    task.requiresCompatibilities?.includes("FARGATE") !== true ||
    task.containerDefinitions?.filter((entry) => entry.name === CONTAINER)
      .length !== 1
  ) {
    throw new Error("durable previous OpenClaw task revision is not restorable");
  }
}

async function restoreAndVerify(context) {
  assertAutomationCaller(awsJson("sts", "get-caller-identity"));
  validateConsumption(context.consumption, context);
  validateLiveConsumption(context.consumption, context);

  const serviceBefore = describeService();
  const observed = serviceBefore?.services?.[0]?.taskDefinition;
  if (!TASK_ARN_PATTERN.test(observed || "")) {
    throw new Error("could not identify the current OpenClaw task revision");
  }
  if (context.newTaskDefinition === "AUTO") {
    context = { ...context, newTaskDefinition: observed };
  }
  validateDurablePreviousTaskDefinition(
    awsJson("ecs", "describe-task-definition", [
      "--task-definition",
      context.previousTaskDefinition,
    ]),
    context.previousTaskDefinition,
  );
  if (context.newTaskDefinition === context.previousTaskDefinition) {
    awsWait("ecs", "services-stable", [
      "--cluster",
      CLUSTER,
      "--services",
      SERVICE,
    ]);
    const stable = validateStableService(
      describeService(),
      context.previousTaskDefinition,
    );
    const inventory = enumerateRunningServiceTasks(
      context.previousTaskDefinition,
      stable.desiredCount,
    );
    return {
      required: false,
      restored: true,
      previousTaskDefinitionArn: context.previousTaskDefinition,
      observedCandidateTaskDefinitionArn: context.newTaskDefinition,
      runningTaskArns: inventory.taskArns.sort(),
    };
  }
  validateDistinctRevisions(
    context.previousTaskDefinition,
    context.newTaskDefinition,
  );
  if (
    observed !== context.newTaskDefinition &&
    observed !== context.previousTaskDefinition
  ) {
    throw new Error(
      "OpenClaw service changed to an unbound third revision during rollback",
    );
  }
  const authorization = consumeRollbackAuthorization(context);
  if (observed === context.newTaskDefinition) {
    awsJson("ecs", "update-service", [
      "--cluster",
      CLUSTER,
      "--service",
      SERVICE,
      "--task-definition",
      context.previousTaskDefinition,
      "--force-new-deployment",
    ]);
  }
  awsWait("ecs", "services-stable", [
    "--cluster",
    CLUSTER,
    "--services",
    SERVICE,
  ]);
  const stable = validateStableService(
    describeService(),
    context.previousTaskDefinition,
  );
  const inventory = enumerateRunningServiceTasks(
    context.previousTaskDefinition,
    stable.desiredCount,
  );
  return {
    required: true,
    restored: true,
    durablePreviousRevisionVerified: true,
    previousTaskDefinitionArn: context.previousTaskDefinition,
    failedCandidateTaskDefinitionArn: context.newTaskDefinition,
    runningTaskArns: inventory.taskArns.sort(),
    rollbackAuthorization: authorization,
  };
}

function describeFixedKmsKey(alias, usage, keySpec, expectedArn) {
  const response = awsJson("kms", "describe-key", ["--key-id", alias]);
  const metadata = response.KeyMetadata;
  if (
    !KMS_ARN_PATTERN.test(metadata?.Arn || "") ||
    metadata.Arn !== expectedArn ||
    metadata.KeyUsage !== usage ||
    metadata.KeySpec !== keySpec ||
    metadata.KeyState !== "Enabled" ||
    metadata.Enabled !== true ||
    metadata.MultiRegion !== false
  ) {
    throw new Error(`fixed KMS key is not enabled for ${usage}`);
  }
  return metadata.Arn;
}

function verifyEvidenceInfrastructure(expectedKmsKeys) {
  describeFixedKmsKey(
    EVIDENCE_ENCRYPTION_ALIAS,
    "ENCRYPT_DECRYPT",
    "SYMMETRIC_DEFAULT",
    expectedKmsKeys.encryption,
  );
  describeFixedKmsKey(
    EVIDENCE_SIGNING_ALIAS,
    "SIGN_VERIFY",
    "RSA_3072",
    expectedKmsKeys.signing,
  );
  const versioning = awsJson("s3api", "get-bucket-versioning", [
    "--bucket",
    EVIDENCE_BUCKET,
    "--expected-bucket-owner",
    ACCOUNT,
  ]);
  const objectLock = awsJson(
    "s3api",
    "get-object-lock-configuration",
    ["--bucket", EVIDENCE_BUCKET, "--expected-bucket-owner", ACCOUNT],
  ).ObjectLockConfiguration;
  const encryption = awsJson(
    "s3api",
    "get-bucket-encryption",
    ["--bucket", EVIDENCE_BUCKET, "--expected-bucket-owner", ACCOUNT],
  ).ServerSideEncryptionConfiguration;
  const encryptionRule = encryption?.Rules?.[0];
  if (
    versioning.Status !== "Enabled" ||
    objectLock?.ObjectLockEnabled !== "Enabled" ||
    objectLock.Rule?.DefaultRetention?.Mode !== "COMPLIANCE" ||
    objectLock.Rule.DefaultRetention.Days !== RETENTION_DAYS ||
    encryption?.Rules?.length !== 1 ||
    encryptionRule.ApplyServerSideEncryptionByDefault?.SSEAlgorithm !==
      "aws:kms" ||
    encryptionRule.ApplyServerSideEncryptionByDefault?.KMSMasterKeyID !==
      expectedKmsKeys.encryption ||
    encryptionRule.BucketKeyEnabled !== true
  ) {
    throw new Error(
      "fixed rollout evidence bucket Versioning/Object Lock/KMS controls differ",
    );
  }
}

function putImmutableObject({ key, bodyPath, contentType, encryptionKeyArn }) {
  const retainUntil = new Date(
    Date.now() + RETENTION_DAYS * 24 * 60 * 60 * 1000,
  ).toISOString();
  const response = awsJson("s3api", "put-object", [
    "--bucket",
    EVIDENCE_BUCKET,
    "--key",
    key,
    "--body",
    bodyPath,
    "--expected-bucket-owner",
    ACCOUNT,
    "--content-type",
    contentType,
    "--server-side-encryption",
    "aws:kms",
    "--ssekms-key-id",
    encryptionKeyArn,
    "--object-lock-mode",
    "COMPLIANCE",
    "--object-lock-retain-until-date",
    retainUntil,
    "--if-none-match",
    "*",
  ]);
  if (!validS3VersionId(response.VersionId)) {
    throw new Error("immutable rollout result has no fixed S3 VersionId");
  }
  return { versionId: response.VersionId, retainUntil };
}

function verifyImmutableObject({
  key,
  versionId,
  expectedBytes,
  encryptionKeyArn,
  outputPath,
}) {
  const head = awsJson("s3api", "head-object", [
    "--bucket",
    EVIDENCE_BUCKET,
    "--key",
    key,
    "--version-id",
    versionId,
    "--expected-bucket-owner",
    ACCOUNT,
  ]);
  const retained = Date.parse(head.ObjectLockRetainUntilDate || "");
  if (
    head.VersionId !== versionId ||
    head.ObjectLockMode !== "COMPLIANCE" ||
    !Number.isFinite(retained) ||
    retained <= Date.now() ||
    head.ServerSideEncryption !== "aws:kms" ||
    head.SSEKMSKeyId !== encryptionKeyArn ||
    head.ContentLength !== expectedBytes.length
  ) {
    throw new Error(
      "immutable rollout object failed VersionId/Object Lock/KMS verification",
    );
  }
  const downloaded = awsDownload({
    bucket: EVIDENCE_BUCKET,
    key,
    versionId,
    output: outputPath,
  });
  const bytes = fs.readFileSync(outputPath);
  if (
    downloaded.VersionId !== versionId ||
    downloaded.ObjectLockMode !== "COMPLIANCE" ||
    downloaded.ServerSideEncryption !== "aws:kms" ||
    downloaded.SSEKMSKeyId !== encryptionKeyArn ||
    !bytes.equals(expectedBytes)
  ) {
    throw new Error(
      "exact immutable rollout object download differs from signed bytes",
    );
  }
  return {
    versionId,
    sha256: sha256(bytes),
    objectLockMode: head.ObjectLockMode,
    objectLockRetainUntil: head.ObjectLockRetainUntilDate,
    encryptionKmsKeyArn: head.SSEKMSKeyId,
  };
}

export function validateImmutableEvidence(
  evidence,
  persistedResult,
  expectedKmsKeys,
) {
  const outcome =
    persistedResult?.passed === true
      ? "passed"
      : persistedResult?.passed === false
        ? "failed"
        : "";
  const resultRetainedUntil = Date.parse(
    evidence?.resultObjectLockRetainUntil || "",
  );
  const signatureRetainedUntil = Date.parse(
    evidence?.signatureObjectLockRetainUntil || "",
  );
  const minimumRetentionEpoch =
    (persistedResult?.producedAtEpoch + RETENTION_DAYS * 24 * 60 * 60 - 300) *
    1000;
  if (
    evidence?.verified !== true ||
    evidence.bucket !== EVIDENCE_BUCKET ||
    evidence.resultKey !==
      `${RESULT_PREFIX}/${persistedResult.applyAttemptId}/${outcome}/result.json` ||
    evidence.signatureKey !==
      `${RESULT_PREFIX}/${persistedResult.applyAttemptId}/${outcome}/result.sig.json` ||
    !validS3VersionId(evidence.resultVersionId) ||
    !validS3VersionId(evidence.signatureVersionId) ||
    !SHA256_PATTERN.test(evidence.resultSha256 || "") ||
    evidence.resultSha256 !== canonicalSha256(persistedResult) ||
    !SHA256_PATTERN.test(evidence.signatureSha256 || "") ||
    evidence.resultObjectLockMode !== "COMPLIANCE" ||
    evidence.signatureObjectLockMode !== "COMPLIANCE" ||
    !Number.isInteger(persistedResult?.producedAtEpoch) ||
    persistedResult.producedAtEpoch > Math.floor(Date.now() / 1000) + 300 ||
    !Number.isFinite(resultRetainedUntil) ||
    !Number.isFinite(signatureRetainedUntil) ||
    resultRetainedUntil <= Date.now() ||
    signatureRetainedUntil <= Date.now() ||
    resultRetainedUntil < minimumRetentionEpoch ||
    signatureRetainedUntil < minimumRetentionEpoch ||
    !KMS_ARN_PATTERN.test(evidence.encryptionKmsKeyArn || "") ||
    !KMS_ARN_PATTERN.test(evidence.signingKmsKeyArn || "") ||
    evidence.encryptionKmsKeyArn !== expectedKmsKeys?.encryption ||
    evidence.signingKmsKeyArn !== expectedKmsKeys?.signing ||
    evidence.encryptionKmsAlias !== EVIDENCE_ENCRYPTION_ALIAS ||
    evidence.signingKmsAlias !== EVIDENCE_SIGNING_ALIAS ||
    evidence.signingAlgorithm !== SIGNING_ALGORITHM ||
    evidence.signatureValid !== true ||
    evidence.exactVersionDownloadsVerified !== true
  ) {
    throw new Error(
      "signed immutable rollout evidence does not bind the persisted result",
    );
  }
  return true;
}

function validatePersistedResultIdentity(result) {
  if (
    result?.schemaVersion !== 2 ||
    (result.passed !== true && result.passed !== false) ||
    result.account !== ACCOUNT ||
    result.region !== REGION ||
    result.cluster !== CLUSTER ||
    result.service !== SERVICE ||
    result.taskFamily !== FAMILY ||
    result.automationRoleArn !== AUTOMATION_ROLE_ARN ||
    !UUID_PATTERN.test(result.intentId || "") ||
    !UUID_PATTERN.test(result.applyAttemptId || "") ||
    result.intentId === result.applyAttemptId ||
    !SHA256_PATTERN.test(result.planSha256 || "") ||
    !Number.isInteger(result.producedAtEpoch) ||
    !TASK_ARN_PATTERN.test(result.previousTaskDefinitionArn || "") ||
    !TASK_ARN_PATTERN.test(result.newTaskDefinitionArn || "") ||
    result.previousTaskDefinitionArn === result.newTaskDefinitionArn
  ) {
    throw new Error(
      "persisted rollout result does not exactly bind the trusted apply identity",
    );
  }
}

function validatePersistedRunningTaskClaims(claims, candidateTaskDefinition) {
  if (
    claims?.complete !== true ||
    claims.exactCandidateRevision !== true ||
    !Array.isArray(claims.taskArns) ||
    !Array.isArray(claims.tasks) ||
    claims.taskArns.length !== 1 ||
    claims.tasks.length !== 1 ||
    claims.tasks[0].taskArn !== claims.taskArns[0] ||
    !RUNNING_TASK_ARN_PATTERN.test(claims.tasks[0].taskArn || "") ||
    claims.tasks[0].taskDefinitionArn !== candidateTaskDefinition ||
    claims.tasks[0].logStreamName !== taskLogStream(claims.tasks[0].taskArn)
  ) {
    throw new Error(
      "persisted rollout task claims do not enumerate the exact candidate",
    );
  }
}

function validatePersistedSuccessClaims(result, context) {
  validatePersistedResultIdentity(result);
  validatePersistedRunningTaskClaims(
    result.runningTasksBeforeSlack,
    context.newTaskDefinition,
  );
  validatePersistedRunningTaskClaims(
    result.runningTasksAfterSlack,
    context.newTaskDefinition,
  );
  const correlation = result.slack?.candidateLogCorrelation;
  const correlatedTask = result.runningTasksBeforeSlack.tasks[0];
  const authorization = result.rollbackAuthorization;
  if (
    result.passed !== true ||
    result.distinctTaskRevisions !== true ||
    result.ecsServiceStable !== true ||
    result.circuitBreakerRollbackEnabled !== true ||
    result.slack?.mentionReplyExact !== true ||
    result.slack?.responseTokenAbsentFromPrompt !== true ||
    correlation?.matched !== true ||
    correlation.taskArn !== correlatedTask.taskArn ||
    correlation.logStreamName !== correlatedTask.logStreamName ||
    authorization?.recordId !== rollbackRecordId(context.applyAttemptId) ||
    authorization.state !== "AUTHORIZED" ||
    authorization.oneUse !== true ||
    authorization.intentId !== context.intentId ||
    authorization.applyAttemptId !== context.applyAttemptId ||
    authorization.planSha256 !== context.planSha256 ||
    authorization.previousTaskDefinitionArn !==
      context.previousTaskDefinition ||
    authorization.newTaskDefinitionArn !== context.newTaskDefinition
  ) {
    throw new Error(
      "persisted successful rollout claims do not bind tasks/logs/rollback",
    );
  }
}

function persistSignedResult(result, expectedKmsKeys) {
  validatePersistedResultIdentity(result);
  const temporary = fs.mkdtempSync(
    resolve(os.tmpdir(), "teamagent-openclaw-rollout."),
  );
  try {
    const encryptionKeyArn = describeFixedKmsKey(
      EVIDENCE_ENCRYPTION_ALIAS,
      "ENCRYPT_DECRYPT",
      "SYMMETRIC_DEFAULT",
      expectedKmsKeys.encryption,
    );
    const signingKeyArn = describeFixedKmsKey(
      EVIDENCE_SIGNING_ALIAS,
      "SIGN_VERIFY",
      "RSA_3072",
      expectedKmsKeys.signing,
    );
    const resultBytes = canonicalBytes(result);
    const resultSha256 = sha256(resultBytes);
    const digest = Buffer.from(resultSha256, "hex");
    const resultPath = resolve(temporary, "result.json");
    const digestPath = resolve(temporary, "result.sha256");
    const signaturePath = resolve(temporary, "result.sig");
    const envelopePath = resolve(temporary, "result.sig.json");
    fs.writeFileSync(resultPath, resultBytes, { mode: 0o600, flag: "wx" });
    fs.writeFileSync(digestPath, digest, { mode: 0o600, flag: "wx" });
    const signed = awsJson("kms", "sign", [
      "--key-id",
      EVIDENCE_SIGNING_ALIAS,
      "--message",
      `fileb://${digestPath}`,
      "--message-type",
      "DIGEST",
      "--signing-algorithm",
      SIGNING_ALGORITHM,
    ]);
    if (
      signed.KeyId !== signingKeyArn ||
      signed.SigningAlgorithm !== SIGNING_ALGORITHM ||
      typeof signed.Signature !== "string"
    ) {
      throw new Error("KMS returned an unexpected rollout signature");
    }
    const signatureBytes = Buffer.from(signed.Signature, "base64");
    if (signatureBytes.length < 256) {
      throw new Error("KMS rollout signature is malformed");
    }
    fs.writeFileSync(signaturePath, signatureBytes, {
      mode: 0o600,
      flag: "wx",
    });
    const envelope = {
      schemaVersion: 1,
      applyAttemptId: result.applyAttemptId,
      previousTaskDefinitionArn: result.previousTaskDefinitionArn,
      newTaskDefinitionArn: result.newTaskDefinitionArn,
      resultSha256,
      signingKmsKeyArn: signingKeyArn,
      signingAlgorithm: SIGNING_ALGORITHM,
      signatureBase64: signed.Signature,
    };
    const envelopeBytes = canonicalBytes(envelope);
    fs.writeFileSync(envelopePath, envelopeBytes, {
      mode: 0o600,
      flag: "wx",
    });

    const outcome = result.passed ? "passed" : "failed";
    const resultKey =
      `${RESULT_PREFIX}/${result.applyAttemptId}/${outcome}/result.json`;
    const signatureKey =
      `${RESULT_PREFIX}/${result.applyAttemptId}/${outcome}/result.sig.json`;
    const resultPut = putImmutableObject({
      key: resultKey,
      bodyPath: resultPath,
      contentType: "application/json",
      encryptionKeyArn,
    });
    const signaturePut = putImmutableObject({
      key: signatureKey,
      bodyPath: envelopePath,
      contentType: "application/json",
      encryptionKeyArn,
    });
    const verifiedResult = verifyImmutableObject({
      key: resultKey,
      versionId: resultPut.versionId,
      expectedBytes: resultBytes,
      encryptionKeyArn,
      outputPath: resolve(temporary, "downloaded-result.json"),
    });
    const verifiedSignature = verifyImmutableObject({
      key: signatureKey,
      versionId: signaturePut.versionId,
      expectedBytes: envelopeBytes,
      encryptionKeyArn,
      outputPath: resolve(temporary, "downloaded-result.sig.json"),
    });
    const verification = awsJson("kms", "verify", [
      "--key-id",
      EVIDENCE_SIGNING_ALIAS,
      "--message",
      `fileb://${digestPath}`,
      "--message-type",
      "DIGEST",
      "--signature",
      `fileb://${signaturePath}`,
      "--signing-algorithm",
      SIGNING_ALGORITHM,
    ]);
    if (
      verification.KeyId !== signingKeyArn ||
      verification.SignatureValid !== true ||
      verification.SigningAlgorithm !== SIGNING_ALGORITHM
    ) {
      throw new Error("KMS rollout result signature verification failed");
    }
    const evidence = {
      verified: true,
      bucket: EVIDENCE_BUCKET,
      resultKey,
      resultVersionId: verifiedResult.versionId,
      resultSha256: verifiedResult.sha256,
      resultObjectLockMode: verifiedResult.objectLockMode,
      resultObjectLockRetainUntil: verifiedResult.objectLockRetainUntil,
      signatureKey,
      signatureVersionId: verifiedSignature.versionId,
      signatureSha256: verifiedSignature.sha256,
      signatureObjectLockMode: verifiedSignature.objectLockMode,
      signatureObjectLockRetainUntil:
        verifiedSignature.objectLockRetainUntil,
      encryptionKmsAlias: EVIDENCE_ENCRYPTION_ALIAS,
      encryptionKmsKeyArn,
      signingKmsAlias: EVIDENCE_SIGNING_ALIAS,
      signingKmsKeyArn: signingKeyArn,
      signingAlgorithm: SIGNING_ALGORITHM,
      signatureValid: true,
      exactVersionDownloadsVerified: true,
    };
    validateImmutableEvidence(evidence, result, expectedKmsKeys);
    return evidence;
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
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

function parseExactArguments(args, allowed) {
  const values = new Map();
  for (let index = 0; index < args.length; index += 2) {
    if (!args[index]?.startsWith("--") || args[index + 1] === undefined) {
      throw new Error("rollout gate arguments must be exact --name value pairs");
    }
    if (values.has(args[index])) {
      throw new Error(`duplicate argument: ${args[index]}`);
    }
    values.set(args[index], args[index + 1]);
  }
  if (
    values.size !== allowed.length ||
    allowed.some((name) => !values.has(name)) ||
    [...values.keys()].some((name) => !allowed.includes(name))
  ) {
    throw new Error(`required arguments: ${allowed.join(" ")}`);
  }
  return values;
}

function contextFromArguments(values, { allowAutoCandidate = false } = {}) {
  const previousTaskDefinition = values.get("--previous-task-definition");
  const newTaskDefinition = values.get("--new-task-definition");
  const applyAttemptId = values.get("--apply-attempt-id");
  const planSha256 = values.get("--plan-sha256");
  const consumption = checkedJsonFile(values.get("--receipt-consumption"));
  const encryptionKmsKeyArn = values.get(
    "--evidence-encryption-kms-key-arn",
  );
  const signingKmsKeyArn = values.get("--evidence-signing-kms-key-arn");
  const mcpTaskDefinition = values.get("--mcp-task-definition");
  if (
    !TASK_ARN_PATTERN.test(previousTaskDefinition || "") ||
    (!TASK_ARN_PATTERN.test(newTaskDefinition || "") &&
      !(allowAutoCandidate && newTaskDefinition === "AUTO")) ||
    (!allowAutoCandidate &&
      (!KMS_ARN_PATTERN.test(encryptionKmsKeyArn || "") ||
        !KMS_ARN_PATTERN.test(signingKmsKeyArn || "") ||
        encryptionKmsKeyArn === signingKmsKeyArn)) ||
    (!allowAutoCandidate && !MCP_TASK_ARN_PATTERN.test(mcpTaskDefinition || ""))
  ) {
    throw new Error("rollout task definition arguments are invalid");
  }
  const intentId = validateConsumption(consumption, {
    applyAttemptId,
    planSha256,
  });
  return {
    previousTaskDefinition,
    newTaskDefinition,
    applyAttemptId,
    planSha256,
    consumption,
    intentId,
    expectedKmsKeys: {
      encryption: encryptionKmsKeyArn,
      signing: signingKmsKeyArn,
    },
    mcpTaskDefinition,
  };
}

async function runLive(args) {
  const allowed = [
    "--new-task-definition",
    "--previous-task-definition",
    "--receipt-consumption",
    "--apply-attempt-id",
    "--plan-sha256",
    "--evidence-encryption-kms-key-arn",
    "--evidence-signing-kms-key-arn",
    "--mcp-task-definition",
    "--output",
  ];
  const values = parseExactArguments(args, allowed);
  const context = contextFromArguments(values);
  validateDistinctRevisions(
    context.previousTaskDefinition,
    context.newTaskDefinition,
  );
  const output = values.get("--output");
  const automationRoleArn = assertAutomationCaller(
    awsJson("sts", "get-caller-identity"),
  );

  try {
    validateLiveConsumption(context.consumption, context);
    const rollbackAuthorization = prepareRollbackAuthorization(context);
    if (rollbackAuthorization.state !== "AUTHORIZED") {
      throw new Error("rollback authorization was already consumed");
    }
    verifyEvidenceInfrastructure(context.expectedKmsKeys);
    awsWait("ecs", "services-stable", [
      "--cluster",
      CLUSTER,
      "--services",
      SERVICE,
    ]);
    const serviceDocument = describeService();
    const stable = validateStableService(
      serviceDocument,
      context.newTaskDefinition,
    );
    const runningBefore = enumerateRunningServiceTasks(
      context.newTaskDefinition,
      stable.desiredCount,
    );
    const expectedToolNames = readExpectedToolNames(
      awsJson("ecs", "describe-task-definition", [
        "--task-definition",
        context.mcpTaskDefinition,
      ]),
      context.mcpTaskDefinition,
    );
    const receiptId = context.intentId;
    const overrides = {
      containerOverrides: [
        {
          name: CONTAINER,
          command: [
            "/opt/teamagent/rollout-task-canary.mjs",
            "--receipt-id",
            receiptId,
            "--expected-tool-names-json",
            JSON.stringify(expectedToolNames),
          ],
        },
      ],
    };
    const runTask = awsJson("ecs", "run-task", [
      "--cluster",
      CLUSTER,
      "--task-definition",
      context.newTaskDefinition,
      "--launch-type",
      "FARGATE",
      "--count",
      "1",
      "--network-configuration",
      JSON.stringify({ awsvpcConfiguration: stable.awsvpc }),
      "--overrides",
      JSON.stringify(overrides),
      "--started-by",
      `openclaw-canary-${sha256(receiptId).slice(0, 16)}`,
    ]);
    if (runTask.failures?.length !== 0 || runTask.tasks?.length !== 1) {
      throw new Error("ECS rejected the one-off rollout canary task");
    }
    const canaryTaskArn = runTask.tasks[0].taskArn;
    awsWait("ecs", "tasks-stopped", [
      "--cluster",
      CLUSTER,
      "--tasks",
      canaryTaskArn,
    ]);
    const taskDocument = awsJson("ecs", "describe-tasks", [
      "--cluster",
      CLUSTER,
      "--tasks",
      canaryTaskArn,
    ]);
    validateStoppedCanaryTask(taskDocument, {
      taskArn: canaryTaskArn,
      newTaskDefinition: context.newTaskDefinition,
    });
    const taskEvent = fetchTaskCanaryEvent(
      canaryTaskArn,
      receiptId,
      expectedToolNames,
    );

    const secretResult = awsJson("secretsmanager", "get-secret-value", [
      "--secret-id",
      CANARY_SECRET,
      "--version-stage",
      "AWSCURRENT",
    ]);
    if (typeof secretResult.SecretString !== "string") {
      throw new Error("Slack rollout secret is not a JSON SecretString");
    }
    const slackPrivate = await verifySlackMentionReply(
      validateSlackSecret(JSON.parse(secretResult.SecretString)),
      receiptId,
    );
    const logCorrelation = fetchSlackLogCorrelation({
      token: slackPrivate.token,
      slack: slackPrivate.publicResult,
      runningInventory: runningBefore,
    });

    awsWait("ecs", "services-stable", [
      "--cluster",
      CLUSTER,
      "--services",
      SERVICE,
    ]);
    const stableAfterSlack = validateStableService(
      describeService(),
      context.newTaskDefinition,
    );
    const runningAfter = enumerateRunningServiceTasks(
      context.newTaskDefinition,
      stableAfterSlack.desiredCount,
    );
    const persistedResult = {
      schemaVersion: 2,
      producedAtEpoch: Math.floor(Date.now() / 1000),
      passed: true,
      account: ACCOUNT,
      region: REGION,
      cluster: CLUSTER,
      service: SERVICE,
      taskFamily: FAMILY,
      automationRoleArn,
      intentId: context.intentId,
      applyAttemptId: context.applyAttemptId,
      planSha256: context.planSha256,
      previousTaskDefinitionArn: context.previousTaskDefinition,
      newTaskDefinitionArn: context.newTaskDefinition,
      distinctTaskRevisions: true,
      ecsServiceStable: true,
      circuitBreakerRollbackEnabled: true,
      runningTasksBeforeSlack: {
        complete: true,
        taskArns: runningBefore.taskArns.sort(),
        tasks: runningTaskEvidence(runningBefore),
        exactCandidateRevision: true,
      },
      oneOffTask: {
        taskArn: canaryTaskArn,
        exactTaskDefinition: true,
        exitCode: 0,
      },
      mcp: taskEvent.mcp,
      bedrock: taskEvent.bedrock,
      slack: {
        ...slackPrivate.publicResult,
        candidateLogCorrelation: logCorrelation,
      },
      runningTasksAfterSlack: {
        complete: true,
        taskArns: runningAfter.taskArns.sort(),
        tasks: runningTaskEvidence(runningAfter),
        exactCandidateRevision: true,
      },
      rollbackAuthorization: {
        recordId: rollbackAuthorization.record_id,
        state: rollbackAuthorization.state,
        oneUse: rollbackAuthorization.one_use,
        intentId: rollbackAuthorization.intent_id,
        applyAttemptId: rollbackAuthorization.apply_attempt_id,
        planSha256: rollbackAuthorization.plan_sha256,
        previousTaskDefinitionArn:
          rollbackAuthorization.previous_task_definition_arn,
        newTaskDefinitionArn:
          rollbackAuthorization.new_task_definition_arn,
      },
    };
    validatePersistedSuccessClaims(persistedResult, context);
    const immutableEvidence = persistSignedResult(
      persistedResult,
      context.expectedKmsKeys,
    );
    const result = {
      schemaVersion: 2,
      required: true,
      passed: true,
      applyAttemptId: context.applyAttemptId,
      previousTaskDefinitionArn: context.previousTaskDefinition,
      newTaskDefinitionArn: context.newTaskDefinition,
      persistedResult,
      immutableEvidence,
    };
    writeOutput(output, result);
  } catch (error) {
    let rollback;
    let rollbackError;
    try {
      rollback = await restoreAndVerify(context);
    } catch (failure) {
      rollbackError = failure;
    }
    const failedResult = {
      schemaVersion: 2,
      producedAtEpoch: Math.floor(Date.now() / 1000),
      passed: false,
      account: ACCOUNT,
      region: REGION,
      cluster: CLUSTER,
      service: SERVICE,
      taskFamily: FAMILY,
      automationRoleArn,
      intentId: context.intentId,
      applyAttemptId: context.applyAttemptId,
      planSha256: context.planSha256,
      previousTaskDefinitionArn: context.previousTaskDefinition,
      newTaskDefinitionArn: context.newTaskDefinition,
      failure: {
        message: error instanceof Error ? error.message : String(error),
      },
      rollback: rollback || {
        restored: false,
        message:
          rollbackError instanceof Error
            ? rollbackError.message
            : String(rollbackError),
      },
    };
    try {
      const immutableEvidence = persistSignedResult(
        failedResult,
        context.expectedKmsKeys,
      );
      writeOutput(output, {
        schemaVersion: 2,
        required: true,
        passed: false,
        applyAttemptId: context.applyAttemptId,
        previousTaskDefinitionArn: context.previousTaskDefinition,
        newTaskDefinitionArn: context.newTaskDefinition,
        persistedResult: failedResult,
        immutableEvidence,
      });
    } catch {
      // The guard independently retries the idempotent restore before it
      // releases either lock. Evidence failure must never mask rollback.
    }
    if (rollbackError) {
      throw new Error(
        `rollout failed and durable rollback verification failed: ${
          rollbackError instanceof Error
            ? rollbackError.message
            : String(rollbackError)
        }`,
      );
    }
    throw new Error(
      `rollout failed; durable previous revision restored and verified: ${
        error instanceof Error ? error.message : String(error)
      }`,
    );
  }
}

async function runRestore(args) {
  const allowed = [
    "--new-task-definition",
    "--previous-task-definition",
    "--receipt-consumption",
    "--apply-attempt-id",
    "--plan-sha256",
    "--output",
  ];
  const values = parseExactArguments(args, allowed);
  const context = contextFromArguments(values, { allowAutoCandidate: true });
  const result = await restoreAndVerify(context);
  writeOutput(values.get("--output"), {
    schemaVersion: 1,
    passed: true,
    applyAttemptId: context.applyAttemptId,
    ...result,
  });
}

function validateFixture(path) {
  const fixture = checkedJsonFile(path);
  const context = {
    applyAttemptId: fixture.expected.applyAttemptId,
    planSha256: fixture.expected.planSha256,
    previousTaskDefinition: fixture.expected.previousTaskDefinition,
    newTaskDefinition: fixture.expected.newTaskDefinition,
    intentId: fixture.consumption.intent_id,
    expectedKmsKeys: {
      encryption: fixture.expected.encryptionKmsKeyArn,
      signing: fixture.expected.signingKmsKeyArn,
    },
  };
  assertAutomationCaller(fixture.caller);
  validateDistinctRevisions(
    context.previousTaskDefinition,
    context.newTaskDefinition,
  );
  validateConsumption(fixture.consumption, context);
  const stable = validateStableService(
    fixture.service,
    context.newTaskDefinition,
  );
  validateRunningTaskInventory(fixture.runningBefore, {
    taskDefinition: context.newTaskDefinition,
    desiredCount: stable.desiredCount,
  });
  validateStoppedCanaryTask(fixture.task, {
    taskArn: fixture.task.tasks[0].taskArn,
    newTaskDefinition: context.newTaskDefinition,
  });
  validateTaskCanaryEvent(fixture.taskEvent, {
    receiptId: context.intentId,
    expectedToolNames: fixture.expected.toolNames,
  });
  validateSlackLogCorrelation(fixture.slack.candidateLogCorrelation, {
    runningInventory: fixture.runningBefore,
    slack: fixture.slack,
  });
  validateRunningTaskInventory(fixture.runningAfter, {
    taskDefinition: context.newTaskDefinition,
    desiredCount: stable.desiredCount,
  });
  validateRollbackAuthorization(
    fixture.rollbackAuthorization,
    context,
    ["AUTHORIZED"],
  );
  validatePersistedResultIdentity(fixture.persistedResult);
  if (
    fixture.persistedResult.passed !== true ||
    fixture.persistedResult.intentId !== context.intentId ||
    fixture.persistedResult.applyAttemptId !== context.applyAttemptId ||
    fixture.persistedResult.planSha256 !== context.planSha256 ||
    fixture.persistedResult.previousTaskDefinitionArn !==
      context.previousTaskDefinition ||
    fixture.persistedResult.newTaskDefinitionArn !== context.newTaskDefinition
  ) {
    throw new Error("fixture persisted result does not bind the rollout context");
  }
  validatePersistedSuccessClaims(fixture.persistedResult, context);
  validateImmutableEvidence(
    fixture.immutableEvidence,
    fixture.persistedResult,
    context.expectedKmsKeys,
  );
  return {
    passed: true,
    applyAttemptId: context.applyAttemptId,
    intentId: context.intentId,
  };
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
  } else if (process.argv[2] === "--restore-and-verify") {
    runRestore(process.argv.slice(3)).catch((error) => {
      process.stderr.write(
        `${JSON.stringify({
          event: "openclaw_rollout_rollback_error",
          level: "error",
          message: error instanceof Error ? error.message : String(error),
        })}\n`,
      );
      process.exitCode = 1;
    });
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
