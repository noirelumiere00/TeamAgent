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
const ACTION_CONTEXT_TTL_MS = 5 * 60 * 1000;
const MAX_TRACKED_CONTEXTS = 1000;
const TEAMAGENT_TOOL_PREFIX = "teamagent__";
const USER_CONTEXT_KEY = "_user_context";
const CLAIM_FIELD = "caller_claim";
const MAIL_DRAFT_ACTION_ID = "mail_draft";
const MAIL_DRAFT_TOOL = "mail_draft";
const SLACK_INTERACTION_EVENT_PREFIX = "Slack interaction: ";
const SLACK_INTERACTION_VALUE_MAX_LENGTH = 160;

// ── 第3層防御: 連携 URL 捏造の封鎖 ──────────────────────────────────────
// 背景（本番実測 2026-08-31）: LLM がツールを 1 つも呼ばないまま
// https://connect.openclaw.ai/oauth/google?user_id=... を捏造し、利用者へ届いた。
// MCP 境界の決定論分岐（server.py の _maybe_redirect_to_connect）は tool 呼び出しが
// 発生して初めて効くため、0 tool call のターンには届かない。ここが最後の砦になる。
//
// 判定は intent ではなく出力検証で行う: この run の teamagent tool call が 0 なら
// oauth_connect は 1 度も URL を発行していない。よって応答本文に現れる連携 URL は
// 定義上すべて捏造である（推測が入らない）。
const ASSISTANT_MESSAGE_SCAN_LIMIT = 100000;
const CONNECT_URL_RE = /https?:\/\/[^\s<>()\[\]"'`|]+/giu;
const CONNECT_URL_TRAILING_RE = /[)\]}>.,;:!?'"`。、）】」]+$/u;
const UPSTREAM_VENDOR_HOST_RE = /(?:^|\.)openclaw\.ai$/iu;
const CONNECT_WEB_HOST_RE = /(?:^|\.)newstv\.co\.jp$/iu;
const CONNECT_PATH_RE = /(?:oauth|authorize|\/connect)/iu;
const CONNECT_FABRICATION_RETRY_KEY = "connect-url-fabrication";
// 第3層の run 台帳は agent_end だけに掃除を任せない。abort/crash/timeout で
// agent_end が発火しない run が残留し、長寿命プロセスで無制限に育つため、
// 他の Map と同じ TTL 掃除に加えて上限で古いものから捨てる。
// ここでの脱落は「介入を 1 回余分に許す/取りこぼす」だけで、上流の revise 予算
// (runId x idempotencyKey) が最終的にループを止める。署名経路を落とす
// MAX_TRACKED_CONTEXTS の fail には意図的に相乗りさせない。
const MAX_CONNECT_GUARD_RUNS = MAX_TRACKED_CONTEXTS;
const MAX_CONNECT_FABRICATION_REVISIONS = 1;
const CONNECT_FABRICATION_REASON =
  "直前の下書き回答には、ツールが発行していない連携 URL が含まれています。その URL は実在しません。";
// 上流の再パス前置き（embedded-agent:1773）は
// "Do not ... rerun tools unless the request explicitly requires it" と指示するため、
// ここで明示的にツール実行を要求しないと握り潰される。
const CONNECT_FABRICATION_INSTRUCTION = [
  "この指示は明示的にツール実行を要求します: oauth_connect を必ず呼び出し、",
  "その戻り値の message に含まれる URL だけを、1 文字も変えずに提示してください。",
  "自分の知識・記憶・過去の会話から URL を組み立てることは禁止です。",
  "oauth_connect が失敗した場合は、URL を書かず、リンクを発行できなかった旨だけを伝えてください。",
].join("\n");

const SLACK_USER_RE = /^U[A-Z0-9]{8,}$/u;
const SLACK_TEAM_RE = /^T[A-Z0-9]{8,}$/u;
const SLACK_CHANNEL_RE = /^[CDG][A-Z0-9]{8,}$/u;
const SLACK_TS_RE = /^[0-9]{10,}\.[0-9]{6}$/u;
const DRAFT_TOKEN_RE = /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{22}$/u;
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

