// caller-identity プラグインの実物（dist/index.js）を、本番と同じ形の ctx で駆動して
// 「Slack のチャンネル／DM でツール呼び出しが通るか」を判定するプローブ。
//
// なぜ必要か（2026-08-07）:
//   チャンネル経路は 07-31 の導入以来ずっと全断していたが、
//   tests/scripts/test_openclaw_runtime_image.py の契約テストは
//   OPENCLAW_RUNTIME_TEST_IMAGE 未設定でスキップされ、CodeBuild のビルド経路
//   （infra/openclaw/build-bundle.sh）からも呼ばれない。
//   つまり「緑のまま7日間壊れていた」。ここは常に走る形で塞ぐ。
//
// ctx の形は上流 OpenClaw 2026.7.1 の実測値を焼き込んである（上流モジュールに依存しない）:
//   buildAgentHookContextChannelFields (core/dist/hook-agent-context-DPPRzCBU.js:52-61) は
//   {channel:"slack", messageProvider:"slack", channelId:X, chatId:X, senderId} を返し、
//   X はセッション鍵由来。チャンネルの app_mention では `:thread:<ts>` が付く。
//   agent の hook ctx に conversationId は存在しない。
import { readFileSync } from "node:fs";
import { createCallerIdentityPlugin } from
  "../../infra/openclaw/caller-identity-plugin/dist/index.js";

const TEAM = "T07MU5P2PBR";
const USER = "U09CX1CCBLN";
const CHANNEL = "C0B0PQD83N2";
const TS = "1785206176.940189";
const SECRET = "x".repeat(48);

function makePlugin() {
  const logs = [];
  const handlers = new Map();
  createCallerIdentityPlugin({
    env: { TEAMAGENT_CALLER_CLAIM_SECRET: SECRET, SLACK_TEAM_ID: TEAM },
  }).register({
    registerInteractiveHandler() {},
    logger: { warn: (m) => logs.push(String(m)) },
    on: (name, fn) => handlers.set(name, fn),
  });
  return { handlers, logs };
}

// 本番の実測形状。channelId と chatId は同一文字列であることが要点。
function agentCtxFields(conversationRawId) {
  return {
    channel: "slack",
    messageProvider: "slack",
    channelId: conversationRawId,
    chatId: conversationRawId,
    senderId: USER,
  };
}

function scenario({ sessionKey, inboundTo, inboundFrom, runRawId, toolChannelId }) {
  const { handlers, logs } = makePlugin();

  handlers.get("message_received")(
    {
      from: inboundFrom,
      senderId: USER,
      messageId: TS,
      metadata: { guildId: TEAM, to: inboundTo, originatingTo: inboundTo },
    },
    {
      channelId: "slack",
      conversationId: inboundTo,
      sessionKey,
      senderId: USER,
      messageId: TS,
    },
  );
  const inboundWarnings = logs.slice();

  handlers.get("before_model_resolve")(
    { prompt: "probe" },
    {
      runId: "run-1",
      agentId: "teamagent",
      sessionKey,
      sessionId: "sid",
      trigger: "user",
      ...agentCtxFields(runRawId),
    },
  );

  const result = handlers.get("before_tool_call")(
    {
      toolName: "teamagent__search",
      runId: "run-1",
      toolCallId: "tc-1",
      params: { query: "q", _user_context: { slack_user_id: USER } },
    },
    {
      toolName: "teamagent__search",
      runId: "run-1",
      toolCallId: "tc-1",
      sessionKey,
      channelId: toolChannelId,
    },
  );

  let claimChannel = null;
  if (!result?.block) {
    const claim = result.params._user_context.caller_claim.split(".")[0];
    claimChannel = JSON.parse(Buffer.from(claim, "base64url").toString()).channel;
  }
  return {
    inboundAccepted: inboundWarnings.length === 0,
    blocked: Boolean(result?.block),
    blockReason: result?.blockReason ?? null,
    claimChannel,
    warnings: logs,
  };
}

// ── 第3層防御（連携 URL 捏造の封鎖）のシナリオ ──────────────────────────
// 本番実測 2026-08-31: 0 tool call のターンで本家ドメインの連携 URL が捏造され
// 利用者へ届いた。MCP 境界の決定論分岐は tool 呼び出し後にしか効かないため、
// before_agent_finalize が最後の砦になる。ここは常に走る形で塞ぐ。
const CONNECT_URL_PATTERNS = JSON.parse(
  readFileSync(new URL("../fixtures/connect_url_patterns.json", import.meta.url), "utf8"),
);
const FABRICATED_REPLY = [
  "Google と Slack の連携リンクをお出しします。開いて「許可」を押すと完了です。",
  "Google 連携: https://connect.openclaw.ai/oauth/google?user_id=U09MBDFQ16J",
  "Slack 連携: https://connect.openclaw.ai/oauth/slack?user_id=U09MBDFQ16J",
].join("\n");

