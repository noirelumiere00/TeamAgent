#!/usr/bin/env node

// Runs only as a one-off Fargate task after the service reaches stable.  It
// proves that the candidate task role can make a Bedrock request and that the
// live MCP server exposes exactly the reviewed/default-enabled tool set.

import { createHash } from "node:crypto";
import fs from "node:fs";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const REGION = "ap-northeast-1";
const MCP_URL = "http://teamagent-mcp.teamagent.internal:8787/mcp";
const TOOL_SCOPE_PATH = "/opt/teamagent/effective-tool-scope.json";
const CONFIG_PATH = process.env.OPENCLAW_CONFIG_PATH;
const TOKEN_PATTERN = /^[A-Za-z0-9._:-]{16,256}$/u;

function fail(message) {
  process.stderr.write(
    `${JSON.stringify({
      event: "openclaw_rollout_task_canary_error",
      level: "error",
      message,
    })}\n`,
  );
  process.exit(1);
}

function canonicalSha256(value) {
  return createHash("sha256")
    .update(JSON.stringify(value))
    .digest("hex");
}

function parseSse(text, expectedId) {
  for (const block of text.split(/\r?\n\r?\n/u)) {
    for (const line of block.split(/\r?\n/u)) {
      if (!line.startsWith("data:")) continue;
      const payload = JSON.parse(line.slice(5).trim());
      if (payload.id === expectedId) return payload;
    }
  }
  throw new Error(`MCP SSE response omitted request id ${expectedId}`);
}

async function mcpRequest({ id, method, params, bearer, sessionId }) {
  const headers = {
    Accept: "application/json, text/event-stream",
    Authorization: `Bearer ${bearer}`,
    "Content-Type": "application/json",
  };
  if (sessionId) headers["Mcp-Session-Id"] = sessionId;
  const response = await fetch(MCP_URL, {
    method: "POST",
    headers,
    body: JSON.stringify({ jsonrpc: "2.0", id, method, params }),
    signal: AbortSignal.timeout(30_000),
  });
  if (!response.ok) {
    throw new Error(`MCP ${method} returned HTTP ${response.status}`);
  }
  const responseSessionId = response.headers.get("mcp-session-id") || sessionId;
  const text = await response.text();
  const payload = response.headers
    .get("content-type")
    ?.toLowerCase()
    .includes("text/event-stream")
    ? parseSse(text, id)
    : JSON.parse(text);
  if (payload.id !== id || payload.error) {
    throw new Error(`MCP ${method} returned an invalid JSON-RPC result`);
  }
  return { payload, sessionId: responseSessionId };
}

async function mcpNotification({ method, params, bearer, sessionId }) {
  const headers = {
    Accept: "application/json, text/event-stream",
    Authorization: `Bearer ${bearer}`,
    "Content-Type": "application/json",
  };
  if (sessionId) headers["Mcp-Session-Id"] = sessionId;
  const response = await fetch(MCP_URL, {
    method: "POST",
    headers,
    body: JSON.stringify({ jsonrpc: "2.0", method, params }),
    signal: AbortSignal.timeout(30_000),
  });
  if (!response.ok) {
    throw new Error(`MCP ${method} notification returned HTTP ${response.status}`);
  }
}

export function assertExactToolNames(expectedToolNames, actualNames) {
  if (
    !Array.isArray(expectedToolNames) ||
    expectedToolNames.length === 0 ||
    expectedToolNames.some((name) => typeof name !== "string") ||
    expectedToolNames.length !== new Set(expectedToolNames).size ||
    JSON.stringify([...expectedToolNames].sort()) !==
      JSON.stringify(expectedToolNames) ||
    !Array.isArray(actualNames) ||
    actualNames.some((name) => typeof name !== "string") ||
    actualNames.length !== new Set(actualNames).size ||
    JSON.stringify([...actualNames].sort()) !== JSON.stringify(actualNames) ||
    JSON.stringify(actualNames) !== JSON.stringify(expectedToolNames)
  ) {
    throw new Error(
      `MCP tools/list differs from reviewed scope: ${JSON.stringify({
        expectedToolNames,
        actualNames,
      })}`,
    );
  }
  return true;
}

export function validateExpectedToolNames(scope, expectedToolNames) {
  const reviewedToolNames = scope?.tools?.map((tool) => tool?.name).sort();
  if (
    scope?.schemaVersion !== 2 ||
    !Array.isArray(reviewedToolNames) ||
    reviewedToolNames.length === 0 ||
    reviewedToolNames.some((name) => typeof name !== "string") ||
    reviewedToolNames.length !== new Set(reviewedToolNames).size ||
    !Array.isArray(expectedToolNames) ||
    expectedToolNames.length === 0 ||
    expectedToolNames.some((name) => typeof name !== "string") ||
    expectedToolNames.length !== new Set(expectedToolNames).size ||
    JSON.stringify([...expectedToolNames].sort()) !==
      JSON.stringify(expectedToolNames) ||
    expectedToolNames.some((name) => !reviewedToolNames.includes(name))
  ) {
    throw new Error("reviewed MCP tool scope or derived expected tools are invalid");
  }
  return reviewedToolNames;
}

