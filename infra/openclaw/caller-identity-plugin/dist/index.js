import {
  createHash,
  createHmac,
  randomBytes,
} from "node:crypto";

const PLUGIN_ID = "teamagent-caller-identity";
const ISSUER = "teamagent-openclaw";
const AUDIENCE = "teamagent-mcp";
const CLAIM_TTL_SECONDS = 60;
const INBOUND_CONTEXT_TTL_MS = 10 * 60 * 1000;
const MAX_INBOUND_CONTEXTS = 1000;
const TEAMAGENT_TOOL_PREFIX = "teamagent__";
const USER_CONTEXT_KEY = "_user_context";
const CLAIM_FIELD = "caller_claim";

const SLACK_USER_RE = /^U[A-Z0-9]{8,}$/u;
const SLACK_TEAM_RE = /^T[A-Z0-9]{8,}$/u;
const SLACK_CHANNEL_RE = /^[CDG][A-Z0-9]{8,}$/u;
const TOOL_RE = /^[a-z][a-z0-9_]{0,127}$/u;

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

function resolveSlackChannel(...values) {
  for (const value of values) {
    if (typeof value !== "string") continue;
    const match = /(?:^|:)([CDG][A-Z0-9]{8,})$/iu.exec(value.trim());
    if (match) return match[1].toUpperCase();
  }
  return null;
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

function block(reason) {
  return {
    block: true,
    blockReason: `${PLUGIN_ID}: ${reason}`,
  };
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

  const inboundBySession = new Map();

  function pruneInbound(nowMs) {
    for (const [sessionKey, context] of inboundBySession) {
      if (nowMs - context.receivedAtMs > INBOUND_CONTEXT_TTL_MS) {
        inboundBySession.delete(sessionKey);
      }
    }
    if (inboundBySession.size >= MAX_INBOUND_CONTEXTS) {
      fail("trusted inbound context capacity is exhausted");
    }
  }

  function rememberInbound(event, ctx, logger) {
    if (ctx?.channelId !== "slack") return;
    const sessionKey = nonBlank(ctx.sessionKey ?? event?.sessionKey, 2048);
    if (!sessionKey) return;
    const senderId = normalizeSlackId(
      ctx.senderId ?? event?.senderId ?? event?.metadata?.senderId,
      SLACK_USER_RE,
    );
    const teamId = normalizeSlackId(event?.metadata?.guildId, SLACK_TEAM_RE);
    const channelId = resolveSlackChannel(
      ctx.conversationId,
      event?.metadata?.to,
      event?.metadata?.originatingTo,
      event?.from,
    );
    const messageId = nonBlank(
      ctx.messageId ?? event?.messageId ?? event?.metadata?.messageId,
    );
    const threadTs =
      nonBlank(event?.threadId ?? event?.metadata?.threadId, 128) ?? null;
    if (
      !senderId ||
      !teamId ||
      teamId !== expectedTeamId ||
      !channelId ||
      !messageId
    ) {
      inboundBySession.delete(sessionKey);
      logger?.warn?.(
        `${PLUGIN_ID}: rejected incomplete or foreign Slack ingress identity`,
      );
      return;
    }
    const nowMs = now();
    pruneInbound(nowMs);
    inboundBySession.set(sessionKey, {
      senderId,
      teamId,
      channelId,
      threadTs,
      messageId,
      sessionSha256: createHash("sha256").update(sessionKey, "utf8").digest("hex"),
      receivedAtMs: nowMs,
    });
  }

  function signToolCall(event, ctx) {
    const tool = canonicalToolName(event?.toolName);
    if (tool === null) return undefined;
    if (ctx?.channelId !== "slack") {
      return block("TeamAgent MCP tools require a trusted Slack ingress");
    }
    const sessionKey = nonBlank(ctx.sessionKey, 2048);
    if (!sessionKey) return block("trusted Slack session binding is missing");
    const nowMs = now();
    pruneInbound(nowMs);
    const trusted = inboundBySession.get(sessionKey);
    if (!trusted || nowMs - trusted.receivedAtMs > INBOUND_CONTEXT_TTL_MS) {
      return block("trusted Slack ingress identity is missing or stale");
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
    if (declaredContext.slack_user_id !== trusted.senderId) {
      return block("declared Slack caller does not match the ingress event");
    }

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
      v: 1,
      iss: ISSUER,
      aud: AUDIENCE,
      sub: trusted.senderId,
      team: trusted.teamId,
      channel: trusted.channelId,
      thread: trusted.threadTs,
      message: trusted.messageId,
      session_sha256: trusted.sessionSha256,
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

  return {
    id: PLUGIN_ID,
    name: "TeamAgent Caller Identity",
    description: "Signs trusted Slack ingress identity for TeamAgent MCP calls",
    register(api) {
      api.on("message_received", (event, ctx) => {
        rememberInbound(event, ctx, api.logger);
      });
      api.on("before_tool_call", (event, ctx) => signToolCall(event, ctx));
    },
  };
}

export default {
  id: PLUGIN_ID,
  name: "TeamAgent Caller Identity",
  description: "Signs trusted Slack ingress identity for TeamAgent MCP calls",
  register(api) {
    createCallerIdentityPlugin().register(api);
  },
};