// OpenClaw がセッション鍵の末尾に付ける唯一の構造サフィックス。
// 実測 2026-08-07（本番 image sha256:144e4edd… の上流コードを実行）:
//   app/dist/hook-agent-context-DPPRzCBU.js:40-62
//     resolveAgentHookChannelId が parseRawSessionConversationRef(sessionKey).rawId を
//     最優先で返し、channelId と chatId に同じ値を入れる（conversationId は ctx に無い）
//   app/dist/session-key-utils-A-JGvyXu.js:246-266  その parser は :thread: を落とさない
//   slack/dist/pipeline.runtime-rpVpay59.js:3060,2304  app_mention は必ず thread を種付ける
// ＝チャンネルでは値が `c0b0pqd83n2:thread:1785206176.940189` になり、
//   会話 id が末尾に来ないため下の $ アンカーが原理的に当たらない。
const SLACK_SESSION_THREAD_SUFFIX_RE = /:thread:[^:]+$/u;
const SLACK_CHANNEL_TAIL_RE = /(?:^|:)([CDG][A-Z0-9]{8,})$/iu;

function resolveSlackChannel(value) {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  // ① 従来どおりの解決を先に試す。ここで当たる値の結果は一切変わらない（単調性）。
  const direct = SLACK_CHANNEL_TAIL_RE.exec(trimmed);
  if (direct) return direct[1].toUpperCase();
  // ② 従来はここで諦めていた。セッション鍵由来の :thread:<ts> を **1 回だけ** 外して再試行する。
  //    照合そのものは緩めない。外した残りが空・不正形式なら従来どおり null。
  const stripped = trimmed.replace(SLACK_SESSION_THREAD_SUFFIX_RE, "");
  if (stripped !== trimmed) {
    const threaded = SLACK_CHANNEL_TAIL_RE.exec(stripped);
    if (threaded) return threaded[1].toUpperCase();
  }
  // ③ DM フォールバックは **元の値** に対して行う（サフィックス除去を波及させない）。
  //    DM は kind=direct で thread が付かないため、剥がす必要が無い。
  // A direct message never carries its D… conversation id on this path: the
  // Slack plugin sets reply.to to `user:<U…>` and OpenClaw derives every
  // candidate (conversationId / to / originatingTo) from that, so the D… id
  // only survives on ctxPayload and is dropped before the plugin sees it.
  // The peer user id identifies the 1:1 conversation just as uniquely, so
  // accept it under a distinct `DM:` prefix. The prefix keeps the DM namespace
  // disjoint from real channels, so a U… value can never be mistaken for — or
  // collide with — a C/D/G channel id.
  const dm = /(?:^|:)(U[A-Z0-9]{8,})$/iu.exec(trimmed);
  return dm ? `DM:${dm[1].toUpperCase()}` : null;
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

function classifyConnectUrl(rawUrl) {
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return null;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
  const host = parsed.hostname;
  const target = `${parsed.pathname}${parsed.search}`;
  // 本家ドメインは自社では絶対に使わない。パスを問わず捏造と断定できる。
  if (UPSTREAM_VENDOR_HOST_RE.test(host)) return "upstream_domain";
  if (CONNECT_WEB_HOST_RE.test(host)) {
    return CONNECT_PATH_RE.test(target) ? "connect_web_oauth" : null;
  }
  return CONNECT_PATH_RE.test(target) ? "oauth_path" : null;
}

function findFabricatedConnectUrlKinds(text) {
  const kinds = new Set();
  let scanned = 0;
  for (const match of text.slice(0, ASSISTANT_MESSAGE_SCAN_LIMIT).matchAll(CONNECT_URL_RE)) {
    scanned += 1;
    const kind = classifyConnectUrl(match[0].replace(CONNECT_URL_TRAILING_RE, ""));
    if (kind) kinds.add(kind);
  }
  return { kinds: [...kinds].sort(), scanned };
}

function sameIngress(left, right) {
  return (
    left.ingressKind === right.ingressKind &&
    left.sessionKey === right.sessionKey &&
    left.senderId === right.senderId &&
    left.teamId === right.teamId &&
    left.channelId === right.channelId &&
    left.threadTs === right.threadTs &&
    left.messageId === right.messageId &&
    left.actionFingerprint === right.actionFingerprint
  );
}

function invocationKey(runId, toolCallId) {
  return JSON.stringify([runId, toolCallId]);
}

function canonicalSlackTimestamp(value) {
  if (typeof value !== "string" || value !== value.trim()) return null;
  return SLACK_TS_RE.test(value) ? value : null;
}

function optionalSlackTimestamp(value) {
  if (value === undefined || value === null || value === "") {
    return {valid: true, value: null};
  }
  const normalized = canonicalSlackTimestamp(value);
  return {valid: normalized !== null, value: normalized};
}

function canonicalDraftToken(value) {
  if (
    typeof value !== "string" ||
    value !== value.trim() ||
    value.length > SLACK_INTERACTION_VALUE_MAX_LENGTH ||
    !DRAFT_TOKEN_RE.test(value)
  ) {
    return null;
  }
  return value;
}

function actionFingerprint({
  senderId,
  teamId,
  channelId,
  messageTs,
  threadTs,
  actionId,
  actionValue,
}) {
  return createHash("sha256")
    .update(
      JSON.stringify([
        senderId,
        teamId,
        channelId,
        messageTs,
        threadTs,
        actionId,
        actionValue,
      ]),
      "utf8",
    )
    .digest("hex");
}

function parseMailDraftSystemEvent(prompt) {
  if (typeof prompt !== "string" || prompt.length > 100_000) return null;
  const matches = [];
  for (const line of prompt.split(/\r?\n/u)) {
    const markerIndex = line.indexOf(SLACK_INTERACTION_EVENT_PREFIX);
    if (markerIndex < 0) continue;
    if (
      line.indexOf(
        SLACK_INTERACTION_EVENT_PREFIX,
        markerIndex + SLACK_INTERACTION_EVENT_PREFIX.length,
      ) >= 0
    ) {
      return null;
    }
    const leader = line.slice(0, markerIndex);
    if (
      leader !== "" &&
      !/^System: \[[^\]\r\n]{1,160}\] $/u.test(leader)
    ) {
      return null;
    }
    try {
      matches.push(
        assertPlainObject(
          JSON.parse(
            line.slice(markerIndex + SLACK_INTERACTION_EVENT_PREFIX.length),
          ),
          "Slack interaction system event",
        ),
      );
    } catch {
      return null;
    }
  }
  if (matches.length !== 1) return null;
  const payload = matches[0];
  const senderId = normalizeSlackId(payload.userId, SLACK_USER_RE);
  const teamId = normalizeSlackId(payload.teamId, SLACK_TEAM_RE);
  const channelId = normalizeSlackId(payload.channelId, SLACK_CHANNEL_RE);
  const messageTs = canonicalSlackTimestamp(payload.messageTs);
  const thread = optionalSlackTimestamp(payload.threadTs);
  const actionValue = canonicalDraftToken(payload.value);
  if (
    payload.interactionType !== "block_action" ||
    payload.actionId !== MAIL_DRAFT_ACTION_ID ||
    payload.actionType !== "button" ||
    !senderId ||
    !teamId ||
    !channelId ||
    !messageTs ||
    !thread.valid ||
    !actionValue
  ) {
    return null;
  }
  return {
    senderId,
    teamId,
    channelId,
    messageTs,
    threadTs: thread.value,
    actionId: MAIL_DRAFT_ACTION_ID,
    actionValue,
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

  const pendingByMessage = new Map();
  const pendingActions = new Map();
  const seenActions = new Map();
  const ingressByRun = new Map();
  const rejectedRuns = new Map();
  const consumedInvocations = new Map();
  const toolCallsByRun = new Map();
  const connectRevisionsByRun = new Map();

  function pruneConnectGuardState(nowMs) {
    for (const ledger of [toolCallsByRun, connectRevisionsByRun]) {
      for (const [runId, entry] of ledger) {
        if (nowMs - entry.updatedAtMs > INBOUND_CONTEXT_TTL_MS) {
          ledger.delete(runId);
        }
      }
      // TTL 内でも上限を超えたら、最も古い記録から落とす。
      // 記録の更新側が delete->set しているので、挿入順が更新順と一致する。
      while (ledger.size > MAX_CONNECT_GUARD_RUNS) {
        const oldest = ledger.keys().next();
        if (oldest.done) break;
        ledger.delete(oldest.value);
      }
    }
  }

  function pruneState(nowMs) {
    pruneConnectGuardState(nowMs);
    for (const [key, ingress] of pendingByMessage) {
      if (nowMs - ingress.receivedAtMs > INBOUND_CONTEXT_TTL_MS) {
        pendingByMessage.delete(key);
      }
    }
    for (const [fingerprint, ingress] of pendingActions) {
      if (nowMs - ingress.receivedAtMs > ACTION_CONTEXT_TTL_MS) {
        pendingActions.delete(fingerprint);
      }
    }
    for (const [fingerprint, seenAtMs] of seenActions) {
      if (nowMs - seenAtMs > INBOUND_CONTEXT_TTL_MS) {
        seenActions.delete(fingerprint);
      }
    }
    for (const [runId, ingress] of ingressByRun) {
      const ttl =
        ingress.ingressKind === "action"
          ? ACTION_CONTEXT_TTL_MS
          : INBOUND_CONTEXT_TTL_MS;
      if (nowMs - ingress.receivedAtMs > ttl) {
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
        pendingActions.size +
        seenActions.size +
        ingressByRun.size +
        rejectedRuns.size +
        consumedInvocations.size >=
      MAX_TRACKED_CONTEXTS
    ) {
      fail("trusted caller binding capacity is exhausted");
    }
  }

  function removePending(ingress) {
    if (ingress.ingressKind === "action") {
      pendingActions.delete(ingress.pendingKey);
    } else {
      pendingByMessage.delete(ingress.pendingKey);
    }
  }

  function rejectRun(runId, rejectedAtMs, ingress = null) {
    const existing = ingressByRun.get(runId);
    if (existing) removePending(existing);
    if (ingress) removePending(ingress);
    ingressByRun.delete(runId);
    rejectedRuns.set(runId, rejectedAtMs);
  }

  function bindRun(runId, ingress) {
    if (rejectedRuns.has(runId)) return false;
    const existing = ingressByRun.get(runId);
    if (existing) {
      const matches = sameIngress(existing, ingress);
      if (matches) removePending(ingress);
      else rejectRun(runId, now(), ingress);
      return matches;
    }
    for (const bound of ingressByRun.values()) {
      if (sameIngress(bound, ingress)) return false;
    }
    ingressByRun.set(runId, ingress);
    removePending(ingress);
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
      // Report which field failed. The combined message made every rejection
      // look identical, so a missing team could not be told apart from a
      // missing run id and the real cause stayed invisible in production.
      // Only field names and a boolean-ish shape are logged, never the values:
      // sender and channel ids are caller identity and must not reach logs.
      // The team id is the sole exception, and only when it is present and
      // wrong, because operators cannot fix a workspace mismatch without
      // seeing which workspace actually arrived.
      const missing = [];
      if (!sessionKey) missing.push("sessionKey");
      if (!senderId) missing.push("senderId");
      if (!teamId) missing.push("teamId");
      if (!channelId) missing.push("channelId");
      if (!messageId) missing.push("messageId");
      if (suppliedRunIds.length > 0 && !runId) missing.push("runId");
      const mismatch = teamId && teamId !== expectedTeamId;
      logger?.warn?.(
        `${PLUGIN_ID}: rejected incomplete, conflicting, or foreign Slack ingress identity` +
          ` (missing=[${missing.join(",")}]` +
          `${mismatch ? ` foreignTeam=${teamId} expected=${expectedTeamId}` : ""}` +
          ` suppliedRunIds=${suppliedRunIds.length})`,
      );
      return;
    }
    const nowMs = now();
    pruneState(nowMs);
    const pendingKey = JSON.stringify([sessionKey, messageId]);
    const ingress = {
      ingressKind: "message",
      pendingKey,
      sessionKey,
      senderId,
      teamId,
      channelId,
      threadTs,
      messageId,
      actionFingerprint: null,
      actionValue: null,
      actionToolCallId: null,
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

  async function rememberMailDraftAction(ctx, logger) {
    try {
      const interaction = assertPlainObject(
        ctx?.interaction,
        "Slack interactive payload",
      );
      const senderId = normalizeSlackId(ctx?.senderId, SLACK_USER_RE);
      const channelId = normalizeSlackId(
        ctx?.conversationId,
        SLACK_CHANNEL_RE,
      );
      const messageTs = canonicalSlackTimestamp(interaction.messageTs);
      const contextThread = optionalSlackTimestamp(ctx?.threadId);
      const interactionThread = optionalSlackTimestamp(interaction.threadTs);
      const actionValue = canonicalDraftToken(interaction.value);
      const triggerId = nonBlank(interaction.triggerId, 512);
      const interactionId = nonBlank(ctx?.interactionId, 2048);
      const expectedInteractionId =
        senderId &&
        channelId &&
        messageTs &&
        triggerId &&
        actionValue
          ? [
              senderId,
              channelId,
              messageTs,
              triggerId,
              MAIL_DRAFT_ACTION_ID,
              actionValue,
            ].join(":")
          : null;
      if (
        ctx?.channel !== "slack" ||
        ctx?.auth?.isAuthorizedSender !== true ||
        interaction.kind !== "button" ||
        interaction.actionId !== MAIL_DRAFT_ACTION_ID ||
        interaction.namespace !== MAIL_DRAFT_ACTION_ID ||
        !actionValue ||
        interaction.payload !== actionValue ||
        interaction.data !== `${MAIL_DRAFT_ACTION_ID}:${actionValue}` ||
        !senderId ||
        !channelId ||
        !messageTs ||
        !contextThread.valid ||
        !interactionThread.valid ||
        (contextThread.value !== null &&
          interactionThread.value !== null &&
          contextThread.value !== interactionThread.value) ||
        !expectedInteractionId ||
        interactionId !== expectedInteractionId
      ) {
        logger?.warn?.(
          `${PLUGIN_ID}: rejected incomplete or unauthorized Slack mail action`,
        );
        return {handled: true};
      }
      const nowMs = now();
      pruneState(nowMs);
      const threadTs = interactionThread.value ?? contextThread.value;
      const fingerprint = actionFingerprint({
        senderId,
        teamId: expectedTeamId,
        channelId,
        messageTs,
        threadTs,
        actionId: MAIL_DRAFT_ACTION_ID,
        actionValue,
      });
      if (seenActions.has(fingerprint) || pendingActions.has(fingerprint)) {
        logger?.warn?.(`${PLUGIN_ID}: rejected replayed Slack mail action`);
        return {handled: true};
      }
      const ingress = {
        ingressKind: "action",
        pendingKey: fingerprint,
        sessionKey: null,
        senderId,
        teamId: expectedTeamId,
        channelId,
        threadTs,
        messageId: messageTs,
        actionFingerprint: fingerprint,
        actionValue,
        actionToolCallId: null,
        sessionSha256: null,
        receivedAtMs: nowMs,
      };
      seenActions.set(fingerprint, nowMs);
      pendingActions.set(fingerprint, ingress);
      // handled:false deliberately preserves OpenClaw's fixed-runtime
      // system-event + immediate-heartbeat path after authoritative capture.
      return {handled: false};
    } catch {
      logger?.warn?.(`${PLUGIN_ID}: rejected malformed Slack mail action`);
      return {handled: true};
    }
  }

  function bindMailDraftActionRun(event, ctx, logger) {
    const runId = canonicalInvocationId(ctx?.runId);
    const sessionKey = nonBlank(ctx?.sessionKey, 2048);
    const channelId = consistentSlackChannel([
      ctx?.conversationId,
      ctx?.channelId,
      ctx?.chatId,
      ctx?.channel,
    ]);
    if (!runId || !sessionKey || !channelId) {
      if (runId) rejectRun(runId, now());
      logger?.warn?.(
        `${PLUGIN_ID}: rejected incomplete Slack action heartbeat run`,
      );
      return;
    }
    const nowMs = now();
    pruneState(nowMs);
    const actionEvent = parseMailDraftSystemEvent(event?.prompt);
    if (
      !actionEvent ||
      actionEvent.teamId !== expectedTeamId ||
      actionEvent.channelId !== channelId
    ) {
      rejectRun(runId, nowMs);
      logger?.warn?.(
        `${PLUGIN_ID}: heartbeat has no exact authoritative Slack mail action`,
      );
      return;
    }
    const fingerprint = actionFingerprint(actionEvent);
    const existing = ingressByRun.get(runId);
    if (existing) {
      if (
        existing.ingressKind !== "action" ||
        existing.actionFingerprint !== fingerprint ||
        existing.sessionKey !== sessionKey ||
        existing.channelId !== channelId
      ) {
        rejectRun(runId, nowMs);
        logger?.warn?.(
          `${PLUGIN_ID}: rejected mismatched repeated Slack action run`,
        );
      }
      return;
    }
    const pending = pendingActions.get(fingerprint);
    if (
      !pending ||
      nowMs - pending.receivedAtMs > ACTION_CONTEXT_TTL_MS
    ) {
      rejectRun(runId, nowMs);
      logger?.warn?.(
        `${PLUGIN_ID}: Slack mail action is missing, replayed, or stale`,
      );
      return;
    }
    const ingress = {
      ...pending,
      sessionKey,
      sessionSha256: createHash("sha256")
        .update(sessionKey, "utf8")
        .digest("hex"),
    };
    if (!bindRun(runId, ingress)) {
      rejectRun(runId, nowMs, ingress);
      logger?.warn?.(
        `${PLUGIN_ID}: Slack mail action could not bind one unique run`,
      );
    }
  }

  function bindAgentRun(_event, ctx, logger) {
    if (String(ctx?.messageProvider ?? "").toLowerCase() !== "slack") return;
    if (ctx?.trigger === "heartbeat") {
      bindMailDraftActionRun(_event, ctx, logger);
      return;
    }
    const runId = canonicalInvocationId(ctx?.runId);
    const sessionKey = nonBlank(ctx?.sessionKey, 2048);
    const senderId = normalizeSlackId(ctx?.senderId, SLACK_USER_RE);
    // OpenClaw 2026.7.1 does not put conversationId on this agent-hook ctx.
    // buildAgentHookContextChannelFields puts the same session-key-derived value
    // in channelId and chatId: `c0b0pqd83n2:thread:<ts>` for a channel and
    // `U09CX1CCBLN` for a DM. channel is currently the provider name (`slack`);
    // conversationId and channel remain candidates in case a future ctx supplies
    // a conversation id through either field.
    const channelId = consistentSlackChannel([
      ctx?.conversationId,
      ctx?.channelId,
      ctx?.chatId,
      ctx?.channel,
    ]);
    if (!runId || !sessionKey || !senderId || !channelId) {
      const missing = [];
      if (!runId) missing.push("runId");
      if (!sessionKey) missing.push("sessionKey");
      if (!senderId) missing.push("senderId");
      if (!channelId) missing.push("channelId");
      const resolution = !channelId
        ? ` resolve=[${[
            ["conversationId", ctx?.conversationId],
            ["channelId", ctx?.channelId],
            ["chatId", ctx?.chatId],
            ["channel", ctx?.channel],
          ]
            .map(([field, value]) => {
              const status =
                typeof value !== "string"
                  ? "absent"
                  : resolveSlackChannel(value) === null
                    ? "unresolved"
                    : "ok";
              return `${field}:${status}`;
            })
            .join(",")}]`
        : "";
      logger?.warn?.(
        `${PLUGIN_ID}: rejected incomplete authoritative agent run` +
          ` (missing=[${missing.join(",")}]${resolution})`,
      );
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
    // Production measurement (2026-08-03): for a DM the two sides name the same
    // conversation differently. The inbound event only ever carries the peer
    // (`user:U…` → stored as `DM:U…`), while the agent-run ctx carries the real
    // DM channel id (`D…`). Both are valid names for the identical 1:1
    // conversation, so treat `D…` as equivalent to `DM:<senderId>` — and only
    // for the sender that every other check already pins. A cross-user or
    // cross-channel forgery still fails on senderId / sessionKey.
    const dmAlias =
      /^D[A-Z0-9]{8,}$/u.test(channelId) ? `DM:${senderId}` : null;
    const channelMatches = value =>
      value === channelId || (dmAlias !== null && value === dmAlias);
    const candidates = [...pendingByMessage.values()].filter(
      ingress =>
        ingress.sessionKey === sessionKey &&
        ingress.senderId === senderId &&
        channelMatches(ingress.channelId) &&
        nowMs - ingress.receivedAtMs <= INBOUND_CONTEXT_TTL_MS,
    );
    if (candidates.length === 1 && candidates[0].channelId !== channelId) {
      // Remember the run-side name too, so the tool gate can accept either
      // representation without re-deriving the sender-based alias.
      candidates[0].channelAliases = [candidates[0].channelId, channelId];
      // Claims must carry a real Slack conversation id: mcp's caller-claim
      // verifier pins `^[CDG][A-Z0-9]{8,}$` and rejects the internal `DM:U…`
      // matching alias (実測: caller_claim_rejected field=channel). When the
      // run ctx supplies the genuine `D…` for a DM bound via a user-only
      // inbound, promote it to the canonical id and keep the alias for gates.
      if (/^[CDG][A-Z0-9]{8,}$/u.test(channelId)) {
        candidates[0].channelId = channelId;
      }
    }
    if (candidates.length !== 1 || !bindRun(runId, candidates[0])) {
      rejectRun(runId, nowMs, candidates.length === 1 ? candidates[0] : null);
      // Distinguish "no inbound was ever recorded" (the usual downstream effect
      // of rememberInbound rejecting the message) from "several inbounds match"
      // and from "the single candidate was refused by bindRun". Without this
      // split the log looked the same in all three cases.
      const bindFailed = candidates.length === 1;
      // When nothing matched but inbounds are pending, report which of the three
      // join keys disagreed. Only per-key match counts are emitted, never the
      // values themselves, so caller identity stays out of the logs.
      let mismatch = "";
      if (candidates.length === 0 && pendingByMessage.size > 0) {
        const pend = [...pendingByMessage.values()];
        const sk = pend.filter(i => i.sessionKey === sessionKey).length;
        const sd = pend.filter(i => i.senderId === senderId).length;
        const ch = pend.filter(i => i.channelId === channelId).length;
        const fresh = pend.filter(
          i => nowMs - i.receivedAtMs <= INBOUND_CONTEXT_TTL_MS,
        ).length;
        // channelId だけが合わない場合は、両側の実値を出す。会話 id は Slack の
        // チャンネル/DM 識別子であって caller identity ではないので出力してよい。
        const seen = ch === 0 ? [...new Set(pend.map(i => i.channelId))].join(",") : "";
        mismatch =
          ` matchSessionKey=${sk} matchSenderId=${sd} matchChannelId=${ch} fresh=${fresh}` +
          (ch === 0 ? ` runChannelId=${channelId} pendingChannelIds=[${seen}]` : "");
      }
      logger?.warn?.(
        `${PLUGIN_ID}: agent run has no unique fresh Slack message binding` +
          ` (candidates=${candidates.length} pending=${pendingByMessage.size}` +
          `${bindFailed ? " bindRunRefused=true" : ""}${mismatch})`,
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
    // This is the same OpenClaw 2026.7.1 agent-hook ctx: conversationId is absent,
    // while buildAgentHookContextChannelFields puts one session-key-derived value
    // in both channelId and chatId (`c0b0pqd83n2:thread:<ts>` for a channel,
    // `U09CX1CCBLN` for a DM). channel is currently `slack`; conversationId and
    // channel remain candidates in case a future ctx supplies a conversation id.
    const channelId = consistentSlackChannel([
      ctx?.conversationId,
      ctx?.channelId,
      ctx?.chatId,
      ctx?.channel,
    ]);
    if (!sessionKey || !channelId) {
      return block("trusted Slack session or channel binding is missing");
    }
    const nowMs = now();
    pruneState(nowMs);
    const trusted = ingressByRun.get(eventRunId);
    const trustedTtl =
      trusted?.ingressKind === "action"
        ? ACTION_CONTEXT_TTL_MS
        : INBOUND_CONTEXT_TTL_MS;
    if (!trusted || nowMs - trusted.receivedAtMs > trustedTtl) {
      return block("trusted Slack run identity is missing or stale");
    }
    const trustedChannelMatches =
      trusted.channelId === channelId ||
      (Array.isArray(trusted.channelAliases) &&
        trusted.channelAliases.includes(channelId));
    if (!trusted.sessionKey || trusted.sessionKey !== sessionKey || !trustedChannelMatches) {
      return block("tool context does not match the bound Slack run");
    }
    const exactInvocationKey = invocationKey(eventRunId, eventToolCallId);
    if (consumedInvocations.has(exactInvocationKey)) {
      return block("tool invocation replay rejected");
    }
    if (trusted.ingressKind === "action") {
      if (tool !== MAIL_DRAFT_TOOL) {
        return block("Slack mail action cannot authorize another tool");
      }
      if (trusted.actionToolCallId !== null) {
        return block("Slack mail action was already consumed");
      }
    } else if (tool === MAIL_DRAFT_TOOL) {
      return block("mail_draft requires an authoritative Slack button action");
    }
    let params;
    let declaredContext;
    try {
      const suppliedParams = assertPlainObject(event.params, "tool params");
      params =
        trusted.ingressKind === "action"
          ? {
              ...suppliedParams,
              draft_token: trusted.actionValue,
            }
          : suppliedParams;
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
    const nonceBytes =
      trusted.ingressKind === "action"
        ? createHmac("sha256", secret)
            .update(
              `teamagent-slack-action-v1:${trusted.actionFingerprint}`,
              "ascii",
            )
            .digest()
            .subarray(0, 16)
        : randomBytesFn(16);
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
    // 第3層の権威条件 (a): この run で teamagent tool call が発生したことの記録。
    const priorToolCalls = toolCallsByRun.get(eventRunId)?.count ?? 0;
    // Map の挿入順は既存キーへの再 set では更新されない（実測）。
    // 退避が「最も古い記録から」になるよう、delete してから set する。
    toolCallsByRun.delete(eventRunId);
    toolCallsByRun.set(eventRunId, { count: priorToolCalls + 1, updatedAtMs: nowMs });
    if (trusted.ingressKind === "action") {
      trusted.actionToolCallId = eventToolCallId;
    }
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

  // 第3層防御。0 tool call のターンで捏造された連携 URL を、送信前に握り潰して
  // ハーネスへ「もう 1 パス」を要求する（＝定型文で返さず、実際に oauth_connect を呼ばせる）。
  // 上流契約: before_agent_finalize は lastAssistantMessage が非空のときだけ走り、
  // revise は runId x idempotencyKey の予算で必ず打ち切られる（openclaw 2026.7.1 実測）。
  function guardConnectUrlFabrication(event, ctx, logger) {
    const nowMs = Date.now();
    // finalize だけが走る経路でも台帳が育たないよう、ここでも掃除する。
    // pruneState 全体は呼ばない（容量超過の fail を握り潰して fail-open
    // させないため。掃除は第3層の台帳に限定する）。
    pruneConnectGuardState(nowMs);
    const eventRunId = canonicalInvocationId(event?.runId);
    const contextRunId = canonicalInvocationId(ctx?.runId);
    if (!eventRunId || !contextRunId || eventRunId !== contextRunId) return undefined;
    // (a) teamagent tool call が 1 件でもあれば、URL はツール発行でありうる。触らない。
    if ((toolCallsByRun.get(eventRunId)?.count ?? 0) > 0) return undefined;
    // (b) 本文が無ければ利用者にも何も届かない。
    if (typeof event?.lastAssistantMessage !== "string") return undefined;
    const reply = event.lastAssistantMessage.trim();
    if (!reply) return undefined;
    // (c) 連携 URL が含まれるときだけ介入する。
    const { kinds } = findFabricatedConnectUrlKinds(reply);
    if (kinds.length === 0) return undefined;
    // (d) 自前の予算。上流予算に依存せずループ不在を担保する。
    const revisions = connectRevisionsByRun.get(eventRunId)?.count ?? 0;
    if (revisions >= MAX_CONNECT_FABRICATION_REVISIONS) {
      logger?.warn?.(
        `${PLUGIN_ID}: connect_url_fabrication_blocked runId=${eventRunId} tool_calls=0 ` +
          `kinds=${kinds.join("+")} outcome=budget_exhausted revise_attempt=${revisions}`,
      );
      return undefined;
    }
    connectRevisionsByRun.set(eventRunId, { count: revisions + 1, updatedAtMs: nowMs });
    // G7: 本文・URL 実体・Slack 識別子は載せない（捏造 URL には user_id が埋まっていた）。
    logger?.warn?.(
      `${PLUGIN_ID}: connect_url_fabrication_blocked runId=${eventRunId} tool_calls=0 ` +
        `kinds=${kinds.join("+")} outcome=revised revise_attempt=${revisions + 1}`,
    );
    return {
      action: "revise",
      reason: CONNECT_FABRICATION_REASON,
      retry: {
        instruction: CONNECT_FABRICATION_INSTRUCTION,
        idempotencyKey: CONNECT_FABRICATION_RETRY_KEY,
        maxAttempts: MAX_CONNECT_FABRICATION_REVISIONS,
      },
    };
  }

  function releaseAgentRun(event, ctx) {
    const eventRunId = canonicalInvocationId(event?.runId);
    const contextRunId = canonicalInvocationId(ctx?.runId);
    if (!eventRunId || !contextRunId || eventRunId !== contextRunId) return;
    ingressByRun.delete(eventRunId);
    toolCallsByRun.delete(eventRunId);
    connectRevisionsByRun.delete(eventRunId);
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
      if (typeof api?.registerInteractiveHandler !== "function") {
        fail("fixed OpenClaw interactive handler API is unavailable");
      }
      api.registerInteractiveHandler({
        channel: "slack",
        namespace: MAIL_DRAFT_ACTION_ID,
        handler: ctx => rememberMailDraftAction(ctx, api.logger),
      });
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
      api.on("before_agent_finalize", (event, ctx) =>
        guardConnectUrlFabrication(event, ctx, api.logger),
      );
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