function connectGuardScenario({
  lastAssistantMessage,
  toolName = null,
  runId = "run-1",
  finalizeRunId = "run-1",
  finalizeCtxRunId = null,
  repeat = 1,
} = {}) {
  const { handlers, logs } = makePlugin();
  const sessionKey = DM_SESSION_KEY;

  handlers.get("message_received")(
    {
      from: `slack:${USER}`,
      senderId: USER,
      messageId: TS,
      metadata: { guildId: TEAM, to: `user:${USER}`, originatingTo: `user:${USER}` },
    },
    { channelId: "slack", conversationId: `user:${USER}`, sessionKey, senderId: USER, messageId: TS },
  );
  handlers.get("before_model_resolve")(
    { prompt: "probe" },
    {
      runId,
      agentId: "teamagent",
      sessionKey,
      sessionId: "sid",
      trigger: "user",
      ...agentCtxFields(USER),
    },
  );

  let toolBlocked = null;
  if (toolName) {
    const toolResult = handlers.get("before_tool_call")(
      {
        toolName,
        runId,
        toolCallId: "tc-1",
        params: { _user_context: { slack_user_id: USER } },
      },
      { toolName, runId, toolCallId: "tc-1", sessionKey, channelId: `user:${USER}` },
    );
    toolBlocked = Boolean(toolResult?.block);
  }

  const results = [];
  for (let i = 0; i < repeat; i += 1) {
    results.push(
      handlers.get("before_agent_finalize")(
        { runId: finalizeRunId, sessionId: "sid", stopHookActive: false, lastAssistantMessage },
        {
          runId: finalizeCtxRunId ?? finalizeRunId,
          agentId: "teamagent",
          sessionKey,
          sessionId: "sid",
        },
      ) ?? null,
    );
  }
  const last = results[results.length - 1];
  return {
    toolBlocked,
    intervened: last?.action === "revise",
    action: last?.action ?? null,
    instruction: last?.retry?.instruction ?? null,
    idempotencyKey: last?.retry?.idempotencyKey ?? null,
    maxAttempts: last?.retry?.maxAttempts ?? null,
    reason: last?.reason ?? null,
    firstIntervened: results[0]?.action === "revise",
    logs,
  };
}

// agent_end が発火しない run（abort/crash/timeout 経路）を大量に流しても、
// 第3層の run 台帳が無制限に育たないこと。掃除を agent_end だけに任せていると
// 長寿命プロセスでリークする（レビュー指摘 2026-08-31）。
function connectGuardLedgerBound({ floodRuns = 1100 } = {}) {
  const { handlers } = makePlugin();
  const finalize = (runId) =>
    handlers.get("before_agent_finalize")(
      { runId, sessionId: "sid", stopHookActive: false, lastAssistantMessage: FABRICATED_REPLY },
      { runId, agentId: "teamagent", sessionKey: DM_SESSION_KEY, sessionId: "sid" },
    ) ?? null;

  const firstRunId = "run-flood-first";
  const firstIntervened = finalize(firstRunId)?.action === "revise";
  // 同じ run の 2 回目は自前予算で止まる（台帳が生きている証明）。
  const secondBlockedByBudget = finalize(firstRunId) === null;

  let threw = null;
  try {
    // agent_end を一切呼ばずに上限超の run を流す。
    for (let i = 0; i < floodRuns; i += 1) finalize(`run-flood-${i}`);
  } catch (error) {
    threw = String(error);
  }
  // 上限退避で最古の記録が落ちているなら、最初の run は再び介入できる。
  // 台帳が無制限に育つ実装ではここが false のままになる。
  const firstEvicted = finalize(firstRunId)?.action === "revise";
  return { firstIntervened, secondBlockedByBudget, threw, firstEvicted };
}

// TTL 掃除の駆動。上限退避（connectGuardLedgerBound）は「上限を超えたとき」しか
// 効かないので、少数 run が長時間残るケースはこちらで守る。時計を進めるだけで
// 実装側に試験用の seam を足さない。
function connectGuardTtlEviction({ advanceMs = 11 * 60 * 1000 } = {}) {
  const { handlers } = makePlugin();
  const finalize = (runId) =>
    handlers.get("before_agent_finalize")(
      { runId, sessionId: "sid", stopHookActive: false, lastAssistantMessage: FABRICATED_REPLY },
      { runId, agentId: "teamagent", sessionKey: DM_SESSION_KEY, sessionId: "sid" },
    ) ?? null;

  const runId = "run-ttl";
  const firstIntervened = finalize(runId)?.action === "revise";
  const blockedWhileFresh = finalize(runId) === null;

  const realNow = Date.now;
  let expiredIntervened = null;
  try {
    const base = realNow();
    Date.now = () => base + advanceMs;
    // 別 run の finalize が掃除を回し、TTL 超過の run-ttl を落とす。
    finalize("run-ttl-other");
    expiredIntervened = finalize(runId)?.action === "revise";
  } finally {
    Date.now = realNow;
  }
  return { firstIntervened, blockedWhileFresh, expiredIntervened };
}

