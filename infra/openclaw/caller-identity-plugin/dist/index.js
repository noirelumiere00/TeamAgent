import {
  createHash,
  createHmac,
  randomBytes,
} from "node:crypto";

const PLUGIN_ID = "teamagent-caller-identity";
const ISSUER = "teamagent-openclaw";
const AUDIENCE = "teamagent-mcp";
const CLAIM_VERSION = 2;
const CLAIM_TTL_SECONDS = 60;
const INBOUND_CONTEXT_TTL_MS = 10 * 60 * 1000;
const MAX_TRACKED_CONTEXTS = 1000;
const TEAMAGENT_TOOL_PREFIX = "teamagent__";
const USER_CONTEXT_KEY = "_user_context";
const CLAIM_FIELD = "caller_claim";

const SLACK_USER_RE = /^U[A-Z0-9]{8,}$/u;
const SLACK_TEAM_RE = /^T[A-Z0-9]{8,}$/u;
const SLACK_CHANNEL_RE = /^[CDG][A-Z0-9]{8,}$/u;
const TOOL_RE = /^[a-z][a-z0-9_]{0,127}$/u;
const INVOCATION_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$/u;
const NATIVE_CALLER_BYPASS_TOOLS = new Set([
  "apply_patch",
  "delete",
  "edit",
  "message",
  "read",
  "send",
  "session_status",
  "sessions_history",
  "sessions_list",
  "sessions_send",
  "sessions_spawn",
  "sessions_yield",
  "subagents",
  "upload",
  "write",
]);

function fail(message) {
  throw new Error(`${PLUGIN_ID}: ${message}`);
}

function assertPlainObject(value, label) {
  if (
    value === null ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    Object.getPrototypeOf(value) !== Object.prototype
  ) {
    fail(`${label} must be a plain object`);
  }
  return value;
}

function assertValidUnicode(value) {
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = value.charCodeAt(index + 1);
      if (!(next >= 0xdc00 && next <= 0xdfff)) {
        fail("tool arguments contain an invalid Unicode string");
      }
      index += 1;
    } else if (code >= 0xdc00 && code <= 0xdfff) {
      fail("tool arguments contain an invalid Unicode string");
    }
  }
}

function canonicalValue(value) {
  if (value === null) return ["null"];
  if (typeof value === "boolean") return ["boolean", value];
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      fail("tool arguments contain a non-finite number");
    }
    const bytes = Buffer.allocUnsafe(8);
    bytes.writeDoubleBE(value, 0);
    return ["float64", bytes.toString("hex")];
  }
  if (typeof value === "string") {
    assertValidUnicode(value);
    return ["string", value];
  }
  if (Array.isArray(value)) {
    return ["array", value.map(canonicalValue)];
  }
  const object = assertPlainObject(value, "tool argument object");
  const keys = Object.keys(object).toSorted((left, right) =>
    Buffer.compare(Buffer.from(left, "utf8"), Buffer.from(right, "utf8")),
  );
  return ["object", keys.map(key => [key, canonicalValue(object[key])])];
}

export function canonicalRequestSha256(argumentsValue) {
  const argumentsObject = assertPlainObject(argumentsValue, "tool arguments");
  const rawContext = assertPlainObject(
    argumentsObject[USER_CONTEXT_KEY],
    USER_CONTEXT_KEY,
  );
  const context = {...rawContext};
  delete context[CLAIM_FIELD];
  const sanitized = {
    ...argumentsObject,
    [USER_CONTEXT_KEY]: context,
  };
  return createHash("sha256")
    .update(JSON.stringify(canonicalValue(sanitized)), "utf8")
    .digest("hex");
}

function base64url(value) {
  return Buffer.from(value).toString("base64url");
}

function normalizeSlackId(value, pattern) {
  if (typeof value !== "string") return null;
  const normalized = value.trim().toUpperCase();
  return pattern.test(normalized) ? normalized : null;
}