async function verifyMcp(expectedToolNames, bearer) {
  const initialized = await mcpRequest({
    id: 1,
    method: "initialize",
    params: {
      protocolVersion: "2025-03-26",
      capabilities: {},
      clientInfo: {
        name: "teamagent-openclaw-rollout-canary",
        version: "1",
      },
    },
    bearer,
  });
  await mcpNotification({
    method: "notifications/initialized",
    params: {},
    bearer,
    sessionId: initialized.sessionId,
  });
  const listed = await mcpRequest({
    id: 2,
    method: "tools/list",
    params: {},
    bearer,
    sessionId: initialized.sessionId,
  });
  const tools = listed.payload.result?.tools;
  if (!Array.isArray(tools)) throw new Error("MCP tools/list result is not an array");
  const actualNames = tools.map((tool) => tool?.name).sort();
  assertExactToolNames(expectedToolNames, actualNames);
  return {
    protocolVersion: initialized.payload.result?.protocolVersion,
    toolCount: actualNames.length,
    toolNamesSha256: canonicalSha256(actualNames),
  };
}

async function verifyBedrock(modelId) {
  if (
    process.env.AWS_ACCESS_KEY_ID ||
    process.env.AWS_SECRET_ACCESS_KEY ||
    process.env.AWS_SESSION_TOKEN
  ) {
    throw new Error("static AWS credential variables reached the rollout task");
  }
  const credentialSource = process.env.AWS_CONTAINER_CREDENTIALS_RELATIVE_URI
    ? "ECS_CONTAINER_CREDENTIALS_RELATIVE_URI"
    : process.env.AWS_CONTAINER_CREDENTIALS_FULL_URI
      ? "ECS_CONTAINER_CREDENTIALS_FULL_URI"
      : null;
  if (!credentialSource) {
    throw new Error("ECS task-role credential endpoint is unavailable");
  }
  if (process.env.AWS_REGION !== REGION) {
    throw new Error("rollout task is not in the fixed AWS region");
  }
  const require = createRequire(
    "/opt/teamagent/plugins/amazon-bedrock/package.json",
  );
  const { BedrockRuntimeClient, ConverseCommand } = require(
    "@aws-sdk/client-bedrock-runtime",
  );
  const client = new BedrockRuntimeClient({ region: REGION });
  const abortController = new AbortController();
  const timeout = setTimeout(() => abortController.abort(), 45_000);
  try {
    const response = await client.send(
      new ConverseCommand({
        modelId,
        messages: [
          {
            role: "user",
            content: [{ text: "Reply with OK." }],
          },
        ],
        inferenceConfig: {
          maxTokens: 2,
          temperature: 0,
        },
      }),
      { abortSignal: abortController.signal },
    );
    if (!response?.output?.message || !response?.usage) {
      throw new Error("Bedrock Converse returned an incomplete response");
    }
  } finally {
    clearTimeout(timeout);
    client.destroy();
  }
  return { modelId, credentialSource, request: "Converse", passed: true };
}

async function run() {
  if (
    process.argv.length !== 6 ||
    process.argv[2] !== "--receipt-id" ||
    process.argv[4] !== "--expected-tool-names-json"
  ) {
    throw new Error(
      "usage: rollout-task-canary.mjs --receipt-id <id> --expected-tool-names-json <json>",
    );
  }
  const receiptId = process.argv[3];
  if (!TOKEN_PATTERN.test(receiptId)) throw new Error("invalid rollout receipt id");
  if (!CONFIG_PATH) throw new Error("OPENCLAW_CONFIG_PATH is unavailable");
  const scope = JSON.parse(fs.readFileSync(TOOL_SCOPE_PATH, "utf8"));
  const config = JSON.parse(fs.readFileSync(CONFIG_PATH, "utf8"));
  const expectedToolNames = JSON.parse(process.argv[5]);
  const configuredToolNames =
    config.mcp?.servers?.teamagent?.toolFilter?.include?.toSorted();
  const reviewedToolNames = validateExpectedToolNames(scope, expectedToolNames);
  const configuredNativeDeny = config.tools?.deny?.toSorted();
  const reviewedNativeDeny = scope.nativeTools?.deny?.toSorted();
  if (
    JSON.stringify(configuredToolNames) !== JSON.stringify(reviewedToolNames) ||
    scope.nativeTools?.profile !== "minimal" ||
    config.tools?.profile !== scope.nativeTools.profile ||
    JSON.stringify(config.tools?.alsoAllow) !==
      JSON.stringify(scope.nativeTools?.alsoAllow) ||
    JSON.stringify(configuredNativeDeny) !== JSON.stringify(reviewedNativeDeny) ||
    !reviewedNativeDeny?.includes("message") ||
    !reviewedNativeDeny?.includes("sessions_send") ||
    !reviewedNativeDeny?.includes("read") ||
    config.mcp?.servers?.teamagent?.url !== MCP_URL
  ) {
    throw new Error("image config and reviewed MCP tool scope differ");
  }
  const bearer = process.env.TEAMAGENT_MCP_BEARER;
  if (!bearer) throw new Error("TEAMAGENT_MCP_BEARER is unavailable");
  const model = config.agents?.list?.find((agent) => agent.default === true)?.model;
  if (
    typeof model !== "string" ||
    !model.startsWith("amazon-bedrock/") ||
    model.length <= "amazon-bedrock/".length
  ) {
    throw new Error("default Bedrock model is not pinned in the runtime config");
  }

  const mcp = await verifyMcp(expectedToolNames, bearer);
  const bedrock = await verifyBedrock(model.slice("amazon-bedrock/".length));
  process.stdout.write(
    `${JSON.stringify({
      event: "openclaw_rollout_task_canary",
      schemaVersion: 1,
      receiptId,
      platform: "linux/arm64",
      mcp,
      bedrock,
      passed: true,
    })}\n`,
  );
}

const isMain =
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href;

if (isMain) {
  run().catch((error) =>
    fail(error instanceof Error ? error.message : String(error)),
  );
}