// fixture（単一正本）の各 URL が、実装の判定と一致すること。
function connectUrlPatternMatrix() {
  const check = (entry, expectMatch) => {
    const reply = `連携はこちら ${entry.url} です`;
    const { handlers } = makePlugin();
    handlers.get("message_received")(
      {
        from: `slack:${USER}`,
        senderId: USER,
        messageId: TS,
        metadata: { guildId: TEAM, to: `user:${USER}`, originatingTo: `user:${USER}` },
      },
      { channelId: "slack", conversationId: `user:${USER}`, sessionKey: DM_SESSION_KEY, senderId: USER, messageId: TS },
    );
    handlers.get("before_model_resolve")(
      { prompt: "probe" },
      {
        runId: "run-1",
        agentId: "teamagent",
        sessionKey: DM_SESSION_KEY,
        sessionId: "sid",
        trigger: "user",
        ...agentCtxFields(USER),
      },
    );
    const out = handlers.get("before_agent_finalize")(
      { runId: "run-1", sessionId: "sid", stopHookActive: false, lastAssistantMessage: reply },
      { runId: "run-1", agentId: "teamagent", sessionKey: DM_SESSION_KEY, sessionId: "sid" },
    );
    return { url: entry.url, expectMatch, matched: out?.action === "revise" };
  };
  return [
    ...CONNECT_URL_PATTERNS.must_match.map((e) => check(e, true)),
    ...CONNECT_URL_PATTERNS.must_not_match.map((e) => check(e, false)),
  ];
}

const CHANNEL_SESSION_KEY =
  `agent:teamagent:slack:channel:${CHANNEL.toLowerCase()}:thread:${TS}`;
const DM_SESSION_KEY = `agent:teamagent:slack:direct:${USER.toLowerCase()}`;

// ── 連携依頼の 3 層防御（2026-09-03） ─────────────────────────────────────
// 本番実測: 利用者が DM で「連携」と送っても Aico がツールを呼ばず自作回答する事故が
// 同一 DM で 5 回以上。層1（before_agent_reply で oauth_connect を直接呼ぶ）、
// 層2（0 tool call × 短い連携依頼を revise）、層3（再パス後も 0 tool なら定型文置換）。
// hook の event/ctx 形状は上流 openclaw 2026.7.1 の実測（get-reply:5599-5617、
// dispatch:2528-2545、message-hook-mappers:23,221-235）を焼き込む。
const CONNECT_REQUEST_PHRASES = JSON.parse(
  readFileSync(new URL("../fixtures/connect_request_phrases.json", import.meta.url), "utf8"),
);
// Slack DM の実 conversation id。before_agent_reply の ctx では identity fields が
// chatId を NativeChannelId ?? ChatId（= Slack の message.channel）で上書きするので `D…` になる。
const DM_CHANNEL = "D0B0PQD83N3";
const BEARER = "b".repeat(48);
const OAUTH_CONNECT_MESSAGE = [
  "以下のリンクから連携してください（本人専用・1 回限り）。",
  "Google: https://connect.newstv.co.jp/oauth2/start?token=abc",
].join("\n");
const SELF_MADE_REPLY =
  "アカウントが未登録のようです。管理者にお問い合わせください。";
const LONG_CONNECT_REQUEST = "〇〇社との連携について提案書を作ってください";

// mcp（streamable-http）の偽物。rollout-task-canary.mjs と同じ手順を受ける。
// 本番の失敗モード（fetch 失敗 / HTTP 5xx / JSON-RPC error / tool の構造化エラー /
// message 欠落）をそれぞれ再現できる。
function makeMcpFake({ mode = "ok" } = {}) {
  const calls = [];
  const respond = (payload, headers = {}) =>
    new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "content-type": "application/json", ...headers },
    });
  const fetchFn = async (url, init) => {
    const body = JSON.parse(init.body);
    calls.push({ url, method: body.method, headers: init.headers, body });
    if (mode === "throw") throw new TypeError("fetch failed");
    if (mode === "http500") return new Response("boom", { status: 500 });
    if (body.method === "initialize") {
      return respond(
        {
          jsonrpc: "2.0",
          id: body.id,
          result: { protocolVersion: "2025-03-26", capabilities: {}, serverInfo: { name: "x", version: "1" } },
        },
        { "mcp-session-id": "sess-1" },
      );
    }
    if (body.method === "notifications/initialized") return new Response(null, { status: 202 });
    if (body.method === "tools/call") {
      if (mode === "rpc_error") {
        return respond({ jsonrpc: "2.0", id: body.id, error: { code: -32001, message: "x" } });
      }
      if (mode === "tool_error") {
        return respond({
          jsonrpc: "2.0",
          id: body.id,
          result: {
            content: [
              {
                type: "text",
                text: JSON.stringify({ error: "Caller authorization failed.", code: "CALLER_IDENTITY_REJECTED" }),
              },
            ],
          },
        });
      }
      if (mode === "no_message") {
        return respond({
          jsonrpc: "2.0",
          id: body.id,
          result: { content: [{ type: "text", text: JSON.stringify({ url: null }) }] },
        });
      }
      // 本番の streamable-http は text/event-stream で返しうる。SSE 形式で読めること。
      const text = JSON.stringify({
        jsonrpc: "2.0",
        id: body.id,
        result: {
          content: [
            {
              type: "text",
              text: JSON.stringify({
                url: "https://connect.newstv.co.jp/oauth2/start?token=abc",
                slack_url: null,
                user_email_masked: "u***@example.com",
                message: OAUTH_CONNECT_MESSAGE,
              }),
            },
          ],
        },
      });
      return new Response(`event: message\ndata: ${text}\n\n`, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    }
    return new Response("unexpected", { status: 400 });
  };
  return { fetchFn, calls };
}