function resolveSlackChannel(value) {
  if (typeof value !== "string") return null;
  const match = /(?:^|:)([CDG][A-Z0-9]{8,})$/iu.exec(value.trim());
  return match ? match[1].toUpperCase() : null;
}

function consistentValue(values, normalize) {
  const normalized = [];
  for (const value of values) {
    if (value === undefined || value === null) continue;
    const item = normalize(value);
    if (item === null) return null;
    normalized.push(item);
  }
  if (normalized.length === 0 || new Set(normalized).size !== 1) return null;
  return normalized[0];
}

function consistentSlackChannel(values) {
  const normalized = values
    .map(resolveSlackChannel)
    .filter(value => value !== null);
  if (normalized.length === 0 || new Set(normalized).size !== 1) return null;
  return normalized[0];
}

function nonBlank(value, maxLength = 512) {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  return normalized && normalized.length <= maxLength ? normalized : null;
}

function canonicalToolName(value) {
  if (typeof value !== "string" || !value.startsWith(TEAMAGENT_TOOL_PREFIX)) {
    return null;
  }
  const tool = value.slice(TEAMAGENT_TOOL_PREFIX.length);
  return TOOL_RE.test(tool) ? tool : null;
}

function canonicalInvocationId(value) {
  const normalized = nonBlank(value, 256);
  return normalized && INVOCATION_ID_RE.test(normalized) ? normalized : null;
}

function block(reason) {
  return {
    block: true,
    blockReason: `${PLUGIN_ID}: ${reason}`,
  };
}

function sameIngress(left, right) {
  return (
    left.sessionKey === right.sessionKey &&
    left.senderId === right.senderId &&
    left.teamId === right.teamId &&
    left.channelId === right.channelId &&
    left.threadTs === right.threadTs &&
    left.messageId === right.messageId
  );
}

function invocationKey(runId, toolCallId) {
  return JSON.stringify([runId, toolCallId]);
}