function makeConnectPlugin({ mode, env = {} } = {}) {
  const logs = [];
  const infos = [];
  const handlers = new Map();
  const mcp = makeMcpFake({ mode });
  createCallerIdentityPlugin({
    env: {
      TEAMAGENT_CALLER_CLAIM_SECRET: SECRET,
      SLACK_TEAM_ID: TEAM,
      TEAMAGENT_MCP_BEARER: BEARER,
      TEAMAGENT_MCP_URL: "http://mcp.test/mcp",
      ...env,
    },
    fetchFn: mcp.fetchFn,
  }).register({
    registerInteractiveHandler() {},
    logger: { warn: (m) => logs.push(String(m)), info: (m) => infos.push(String(m)) },
    on: (name, fn) => handlers.set(name, fn),
  });
  return { handlers, logs, infos, calls: mcp.calls };
}

// message_received（message-hook-mappers:221-235 の形）。content は封筒無しの生本文。
function receiveDm(handlers, content, { runId, messageId = TS } = {}) {
  handlers.get("message_received")(
    {
      from: `slack:${USER}`,
      content,
      senderId: USER,
      messageId,
      ...(runId ? { runId } : {}),
      metadata: { guildId: TEAM, to: `user:${USER}`, originatingTo: `user:${USER}` },
    },
    {
      channelId: "slack",
      conversationId: `user:${USER}`,
      sessionKey: DM_SESSION_KEY,
      senderId: USER,
      messageId,
      ...(runId ? { runId } : {}),
    },
  );
}

// チャンネルスレッドの message_received（別の送信者）。
function receiveThreadMessage(handlers, content, { senderId, messageId }) {
  handlers.get("message_received")(
    {
      from: `slack:channel:${CHANNEL}`,
      content,
      senderId,
      messageId,
      threadId: TS,
      metadata: { guildId: TEAM, to: `channel:${CHANNEL}`, originatingTo: `channel:${CHANNEL}`, threadId: TS },
    },
    {
      channelId: "slack",
      conversationId: `channel:${CHANNEL}`,
      sessionKey: CHANNEL_SESSION_KEY,
      senderId,
      messageId,
    },
  );
}

// before_agent_reply の ctx（get-reply:5599-5617）。channel fields の後に identity fields が
// 展開されるため chatId は `D…`、channelId はセッション鍵/`user:U…` 由来の `U…` のまま。
function beforeAgentReplyCtx({ chatId = DM_CHANNEL, trigger = "user" } = {}) {
  return {
    agentId: "teamagent",
    sessionKey: DM_SESSION_KEY,
    sessionId: "sid",
    workspaceDir: "/w",
    trigger,
    ...agentCtxFields(USER),
    chatId,
    channelContext: { sender: { id: USER }, chat: { id: chatId } },
  };
}

function finalizeRun(handlers, { runId = "run-1", ctxRunId = null, lastAssistantMessage } = {}) {
  return (
    handlers.get("before_agent_finalize")(
      { runId, sessionId: "sid", stopHookActive: false, lastAssistantMessage },
      { runId: ctxRunId ?? runId, agentId: "teamagent", sessionKey: DM_SESSION_KEY, sessionId: "sid" },
    ) ?? null
  );
}

function startRun(handlers, runId = "run-1") {
  handlers.get("before_model_resolve")(
    { prompt: "probe" },
    { runId, agentId: "teamagent", sessionKey: DM_SESSION_KEY, sessionId: "sid", trigger: "user", ...agentCtxFields(USER) },
  );
}

function callTool(handlers, toolName, runId = "run-1") {
  const result = handlers.get("before_tool_call")(
    { toolName, runId, toolCallId: "tc-1", params: { _user_context: { slack_user_id: USER } } },
    { toolName, runId, toolCallId: "tc-1", sessionKey: DM_SESSION_KEY, channelId: `user:${USER}` },
  );
  return Boolean(result?.block);
}

// reply_payload_sending（dispatch:2528-2545）。event.runId と ctx.runId に同じ run id が載る。
function deliverPayload(handlers, payload, { runId = "run-1", ctxRunId = null } = {}) {
  return (
    handlers.get("reply_payload_sending")(
      { payload, kind: "final", channel: "slack", sessionKey: DM_SESSION_KEY, runId },
      { channelId: "slack", conversationId: `user:${USER}`, sessionKey: DM_SESSION_KEY, runId: ctxRunId ?? runId },
    ) ?? null
  );
}

// 層1 のシナリオ。message_received → before_agent_reply。
async function deterministicScenario({
  content,
  mode = "ok",
  env,
  chatId,
  trigger,
  runId,
  repeat = 1,
} = {}) {
  const plugin = makeConnectPlugin({ mode, env });
  const { handlers, logs, infos, calls } = plugin;
  receiveDm(handlers, content, { runId });
  const results = [];
  for (let i = 0; i < repeat; i += 1) {
    results.push(
      (await handlers.get("before_agent_reply")(
        { cleanedBody: content },
        beforeAgentReplyCtx({ chatId, trigger }),
      )) ?? null,
    );
  }
  const last = results[results.length - 1];
  const toolCalls = calls.filter((c) => c.method === "tools/call");
  const first = toolCalls[0] ?? null;
  const claim = first?.body.params.arguments?._user_context?.caller_claim ?? null;
  const claimPayload = claim
    ? JSON.parse(Buffer.from(claim.split(".")[0], "base64url").toString())
    : null;
  return {
    plugin,
    handled: last?.handled === true,
    replyText: last?.reply?.text ?? null,
    toolCallCount: toolCalls.length,
    toolName: first?.body.params.name ?? null,
    methods: calls.map((c) => c.method),
    url: first?.url ?? null,
    bearerHeader: first?.headers.Authorization ?? null,
    sessionHeader: first?.headers["Mcp-Session-Id"] ?? null,
    claimPayload,
    declaredContext: first?.body.params.arguments?._user_context ?? null,
    logs,
    infos,
  };
}

// 層1 成功後の次の受信で run 束縛とツール呼び出しが通ること（レビュー指摘 1）。
// handled で返すとモデルが起動せず before_model_resolve が走らないため、層1 が受信を
// pending に残すと次の受信で bindAgentRun が candidates=2 で run を拒否し全ツールが止まる。
async function nextMessageAfterDeterministic() {
  const l1 = await deterministicScenario({ content: "連携" });
  const { handlers, logs } = l1.plugin;
  const nextTs = "1785206200.000001";
  receiveDm(handlers, "今日の予定を教えて", { messageId: nextTs });
  startRun(handlers, "run-2");
  const blocked = callTool(handlers, "teamagent__search", "run-2");
  return {
    l1Handled: l1.handled,
    nextRunRejected: logs.some((m) => m.includes("no unique fresh Slack message binding")),
    nextToolBlocked: blocked,
    logs,
  };
}

// 層1 成功後 30 秒以内の再度の「連携」でも層1 が再び handled になること（レビュー指摘 2）。
async function repeatConnectAfterDeterministic({ advanceMs = 30 * 1000 } = {}) {
  const l1 = await deterministicScenario({ content: "連携" });
  const { handlers, logs, calls } = l1.plugin;
  const realNow = Date.now;
  let secondHandled = null;
  try {
    const base = realNow();
    Date.now = () => base + advanceMs;
    receiveDm(handlers, "連携", { messageId: "1785206206.000002" });
    const second = await handlers.get("before_agent_reply")(
      { cleanedBody: "連携" },
      beforeAgentReplyCtx(),
    );
    secondHandled = second?.handled === true;
  } finally {
    Date.now = realNow;
  }
  return {
    firstHandled: l1.handled,
    secondHandled,
    toolCallCount: calls.filter((c) => c.method === "tools/call").length,
    ambiguous: logs.some((m) => m.includes("reason=ambiguous_ingress")),
    logs,
  };
}

// チャンネルスレッドで別の送信者 B の「連携」が pending のとき、A の before_agent_reply が
// B の claim で B 専用リンクを A に返さないこと（レビュー指摘 3）。
async function otherSenderPendingInThread() {
  const OTHER = "U0AAAAAAAAB";
  const { handlers, calls, logs } = makeConnectPlugin({});
  receiveThreadMessage(handlers, "連携", { senderId: OTHER, messageId: "1785206210.000003" });
  const rawId = `${CHANNEL.toLowerCase()}:thread:${TS}`;
  const result =
    (await handlers.get("before_agent_reply")(
      { cleanedBody: "連携" },
      {
        agentId: "teamagent",
        sessionKey: CHANNEL_SESSION_KEY,
        sessionId: "sid",
        workspaceDir: "/w",
        trigger: "user",
        ...agentCtxFields(rawId),
        chatId: CHANNEL,
        channelContext: { sender: { id: USER }, chat: { id: CHANNEL } },
      },
    )) ?? null;
  return {
    handled: result?.handled === true,
    toolCallCount: calls.filter((c) => c.method === "tools/call").length,
    logs,
  };
}

// 層1 が失敗したとき、同じ受信がモデル経路へ進み層2 が受けること（fail-to-next-layer）。
async function fallthroughThenLayer2({ mode, env, chatId } = {}) {
  const l1 = await deterministicScenario({ content: "連携", mode, env, chatId });
  const { handlers } = l1.plugin;
  startRun(handlers);
  const finalize = finalizeRun(handlers, { lastAssistantMessage: SELF_MADE_REPLY });
  return {
    l1Handled: l1.handled,
    toolCallCount: l1.toolCallCount,
    fallthroughReason:
      l1.logs.find((m) => m.includes("outcome=fallthrough"))?.match(/reason=(\S+)/u)?.[1] ?? null,
    layer2Intervened: finalize?.action === "revise",
    layer2Key: finalize?.retry?.idempotencyKey ?? null,
    logs: l1.logs,
  };
}