export function createCallerIdentityPlugin({
  env = process.env,
  now = () => Date.now(),
  randomBytesFn = randomBytes,
} = {}) {
  const rawSecret = env.TEAMAGENT_CALLER_CLAIM_SECRET;
  const secret = typeof rawSecret === "string" ? Buffer.from(rawSecret, "utf8") : null;
  if (!secret || secret.length < 32 || rawSecret.includes("${")) {
    fail("TEAMAGENT_CALLER_CLAIM_SECRET must contain at least 32 bytes");
  }
  const expectedTeamId = normalizeSlackId(env.SLACK_TEAM_ID, SLACK_TEAM_RE);
  if (!expectedTeamId) {
    fail("SLACK_TEAM_ID must be a canonical Slack T ID");
  }

  const pendingByMessage = new Map();
  const ingressByRun = new Map();
  const rejectedRuns = new Map();
  const consumedInvocations = new Map();

  function pruneState(nowMs) {
    for (const [key, ingress] of pendingByMessage) {
      if (nowMs - ingress.receivedAtMs > INBOUND_CONTEXT_TTL_MS) {
        pendingByMessage.delete(key);
      }
    }
    for (const [runId, ingress] of ingressByRun) {
      if (nowMs - ingress.receivedAtMs > INBOUND_CONTEXT_TTL_MS) {
        ingressByRun.delete(runId);
      }
    }
    for (const [runId, rejectedAtMs] of rejectedRuns) {
      if (nowMs - rejectedAtMs > INBOUND_CONTEXT_TTL_MS) {
        rejectedRuns.delete(runId);
      }
    }
    for (const [key, invocation] of consumedInvocations) {
      if (nowMs - invocation.consumedAtMs > INBOUND_CONTEXT_TTL_MS) {
        consumedInvocations.delete(key);
      }
    }
    if (
      pendingByMessage.size +
        ingressByRun.size +
        rejectedRuns.size +
        consumedInvocations.size >=
      MAX_TRACKED_CONTEXTS
    ) {
      fail("trusted caller binding capacity is exhausted");
    }
  }

  function rejectRun(runId, rejectedAtMs, ingress = null) {
    const existing = ingressByRun.get(runId);
    if (existing) pendingByMessage.delete(existing.pendingKey);
    if (ingress) pendingByMessage.delete(ingress.pendingKey);
    ingressByRun.delete(runId);
    rejectedRuns.set(runId, rejectedAtMs);
  }

  function bindRun(runId, ingress) {
    if (rejectedRuns.has(runId)) return false;
    const existing = ingressByRun.get(runId);
    if (existing) {
      const matches = sameIngress(existing, ingress);
      if (matches) pendingByMessage.delete(ingress.pendingKey);
      else rejectRun(runId, now(), ingress);
      return matches;
    }
    for (const bound of ingressByRun.values()) {
      if (sameIngress(bound, ingress)) return false;
    }
    ingressByRun.set(runId, ingress);
    pendingByMessage.delete(ingress.pendingKey);
    return true;
  }

  function rememberInbound(event, ctx, logger) {
    if (ctx?.channelId !== "slack") return;
    const sessionKey = consistentValue(
      [ctx.sessionKey, event?.sessionKey],
      value => nonBlank(value, 2048),
    );
    const senderId = consistentValue(
      [ctx.senderId, event?.senderId, event?.metadata?.senderId],
      value => normalizeSlackId(value, SLACK_USER_RE),
    );
    const teamId = normalizeSlackId(event?.metadata?.guildId, SLACK_TEAM_RE);
    const channelId = consistentSlackChannel([
      ctx.conversationId,
      event?.metadata?.to,
      event?.metadata?.originatingTo,
      event?.from,
    ]);
    const messageId = consistentValue(
      [ctx.messageId, event?.messageId, event?.metadata?.messageId],
      value => nonBlank(value, 512),
    );
    const threadTs =
      consistentValue(
        [event?.threadId, event?.metadata?.threadId],
        value => nonBlank(String(value), 128),
      ) ?? null;
    const suppliedRunIds = [ctx.runId, event?.runId].filter(
      value => value !== undefined && value !== null,
    );
    const runId =
      suppliedRunIds.length === 0
        ? null
        : consistentValue(suppliedRunIds, canonicalInvocationId);
    if (
      !sessionKey ||
      !senderId ||
      !teamId ||
      teamId !== expectedTeamId ||
      !channelId ||
      !messageId ||
      (suppliedRunIds.length > 0 && !runId)
    ) {
      logger?.warn?.(
        `${PLUGIN_ID}: rejected incomplete, conflicting, or foreign Slack ingress identity`,
      );
      return;
    }
    const nowMs = now();
    pruneState(nowMs);
    const pendingKey = JSON.stringify([sessionKey, messageId]);
    const ingress = {
      pendingKey,
      sessionKey,
      senderId,
      teamId,
      channelId,
      threadTs,
      messageId,
      sessionSha256: createHash("sha256").update(sessionKey, "utf8").digest("hex"),
      receivedAtMs: nowMs,
    };
    const existing = pendingByMessage.get(pendingKey);
    if (existing && !sameIngress(existing, ingress)) {
      pendingByMessage.delete(pendingKey);
      logger?.warn?.(`${PLUGIN_ID}: rejected conflicting Slack message identity`);
      return;
    }
    pendingByMessage.set(pendingKey, ingress);
    if (runId && !bindRun(runId, ingress)) {
      pendingByMessage.delete(pendingKey);
      logger?.warn?.(`${PLUGIN_ID}: rejected conflicting Slack run binding`);
    }
  }

  function bindAgentRun(_event, ctx, logger) {
    if (String(ctx?.messageProvider ?? "").toLowerCase() !== "slack") return;
    const runId = canonicalInvocationId(ctx?.runId);
    const sessionKey = nonBlank(ctx?.sessionKey, 2048);
    const senderId = normalizeSlackId(ctx?.senderId, SLACK_USER_RE);
    const channelId = consistentSlackChannel([ctx?.channelId, ctx?.channel]);
    if (!runId || !sessionKey || !senderId || !channelId) {
      logger?.warn?.(`${PLUGIN_ID}: rejected incomplete authoritative agent run`);
      return;
    }
    const nowMs = now();
    pruneState(nowMs);
    if (rejectedRuns.has(runId)) {
      logger?.warn?.(`${PLUGIN_ID}: authoritative agent run was already rejected`);
      return;
    }
    const existing = ingressByRun.get(runId);
    if (existing) {
      if (
        existing.sessionKey !== sessionKey ||
        existing.senderId !== senderId ||
        existing.channelId !== channelId
      ) {
        rejectRun(runId, nowMs);
        logger?.warn?.(`${PLUGIN_ID}: rejected mismatched repeated agent run`);
      }
      return;
    }
    const candidates = [...pendingByMessage.values()].filter(
      ingress =>
        ingress.sessionKey === sessionKey &&
        ingress.senderId === senderId &&
        ingress.channelId === channelId &&
        nowMs - ingress.receivedAtMs <= INBOUND_CONTEXT_TTL_MS,
    );
    if (candidates.length !== 1 || !bindRun(runId, candidates[0])) {
      rejectRun(runId, nowMs, candidates.length === 1 ? candidates[0] : null);
      logger?.warn?.(
        `${PLUGIN_ID}: agent run has no unique fresh Slack message binding`,
      );
    }
  }

  function validateDeclaredContext(declaredContext, trusted) {
    if (declaredContext[CLAIM_FIELD] !== undefined) {
      return "model-supplied or replayed caller claim is forbidden";
    }
    if (declaredContext.slack_user_id !== trusted.senderId) {
      return "declared Slack caller does not match the bound ingress";
    }
    for (const [field, expected] of [
      ["slack_team_id", trusted.teamId],
      ["channel_id", trusted.channelId],
      ["thread_ts", trusted.threadTs],
    ]) {
      if (
        Object.hasOwn(declaredContext, field) &&
        declaredContext[field] !== expected
      ) {
        return `declared ${field} does not match the bound ingress`;
      }
    }
    return null;
  }

  function signToolCall(event, ctx) {
    const observedToolName = nonBlank(event?.toolName, 256);
    const contextToolName = nonBlank(ctx?.toolName, 256);
    if ([observedToolName, contextToolName].some(
      name => name && NATIVE_CALLER_BYPASS_TOOLS.has(name.toLowerCase()),
    )) {
      return block("native message, filesystem, and session tools are denied");
    }
    const tool = canonicalToolName(observedToolName);
    if (tool === null) return undefined;
    if (ctx?.toolName !== event?.toolName) {
      return block("authoritative tool name binding is missing or mismatched");
    }
    const eventRunId = canonicalInvocationId(event?.runId);
    const contextRunId = canonicalInvocationId(ctx?.runId);
    if (!eventRunId || !contextRunId || eventRunId !== contextRunId) {
      return block("authoritative run binding is missing or mismatched");
    }
    const eventToolCallId = canonicalInvocationId(event?.toolCallId);
    const contextToolCallId = canonicalInvocationId(ctx?.toolCallId);
    if (
      !eventToolCallId ||
      !contextToolCallId ||
      eventToolCallId !== contextToolCallId
    ) {
      return block("authoritative tool invocation binding is missing or mismatched");
    }
    const sessionKey = nonBlank(ctx?.sessionKey, 2048);
    const channelId = resolveSlackChannel(ctx?.channelId);
    if (!sessionKey || !channelId) {
      return block("trusted Slack session or channel binding is missing");
    }
    const nowMs = now();
    pruneState(nowMs);
    const trusted = ingressByRun.get(eventRunId);
    if (!trusted || nowMs - trusted.receivedAtMs > INBOUND_CONTEXT_TTL_MS) {
      return block("trusted Slack run identity is missing or stale");
    }
    if (
      trusted.sessionKey !== sessionKey ||
      trusted.channelId !== channelId
    ) {
      return block("tool context does not match the bound Slack run");
    }
    const exactInvocationKey = invocationKey(eventRunId, eventToolCallId);
    if (consumedInvocations.has(exactInvocationKey)) {
      return block("tool invocation replay rejected");
    }
    let params;
    let declaredContext;
    try {
      params = assertPlainObject(event.params, "tool params");
      declaredContext = assertPlainObject(
        params[USER_CONTEXT_KEY],
        USER_CONTEXT_KEY,
      );
    } catch (error) {
      return block(error instanceof Error ? error.message : "invalid tool params");
    }
    const declarationError = validateDeclaredContext(declaredContext, trusted);
    if (declarationError) return block(declarationError);

    const authoritativeContext = {
      slack_user_id: trusted.senderId,
      slack_team_id: trusted.teamId,
      channel_id: trusted.channelId,
      ...(trusted.threadTs === null ? {} : {thread_ts: trusted.threadTs}),
    };
    const adjustedParams = {
      ...params,
      [USER_CONTEXT_KEY]: authoritativeContext,
    };
    let argumentsSha256;
    try {
      argumentsSha256 = canonicalRequestSha256(adjustedParams);
    } catch (error) {
      return block(error instanceof Error ? error.message : "request binding failed");
    }
    const issuedAt = Math.floor(nowMs / 1000);
    const nonceBytes = randomBytesFn(16);
    if (!Buffer.isBuffer(nonceBytes) || nonceBytes.length !== 16) {
      return block("secure nonce generation failed");
    }
    const payload = {
      v: CLAIM_VERSION,
      iss: ISSUER,
      aud: AUDIENCE,
      sub: trusted.senderId,
      team: trusted.teamId,
      channel: trusted.channelId,
      thread: trusted.threadTs,
      message: trusted.messageId,
      session_sha256: trusted.sessionSha256,
      run_id: eventRunId,
      tool_call_id: eventToolCallId,
      tool,
      arguments_sha256: argumentsSha256,
      nonce: nonceBytes.toString("base64url"),
      iat: issuedAt,
      exp: issuedAt + CLAIM_TTL_SECONDS,
    };
    const payloadSegment = base64url(JSON.stringify(payload));
    const signatureSegment = createHmac("sha256", secret)
      .update(payloadSegment, "ascii")
      .digest("base64url");
    consumedInvocations.set(exactInvocationKey, {
      runId: eventRunId,
      consumedAtMs: nowMs,
    });
    return {
      params: {
        ...adjustedParams,
        [USER_CONTEXT_KEY]: {
          ...authoritativeContext,
          [CLAIM_FIELD]: `${payloadSegment}.${signatureSegment}`,
        },
      },
    };
  }

  function releaseAgentRun(event, ctx) {
    const eventRunId = canonicalInvocationId(event?.runId);
    const contextRunId = canonicalInvocationId(ctx?.runId);
    if (!eventRunId || !contextRunId || eventRunId !== contextRunId) return;
    ingressByRun.delete(eventRunId);
    for (const [key, invocation] of consumedInvocations) {
      if (invocation.runId === eventRunId) {
        consumedInvocations.delete(key);
      }
    }
  }

  return {
    id: PLUGIN_ID,
    name: "TeamAgent Caller Identity",
    description: "Signs exact Slack run and tool-invocation caller identity",
    register(api) {
      api.on("inbound_claim", (event, ctx) => {
        rememberInbound(event, ctx, api.logger);
      });
      api.on("message_received", (event, ctx) => {
        rememberInbound(event, ctx, api.logger);
      });
      api.on("before_model_resolve", (event, ctx) => {
        bindAgentRun(event, ctx, api.logger);
      });
      api.on("before_tool_call", (event, ctx) => signToolCall(event, ctx));
      api.on("agent_end", (event, ctx) => {
        releaseAgentRun(event, ctx);
      });
    },
  };
}

export default {
  id: PLUGIN_ID,
  name: "TeamAgent Caller Identity",
  description: "Signs exact Slack run and tool-invocation caller identity",
  register(api) {
    createCallerIdentityPlugin().register(api);
  },
};