// 層2/層3 のシナリオ。message_received(content) → run → (tool) → finalize×N → 配信。
function zeroToolConnectScenario({
  content,
  lastAssistantMessage = SELF_MADE_REPLY,
  toolName = null,
  repeat = 1,
  deliverPayloads = [],
  deliverCtxRunId = null,
} = {}) {
  const { handlers, logs } = makeConnectPlugin({});
  receiveDm(handlers, content);
  startRun(handlers);
  const toolBlocked = toolName ? callTool(handlers, toolName) : null;
  const results = [];
  for (let i = 0; i < repeat; i += 1) {
    results.push(finalizeRun(handlers, { lastAssistantMessage }));
  }
  const deliveries = deliverPayloads.map((payload) =>
    deliverPayload(handlers, payload, { ctxRunId: deliverCtxRunId }),
  );
  const last = results[results.length - 1];
  return {
    toolBlocked,
    firstIntervened: results[0]?.action === "revise",
    intervened: last?.action === "revise",
    instruction: last?.retry?.instruction ?? null,
    idempotencyKey: last?.retry?.idempotencyKey ?? null,
    maxAttempts: last?.retry?.maxAttempts ?? null,
    reason: last?.reason ?? null,
    deliveries,
    logs,
  };
}

// fixture（単一正本）の各文言が、層1 の実経路（message_received → before_agent_reply）で
// 「oauth_connect が 1 回呼ばれ、戻り値 message がそのまま返る」こと。
// must_not_match は層1 を通らず（tools/call 0 件・handled 無し）通常処理に進むこと。
async function connectPhraseMatrix() {
  const rows = [];
  const check = async (entry, expectMatch) => {
    const r = await deterministicScenario({ content: entry.text });
    rows.push({
      text: entry.text,
      expectMatch,
      handled: r.handled,
      toolCallCount: r.toolCallCount,
      toolName: r.toolName,
      replyIsToolMessage: r.replyText === OAUTH_CONNECT_MESSAGE,
    });
  };
  for (const entry of CONNECT_REQUEST_PHRASES.must_match) await check(entry, true);
  for (const entry of CONNECT_REQUEST_PHRASES.must_not_match) await check(entry, false);
  return rows;
}

const connectL1Short = await deterministicScenario({ content: "連携" });
const connectL1Report = {
  handled: connectL1Short.handled,
  replyText: connectL1Short.replyText,
  toolCallCount: connectL1Short.toolCallCount,
  toolName: connectL1Short.toolName,
  methods: connectL1Short.methods,
  url: connectL1Short.url,
  bearerHeader: connectL1Short.bearerHeader,
  sessionHeader: connectL1Short.sessionHeader,
  claimPayload: connectL1Short.claimPayload,
  declaredContext: connectL1Short.declaredContext,
  logs: connectL1Short.logs,
  infos: connectL1Short.infos,
};
const connectL1Long = await deterministicScenario({ content: LONG_CONNECT_REQUEST });
const connectL1Repeat = await deterministicScenario({ content: "連携", repeat: 2 });
const connectL1BoundRun = await deterministicScenario({ content: "連携", runId: "run-1" });
const connectL1Heartbeat = await deterministicScenario({ content: "連携", trigger: "heartbeat" });
const connectL1Failures = {
  fetch_throws: await fallthroughThenLayer2({ mode: "throw" }),
  http_500: await fallthroughThenLayer2({ mode: "http500" }),
  rpc_error: await fallthroughThenLayer2({ mode: "rpc_error" }),
  tool_error: await fallthroughThenLayer2({ mode: "tool_error" }),
  no_message: await fallthroughThenLayer2({ mode: "no_message" }),
  no_bearer: await fallthroughThenLayer2({ env: { TEAMAGENT_MCP_BEARER: "" } }),
  no_canonical_channel: await fallthroughThenLayer2({ chatId: USER }),
};
const connectPhraseRows = await connectPhraseMatrix();
const connectL1NextMessage = await nextMessageAfterDeterministic();
const connectL1RepeatAfter30s = await repeatConnectAfterDeterministic();
const connectL1OtherSender = await otherSenderPendingInThread();

const report = {
  // チャンネルの app_mention。run ctx は `c0b0pqd83n2:thread:<ts>`（本番実測）。
  channel_threaded: scenario({
    sessionKey: CHANNEL_SESSION_KEY,
    inboundTo: `channel:${CHANNEL}`,
    inboundFrom: `slack:channel:${CHANNEL}`,
    runRawId: `${CHANNEL.toLowerCase()}:thread:${TS}`,
    toolChannelId: `channel:${CHANNEL}`,
  }),
  // DM。kind=direct なので上流はセッション鍵を解析せず currentChannelId に落ちる。
  direct_message: scenario({
    sessionKey: DM_SESSION_KEY,
    inboundTo: `user:${USER}`,
    inboundFrom: `slack:${USER}`,
    runRawId: USER,
    toolChannelId: `user:${USER}`,
  }),
  // 会話 id を含まない不正なサフィックス。fail-closed であること。
  channel_malformed_suffix: scenario({
    sessionKey: CHANNEL_SESSION_KEY,
    inboundTo: `channel:${CHANNEL}`,
    inboundFrom: `slack:channel:${CHANNEL}`,
    runRawId: `${CHANNEL.toLowerCase()}:thread:${TS}:extra`,
    toolChannelId: `channel:${CHANNEL}`,
  }),
  // サフィックス除去を `:thread:` 以外へ広げていないこと。
  // 貪欲な正規表現（任意の2セグメントを剥がす）にすると、ここが誤って通ってしまう。
  channel_unknown_suffix: scenario({
    sessionKey: CHANNEL_SESSION_KEY,
    inboundTo: `channel:${CHANNEL}`,
    inboundFrom: `slack:channel:${CHANNEL}`,
    runRawId: `${CHANNEL.toLowerCase()}:reply:${TS}`,
    toolChannelId: `channel:${CHANNEL}`,
  }),
  // 空の thread 値は上流が作らない。受理すると入力面が広がるので拒否のままにする。
  // 除去の正規表現を `[^:]+` から `[^:]*` に緩めると、ここが通ってしまう。
  channel_empty_thread: scenario({
    sessionKey: CHANNEL_SESSION_KEY,
    inboundTo: `channel:${CHANNEL}`,
    inboundFrom: `slack:channel:${CHANNEL}`,
    runRawId: `${CHANNEL.toLowerCase()}:thread:`,
    toolChannelId: `channel:${CHANNEL}`,
  }),
  // DM 側にサフィックス除去を波及させていないこと。
  // DM ブランチを除去後の値で照合するようにすると、ここが誤って通ってしまう。
  dm_with_thread_suffix: scenario({
    sessionKey: DM_SESSION_KEY,
    inboundTo: `user:${USER}`,
    inboundFrom: `slack:${USER}`,
    runRawId: `${USER.toLowerCase()}:thread:${TS}`,
    toolChannelId: `user:${USER}`,
  }),
  // ── 第3層防御 ──────────────────────────────────────────────────────
  // ①0 tool call ＋ 捏造 URL → revise を返し、instruction がツール実行を明示要求する。
  connect_fabricated_zero_tool: connectGuardScenario({
    lastAssistantMessage: FABRICATED_REPLY,
  }),
  // ②0 tool call ＋ URL 無し（雑談）→ 不介入。intent ではなく出力を見ている証明。
  connect_no_url_zero_tool: connectGuardScenario({
    lastAssistantMessage: "こんにちは。今日のご予定について何かお手伝いできますか？",
  }),
  // ③tool call あり（search）＋ 一般 URL → 不介入。
  connect_other_tool_generic_url: connectGuardScenario({
    lastAssistantMessage: "資料はこちらです https://example.com/help をご覧ください",
    toolName: "teamagent__search",
  }),
  // ④oauth_connect を呼んだ run ＋ 正規 URL → 不介入（条件(a)で除外されること）。
  connect_after_oauth_connect: connectGuardScenario({
    lastAssistantMessage:
      "連携リンクです https://connect.newstv.co.jp/oauth2/callback?state=x を開いてください",
    toolName: "teamagent__oauth_connect",
  }),
  // ⑤同一 run で 2 回目 → 自前予算で不介入（上流予算に依存しないループ不在の担保）。
  connect_budget_exhausted: connectGuardScenario({
    lastAssistantMessage: FABRICATED_REPLY,
    repeat: 2,
  }),
  // ⑥lastAssistantMessage 未定義 → 不介入（fail-open）。
  connect_missing_message: connectGuardScenario({
    lastAssistantMessage: undefined,
  }),
  // ⑦event と ctx の runId が食い違う finalize → 不介入。
  //   （signToolCall と同じ「権威 run 束縛」の規律を、この hook でも緩めていないこと）
  connect_run_mismatch: connectGuardScenario({
    lastAssistantMessage: FABRICATED_REPLY,
    finalizeRunId: "run-2",
    finalizeCtxRunId: "run-1",
  }),
  // ⑦' 別 run（その run 自身は 0 tool call）の finalize は介入する。
  //   カウンタが run 単位であることの明示。前段の run が tool を呼んでいても、
  //   捏造したのは別 run なので見逃さない。
  connect_other_run_zero_tool: connectGuardScenario({
    lastAssistantMessage: FABRICATED_REPLY,
    toolName: "teamagent__search",
    finalizeRunId: "run-2",
  }),
  // ⑧revise 後のパスで oauth_connect が呼ばれ counter>=1 になった run では再介入しない。
  //   ループ不在を上流挙動でなく自前条件 (a) で担保していることの証明（レビュー指摘）。
  connect_recovered_after_tool_call: (() => {
    const first = connectGuardScenario({ lastAssistantMessage: FABRICATED_REPLY });
    const recovered = connectGuardScenario({
      lastAssistantMessage: FABRICATED_REPLY,
      toolName: "teamagent__oauth_connect",
    });
    return { firstIntervened: first.intervened, recoveredIntervened: recovered.intervened };
  })(),
  // ⑨agent_end 無しで大量 run を流しても台帳が上限を超えないこと。
  connect_ledger_bound: connectGuardLedgerBound(),
  // ⑩TTL 超過の run 記録が掃除されること（少数 run の長期残留）。
  connect_ledger_ttl: connectGuardTtlEviction(),
  // fixture（単一正本）と実装の一致。
  connect_url_pattern_matrix: connectUrlPatternMatrix(),
  // ── 連携依頼の 3 層防御（2026-09-03） ──────────────────────────────────
  // 層1 ①短い連携依頼 → モデルを通さず oauth_connect を 1 回呼び、message をそのまま返す。
  connect_l1_short_request: connectL1Report,
  // 層1 ②長文「〇〇社との連携について提案書を」→ 層1 を通らず通常処理へ。
  connect_l1_long_request: {
    handled: connectL1Long.handled,
    toolCallCount: connectL1Long.toolCallCount,
    logs: connectL1Long.logs,
  },
  // 層1 ③同じ受信に対して 2 度は鋳造しない。
  connect_l1_repeat_same_message: {
    handled: connectL1Repeat.handled,
    toolCallCount: connectL1Repeat.toolCallCount,
  },
  // 層1 ④message_received が runId を伴い先に run へ束縛されても層1 が見つけること。
  connect_l1_bound_run: {
    handled: connectL1BoundRun.handled,
    toolCallCount: connectL1BoundRun.toolCallCount,
  },
  // 層1 ⑤heartbeat 等の非利用者トリガでは動かない。
  connect_l1_non_user_trigger: {
    handled: connectL1Heartbeat.handled,
    toolCallCount: connectL1Heartbeat.toolCallCount,
  },
  // 層1 ⑥失敗はすべて次の層へ落ちる（fail-open ではなく fail-to-next-layer）。
  connect_l1_failures: connectL1Failures,
  // 層1 ⑦handled の後、同じ DM の次の受信で run 束縛とツール呼び出しが通る（pending 残留なし）。
  connect_l1_next_message_tools_ok: {
    l1Handled: connectL1NextMessage.l1Handled,
    nextRunRejected: connectL1NextMessage.nextRunRejected,
    nextToolBlocked: connectL1NextMessage.nextToolBlocked,
  },
  // 層1 ⑧handled の 30 秒後に再度「連携」→ 層1 が再び handled（無言で不発にならない）。
  connect_l1_repeat_after_30s: {
    firstHandled: connectL1RepeatAfter30s.firstHandled,
    secondHandled: connectL1RepeatAfter30s.secondHandled,
    toolCallCount: connectL1RepeatAfter30s.toolCallCount,
    ambiguous: connectL1RepeatAfter30s.ambiguous,
  },
  // 層1 ⑨スレッドで別送信者の「連携」が pending でも、A には B の claim で返さない。
  connect_l1_other_sender_pending: connectL1OtherSender,
  // 層2 ①0 tool call × 短い連携依頼 → revise（固定 instruction）。
  connect_zero_tool_short_request: zeroToolConnectScenario({ content: "連携" }),
  // 層2 ②長文 × 0 tool call → 不介入（誤爆しない）。
  connect_zero_tool_long_request: zeroToolConnectScenario({ content: LONG_CONNECT_REQUEST }),
  // 層2 ③短い連携依頼 × oauth_connect 呼び出しあり → 不介入。
  connect_zero_tool_with_tool_call: zeroToolConnectScenario({
    content: "連携",
    toolName: "teamagent__oauth_connect",
    lastAssistantMessage: OAUTH_CONNECT_MESSAGE,
  }),
  // 層3 ①再パス後も 0 tool call → 予算切れで層3 を武装し、送信直前に定型文へ置換。
  //     2 通目（分割 payload）は取り消す。
  connect_zero_tool_fallback: zeroToolConnectScenario({
    content: "連携",
    repeat: 2,
    deliverPayloads: [{ text: SELF_MADE_REPLY }, { text: "（続き）" }],
  }),
  // 層3 ②event と ctx の runId が食い違う配信には触らない。
  connect_zero_tool_fallback_run_mismatch: zeroToolConnectScenario({
    content: "連携",
    repeat: 2,
    deliverPayloads: [{ text: SELF_MADE_REPLY }],
    deliverCtxRunId: "run-2",
  }),
  // 層3 ③武装していない run の配信には触らない（雑談の応答が消えない）。
  connect_zero_tool_fallback_not_armed: zeroToolConnectScenario({
    content: "こんにちは",
    lastAssistantMessage: "こんにちは。何かお手伝いできますか？",
    deliverPayloads: [{ text: "こんにちは。何かお手伝いできますか？" }],
  }),
  // fixture（単一正本）と層1 実経路の一致。
  connect_phrase_matrix: connectPhraseRows,
};

process.stdout.write(JSON.stringify(report, null, 2) + "\n");
