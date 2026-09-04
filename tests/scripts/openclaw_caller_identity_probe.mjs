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
import {
  createCallerIdentityPlugin,
  unwrapToolArguments,
  REGISTERED_HOOKS,
  connectRequestShape,
  classifyConnectRequest,
} from
  "../../infra/openclaw/caller-identity-plugin/dist/index.js";

// ── console の捕捉（2026-09-03）───────────────────────────────────────────
// plugin は logger（上流の subsystem logger）に加えて console にも同じ 1 行を書く。
// 上流 logger が no-op に差し替わる経路（bundled-capability-runtime）や
// consoleLevel=error では logger 側が消えるため、console 側が最後の砦になる。
// このレポート自身は process.stdout.write で書くので、console を横取りしても JSON は壊れない。
// 捕捉は import 直後（どのシナリオより前）に張る。
const consoleLines = [];
for (const level of ["log", "info", "warn", "error", "debug"]) {
  const original = console[level]?.bind(console);
  console[level] = (...args) => {
    consoleLines.push({ level, text: args.map((a) => String(a)).join(" ") });
    void original;
  };
}
function captureConsole(run) {
  const before = consoleLines.length;
  const value = run();
  return { value, console: consoleLines.slice(before) };
}

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

// フックごとの `hook first_fired` は **プロセス内で 1 回だけ**出る観測行であって、
// 呼び出しごとの騒音ではない。拒否/正常系の「1 行だけ」を測る前に、その 1 回を
// 消費しておく（= 定常状態を測る）。
// どの呼び出しも最初のガードで即 return する形にしてあるので、初回消費以外の副作用は無い:
//   message_received      … ctx.channelId が "slack" でないので rememberInbound が即 return
//   before_model_resolve  … messageProvider が slack でないので即 return
//   before_tool_call      … teamagent__ 接頭辞が無く canonicalToolName が null で即 return
function warmUpHooks(handlers) {
  handlers.get("message_received")?.({}, { channelId: "warmup" });
  handlers.get("before_model_resolve")?.({}, { messageProvider: "warmup" });
  handlers.get("before_tool_call")?.(
    { toolName: "chitchat", runId: "warm", toolCallId: "warm", params: {} },
    { toolName: "chitchat", runId: "warm", toolCallId: "warm" },
  );
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
      // 実際の受信は本文を持つ（連携依頼ではない通常の会話）。
      content: "テスト",
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
// plugin が叩く Slack Web API の基底（実装の SLACK_API_BASE と一致させる）。
const SLACK_API_BASE_URL = "https://slack.com/api";
// 既に両方連携済み / 片方だけ連携済み のときに mcp が返す message（skill.py:460-492）。
const OAUTH_ALREADY_CONNECTED_MESSAGE =
  "✅ *u***@example.com* は既に Google と Slack を連携済みです。追加の操作は不要です。そのまま話しかけてください。";
const OAUTH_PARTIAL_MESSAGE = [
  "👋 *u***@example.com* の連携リンクです（1回だけ・所要1分）。",
  "（Google は連携済みのため省略しています）",
  "*Slack を連携*（本人としての検索・チャンネル巡回）",
  "https://connect.newstv.co.jp/slack/oauth/start/abc",
].join("\n");
const OAUTH_CONNECT_MESSAGE = [
  "以下のリンクから連携してください（本人専用・1 回限り）。",
  "Google: https://connect.newstv.co.jp/oauth2/start?token=abc",
].join("\n");
const SELF_MADE_REPLY =
  "アカウントが未登録のようです。管理者にお問い合わせください。";
// mcp が新規ユーザー（Slack プロフィールに会社メールが無い）に返す実物の形。
// 失敗も成功と同じ TextContent の JSON で返り（server.py:442-445,819）、
// `error` の中身は既に利用者向けに整形済み（skill.py:228-236 /
// connect_diagnostics.py:260-277）。plugin はこれを一字も変えずに届ける。
const MCP_USER_FACING_ERROR = [
  "PermissionError: oauth_connect は本人 user_email が必須です（本人専用リンク）",
  "Slack プロフィールのメールアドレスが会社メールになっているか確認し、管理者へご連絡ください。",
  "解決しない場合は、次の 1 行をそのまま管理者（小俣）へ送ってください:",
  `診断: CONNECT-I02 2026-09-04 10:23 JST ${USER} req-1`,
].join("\n");
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
      if (mode === "user_error") {
        // 利用者向けに整形済みの失敗文面（新規ユーザーの CONNECT-I02）。
        return respond({
          jsonrpc: "2.0",
          id: body.id,
          result: {
            content: [
              {
                type: "text",
                text: JSON.stringify({ error: MCP_USER_FACING_ERROR, request_id: "req-1" }),
              },
            ],
          },
        });
      }
      if (mode === "already_connected" || mode === "partially_connected") {
        const connected = mode === "already_connected";
        return respond({
          jsonrpc: "2.0",
          id: body.id,
          result: {
            content: [
              {
                type: "text",
                text: JSON.stringify({
                  url: null,
                  slack_url: connected
                    ? null
                    : "https://connect.newstv.co.jp/slack/oauth/start/abc",
                  user_email_masked: "u***@example.com",
                  message: connected
                    ? OAUTH_ALREADY_CONNECTED_MESSAGE
                    : OAUTH_PARTIAL_MESSAGE,
                }),
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
      // どの規則で一致したか（抑止の可否を決める）。
      rule: classifyConnectRequest(entry.text),
      expectRule: entry.rule ?? null,
    });
  };
  for (const entry of CONNECT_REQUEST_PHRASES.must_match) await check(entry, true);
  for (const entry of CONNECT_REQUEST_PHRASES.must_not_match) await check(entry, false);
  // Slack が機械的に付ける送信通知つきの実形（2026-09-04 本番実測）。
  for (const entry of CONNECT_REQUEST_PHRASES.must_match_with_slack_boilerplate) {
    await check(entry, true);
  }
  for (const entry of CONNECT_REQUEST_PHRASES.must_not_match_with_slack_boilerplate) {
    await check(entry, false);
  }
  // 曖昧な形（先頭行だけが連携依頼・後続行は別の依頼）。トリガーは立てる。
  for (const entry of CONNECT_REQUEST_PHRASES.must_match_ambiguous_do_not_suppress) {
    await check(entry, true);
  }
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
  no_message: await fallthroughThenLayer2({ mode: "no_message" }),
  no_bearer: await fallthroughThenLayer2({ env: { TEAMAGENT_MCP_BEARER: "" } }),
  no_canonical_channel: await fallthroughThenLayer2({ chatId: USER }),
};
// ══ (D)(E) 保証マトリクス ═══════════════════════════════════════════════════
// ゴール（2026-09-04）: 新規／既存／過去のテストユーザーを問わず、「連携して」と
// 言ったら **漏れなく** 連携リンク（または次の一手が分かる診断つき案内）が届くこと。
//
// 保証経路は message_received に載っている。これは非 conversation hook で、
// 非 bundled plugin でも `hooks.allowConversationAccess` の可否に依存せず登録される
// （registry-D1_pYg_a.js:4224-4235 の門は CONVERSATION_HOOK_NAMES にしか掛からない）。
// 層1（before_agent_reply）は conversation hook なので、設定が 1 つ欠けるだけで
// **診断も出ないまま黙って捨てられる**＝保証の土台には使えない。
//
// ここで固定するのは 1 点だけ:
//   どの利用者状態 × どの言い回し でも、Slack への投稿が **必ず 1 通** 起きること。
// 無言になる組み合わせが 1 つでもあれば赤。
const SLACK_BOT_TOKEN = "xoxb-probe-token";

// Slack Web API の偽物。conversations.open と chat.postMessage だけを受ける。
// 本番の失敗モード（HTTP 200 + {"ok":false,"error":…}）を再現する。
function makeSlackFake({ mode = "ok" } = {}) {
  const posts = [];
  const opens = [];
  const respond = (payload) =>
    new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  const attempts = [];
  let postCalls = 0;
  const handle = async (url, init) => {
    const method = String(url).slice(`${SLACK_API_BASE_URL}/`.length);
    const body = JSON.parse(init.body);
    attempts.push(method);
    if (method === "conversations.open") {
      opens.push(body);
      if (mode === "open_fails") return respond({ ok: false, error: "user_not_found" });
      return respond({ ok: true, channel: { id: DM_CHANNEL } });
    }
    if (method === "chat.postMessage") {
      postCalls += 1;
      if (mode === "post_fails") return respond({ ok: false, error: "channel_not_found" });
      // 本番の一時失敗: 1 回目だけ 429（Retry-After 付き）で弾き、再試行で通す。
      if (mode === "rate_limited_once" && postCalls === 1) {
        return new Response(JSON.stringify({ ok: false, error: "ratelimited" }), {
          status: 429,
          headers: { "content-type": "application/json", "retry-after": "1" },
        });
      }
      // 本番の一時失敗: 1 回目だけ 5xx。
      if (mode === "http500_once" && postCalls === 1) {
        return new Response("boom", { status: 503 });
      }
      // スレッドが消えている等で thread_ts 付きだけが弾かれる面。
      if (mode === "thread_rejected" && body.thread_ts !== undefined) {
        return respond({ ok: false, error: "thread_not_found" });
      }
      posts.push({ ...body, authorization: init.headers.Authorization });
      return respond({ ok: true, ts: "1785206176.940190" });
    }
    return respond({ ok: false, error: "unknown_method" });
  };
  return { handle, posts, opens, attempts };
}

// mcp と Slack の両方を 1 つの fetchFn で受ける（plugin は fetchFn を 1 つしか持たない）。
function makeGuaranteePlugin({ mcpMode = "ok", slackMode = "ok", env = {} } = {}) {
  const logs = [];
  const infos = [];
  const handlers = new Map();
  const mcp = makeMcpFake({ mode: mcpMode });
  const slack = makeSlackFake({ mode: slackMode });
  const fetchFn = async (url, init) =>
    String(url).startsWith(SLACK_API_BASE_URL)
      ? slack.handle(url, init)
      : mcp.fetchFn(url, init);
  // 保証経路は hook の await から切り離されている（実装の startConnectGuarantee）。
  // 本番では「受信パイプラインを止めない」ためにそうしており、テストからは
  // この注入口で切り離した仕事を回収して待つ。
  const tasks = [];
  createCallerIdentityPlugin({
    env: {
      TEAMAGENT_CALLER_CLAIM_SECRET: SECRET,
      SLACK_TEAM_ID: TEAM,
      TEAMAGENT_MCP_BEARER: BEARER,
      TEAMAGENT_MCP_URL: "http://mcp.test/mcp",
      SLACK_BOT_TOKEN,
      ...env,
    },
    fetchFn,
    onBackgroundTask: (task) => tasks.push(task),
    // 再試行の待ちはテストでは 0（挙動だけを見る）。
    sleepFn: async () => {},
  }).register({
    registerInteractiveHandler() {},
    logger: { warn: (m) => logs.push(String(m)), info: (m) => infos.push(String(m)) },
    on: (name, fn) => handlers.set(name, fn),
  });
  const settle = async () => {
    // 背景タスクが別の背景タスクを生むことは無いが、増えなくなるまで待つ。
    while (tasks.length > 0) await Promise.all(tasks.splice(0));
  };
  return {
    handlers,
    logs,
    infos,
    settle,
    posts: slack.posts,
    opens: slack.opens,
    attempts: slack.attempts,
    mcpCalls: mcp.calls,
  };
}

// 受信フックへ 1 通流す。hook 名を選べるようにして、本番でどちらが発火しても
// 保証が立つこと（レビュー指摘 重大2）を測れるようにする。
function notifyInbound(handlers, content, { runId, messageId = TS, hook = "message_received" } = {}) {
  return handlers.get(hook)(
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

async function guaranteeScenario({
  content = "連携",
  mcpMode = "ok",
  slackMode = "ok",
  messageId = TS,
  // 本番では同じ受信が 2 度通知されうる（inbound_claim と message_received の両方、
  // run 束縛後の再通知）。マトリクスの全行でそれを再現し、
  // 「必ず 1 通・多くても 1 通」を同時に測る（2026-09-04 レビュー指摘 重大1）。
  notifyTwice = true,
} = {}) {
  const plugin = makeGuaranteePlugin({ mcpMode, slackMode });
  await notifyInbound(plugin.handlers, content, { messageId });
  await plugin.settle();
  if (notifyTwice) {
    await notifyInbound(plugin.handlers, content, { messageId });
    await plugin.settle();
  }
  const post = plugin.posts[0] ?? null;
  return {
    postCount: plugin.posts.length,
    postText: post?.text ?? null,
    postChannel: post?.channel ?? null,
    // 保証経路が使う claim は署名済み（mcp が before_tool_call 経由と同じ検証を通す）。
    claimChannel: (() => {
      const call = plugin.mcpCalls.find((c) => c.method === "tools/call");
      const claim = call?.body.params.arguments?._user_context?.caller_claim ?? null;
      return claim
        ? JSON.parse(Buffer.from(claim.split(".")[0], "base64url").toString()).channel
        : null;
    })(),
    toolName:
      plugin.mcpCalls.find((c) => c.method === "tools/call")?.body.params.name ?? null,
    logs: plugin.logs,
    infos: plugin.infos,
    plugin,
  };
}

// 利用者の状態差 × 言い回し の全組み合わせ。どれでも「必ず 1 通」届くこと。
const GUARANTEE_STATES = {
  // 新規（Slack プロフィールにメールが無い / 会社ドメイン外）→ mcp が I02 を返す。
  new_user_without_email: { mcpMode: "user_error" },
  // 新規（メールあり・未連携）→ 両方のリンク。
  new_user_with_email: { mcpMode: "ok" },
  // 既存（両方連携済み）／既存（片方のみ）→ mcp の message にその旨が入って返る。
  existing_fully_connected: { mcpMode: "already_connected" },
  existing_partially_connected: { mcpMode: "partially_connected" },
  // mcp 障害（到達不能・5xx・壊れた戻り値）→ 診断つき案内。
  mcp_unreachable: { mcpMode: "throw" },
  mcp_http_500: { mcpMode: "http500" },
  mcp_invalid_result: { mcpMode: "no_message" },
};

async function guaranteeMatrix() {
  const rows = [];
  // 素の表現と、Slack の送信通知が付いた実形の両方を全状態に掛ける（2026-09-04）。
  const variants = [
    ...CONNECT_REQUEST_PHRASES.must_match.map((e) => ({ ...e, variant: "plain" })),
    ...CONNECT_REQUEST_PHRASES.must_match_with_slack_boilerplate.map((e) => ({
      ...e,
      variant: "slack_boilerplate",
    })),
  ];
  for (const [state, options] of Object.entries(GUARANTEE_STATES)) {
    for (const entry of variants) {
      const r = await guaranteeScenario({ content: entry.text, ...options });
      rows.push({
        state,
        variant: entry.variant,
        text: entry.text,
        postCount: r.postCount,
        postText: r.postText,
        // 「必ず何かが届く」の判定: 空でない本文が 1 通だけ出ていること。
        delivered: r.postCount === 1 && typeof r.postText === "string" && r.postText.length > 0,
        // 到達できなかった場合は必ず転送用の診断行が付く（無言でも空文でもない）。
        hasDiagnostic: (r.postText ?? "").includes("診断: "),
      });
    }
  }
  return rows;
}

// ── 一回性が「pending から消えた瞬間」に失効しないこと（レビュー指摘 重大1）──────
// 旧実装は一回性の旗を ingress オブジェクトに載せていた。bindRun 成功時に
// removePending で pending から ingress が消え、次の通知は新しいオブジェクトを作るため、
// 旗が毎回リセットされて **同じ受信に何通も投稿し、oauth_connect も複数回呼ばれていた**
// （= state token が複数発行される）。台帳を pendingKey 基準にして断つ。
async function guaranteeOnceCases() {
  const toolCalls = (plugin) =>
    plugin.mcpCalls.filter((c) => c.method === "tools/call").length;

  // ① message_received ×2（event に runId 有り＝初回で run へ束縛され pending から消える）。
  const boundRunRenotify = await (async () => {
    const plugin = makeGuaranteePlugin({});
    await notifyInbound(plugin.handlers, "連携", { runId: "run-1" });
    await plugin.settle();
    await notifyInbound(plugin.handlers, "連携", { runId: "run-1" });
    await plugin.settle();
    return { postCount: plugin.posts.length, toolCallCount: toolCalls(plugin) };
  })();

  // ② inbound_claim → message_received ×2（両フックが同じ受信を運ぶ経路）。
  const bothHooksRenotify = await (async () => {
    const plugin = makeGuaranteePlugin({});
    await notifyInbound(plugin.handlers, "連携", { hook: "inbound_claim" });
    await plugin.settle();
    await notifyInbound(plugin.handlers, "連携");
    await plugin.settle();
    await notifyInbound(plugin.handlers, "連携");
    await plugin.settle();
    return { postCount: plugin.posts.length, toolCallCount: toolCalls(plugin) };
  })();

  // ③ message_received → before_model_resolve → message_received。
  //    最も現実的な経路。runId が無くても run 束縛で pending から消えるため、
  //    旧実装では再通知のたびに投稿していた。
  const afterRunBindingRenotify = await (async () => {
    const plugin = makeGuaranteePlugin({});
    await notifyInbound(plugin.handlers, "連携");
    await plugin.settle();
    startRun(plugin.handlers, "run-1");
    await notifyInbound(plugin.handlers, "連携");
    await plugin.settle();
    return { postCount: plugin.posts.length, toolCallCount: toolCalls(plugin) };
  })();

  // ④ 別の受信（messageId が違う）なら、それぞれ 1 通ずつ届く（抑制しすぎない）。
  const distinctInboundsBothAnswered = await (async () => {
    const plugin = makeGuaranteePlugin({});
    await notifyInbound(plugin.handlers, "連携", { messageId: "1785206401.000001" });
    await plugin.settle();
    await notifyInbound(plugin.handlers, "連携", { messageId: "1785206402.000002" });
    await plugin.settle();
    return { postCount: plugin.posts.length, toolCallCount: toolCalls(plugin) };
  })();

  return {
    bound_run_renotify: boundRunRenotify,
    both_hooks_renotify: bothHooksRenotify,
    after_run_binding_renotify: afterRunBindingRenotify,
    distinct_inbounds_both_answered: distinctInboundsBothAnswered,
  };
}

// ── どちらの受信フックが発火しても保証が立つこと（レビュー指摘 重大2）──────────
// 「mcp が署名 claim を受理している」という実績は message_received **または**
// inbound_claim のどちらかを示すだけで、message_received 単独の実証にはならない。
// 本番で inbound_claim だけが発火していた場合でも保証が成立することを固定する。
async function guaranteeHookSourceMatrix() {
  const run = async (hooks) => {
    const plugin = makeGuaranteePlugin({});
    for (const hook of hooks) {
      await notifyInbound(plugin.handlers, "連携", { hook });
      await plugin.settle();
    }
    return {
      hooks,
      postCount: plugin.posts.length,
      toolCallCount: plugin.mcpCalls.filter((c) => c.method === "tools/call").length,
      postText: plugin.posts[0]?.text ?? null,
    };
  };
  return {
    message_received_only: await run(["message_received"]),
    inbound_claim_only: await run(["inbound_claim"]),
    both: await run(["inbound_claim", "message_received"]),
  };
}

// 保証経路が「他の層の失敗から独立している」ことと、無言終了を作らないことの固定。
async function guaranteeCaseReport() {
  // ① run 束縛が無い（＝従来 9 件の `trusted Slack run identity is missing or stale`）
  //    状態でツールが block されても、保証経路の投稿は既に済んでいること。
  const runBindingLost = await (async () => {
    const plugin = makeGuaranteePlugin({});
    await notifyInbound(plugin.handlers, "連携");
    await plugin.settle();
    // before_model_resolve を通していない run のツール呼び出しは従来どおり block される。
    const blocked = callTool(plugin.handlers, "teamagent__oauth_connect", "run-unbound");
    return { postCount: plugin.posts.length, toolBlocked: blocked };
  })();

  // ② モデルが `_user_context.channel_id` に別の値を申告しても、
  //    もう block しない（authoritative 値で上書きして続行する）。
  const declaredChannelMismatch = await (async () => {
    const plugin = makeGuaranteePlugin({});
    const { handlers, logs } = plugin;
    await notifyInbound(handlers, "連携");
    await plugin.settle();
    startRun(handlers);
    const result = handlers.get("before_tool_call")(
      {
        toolName: "teamagent__oauth_connect",
        runId: "run-1",
        toolCallId: "tc-9",
        params: {
          _user_context: { slack_user_id: USER, channel_id: "C0000000OTHER" },
        },
      },
      {
        toolName: "teamagent__oauth_connect",
        runId: "run-1",
        toolCallId: "tc-9",
        sessionKey: DM_SESSION_KEY,
        channelId: `user:${USER}`,
      },
    );
    const context = result?.block ? null : result.params._user_context;
    return {
      blocked: Boolean(result?.block),
      // authoritative 値で上書きされていること（申告値は 1 文字も残らない）。
      // 値そのものは束縛された受信の会話 id（DM は内部別名）で、ここでの論点ではない。
      // 論点は「申告値が残らないこと」と「もう block しないこと」。
      signedChannel: context?.channel_id ?? null,
      declaredValueSurvived: context?.channel_id === "C0000000OTHER",
      discardedLogged: logs.some((m) => m.includes("discarded declared user_context fields")),
      postCount: plugin.posts.length,
    };
  })();

  // ③ 過去のテストユーザー: セッション履歴が汚染され、モデルが引数を二重に包む。
  //    unwrap で引数は救えるが、そもそも保証経路はモデル経路と独立に投稿を終えている。
  const pollutedHistory = await (async () => {
    const plugin = makeGuaranteePlugin({});
    const { handlers } = plugin;
    await notifyInbound(handlers, "連携");
    await plugin.settle();
    startRun(handlers);
    const result = handlers.get("before_tool_call")(
      {
        toolName: "teamagent__oauth_connect",
        runId: "run-1",
        toolCallId: "tc-8",
        params: { arguments: { _user_context: { slack_user_id: USER } } },
      },
      {
        toolName: "teamagent__oauth_connect",
        runId: "run-1",
        toolCallId: "tc-8",
        sessionKey: DM_SESSION_KEY,
        channelId: `user:${USER}`,
      },
    );
    return { blocked: Boolean(result?.block), postCount: plugin.posts.length };
  })();

  // ④/⑤ Slack 側の投稿が失敗したときは、無言で終わらせないだけでは足りない。
  //      **台帳を解放して層1 に救済させる**こと（2026-09-04 レビュー指摘）。
  //      層1 はハーネスの reply 経路で返す＝bot token も Slack Web API も使わない
  //      別の故障ドメインなので、ここで降りるのは救済機会の放棄になる。
  const slackFailureRescue = await (async () => {
    const out = {};
    for (const mode of ["post_fails", "open_fails"]) {
      // TRACE ON で測る。`already_attempted` は emitTrace 依存の行なので、
      // OFF のままだと「出ていない」が stand down していない証拠にならない。
      const plugin = makeGuaranteePlugin({
        slackMode: mode,
        env: { TEAMAGENT_CALLER_IDENTITY_TRACE: "1" },
      });
      await notifyInbound(plugin.handlers, "連携");
      await plugin.settle();
      const beforeLayer1 = {
        postCount: plugin.posts.length,
        postFailedLogged: plugin.logs.some((m) => m.includes("outcome=post_failed")),
      };
      // 保証経路が失敗した同じ受信に対して、層1 が答えられること。
      const l1 =
        (await plugin.handlers.get("before_agent_reply")(
          { cleanedBody: "連携" },
          beforeAgentReplyCtx(),
        )) ?? null;
      out[mode] = {
        ...beforeLayer1,
        layer1Handled: l1?.handled === true,
        layer1ReplyIsToolMessage: l1?.reply?.text === OAUTH_CONNECT_MESSAGE,
        // 層1 が `already_attempted` で降りていないこと。
        standDown: plugin.logs.some((m) => m.includes("reason=already_attempted")),
      };
    }
    return out;
  })();

  // ⑥ 一回性: 同じ受信が 2 度通知されても投稿は 1 通だけ。
  const oncePerInbound = await (async () => {
    const plugin = makeGuaranteePlugin({});
    await notifyInbound(plugin.handlers, "連携");
    await plugin.settle();
    await notifyInbound(plugin.handlers, "連携");
    await plugin.settle();
    return { postCount: plugin.posts.length };
  })();

  // ⑦ 保証経路が答えたら層1 は降りる（同じ受信に 2 回答えない）。
  const layer1StandsDown = await (async () => {
    // TRACE ON。降りたことを `already_attempted` の 1 行で積極的に確認する。
    const plugin = makeGuaranteePlugin({
      env: { TEAMAGENT_CALLER_IDENTITY_TRACE: "1" },
    });
    const { handlers, infos } = plugin;
    await notifyInbound(handlers, "連携");
    await plugin.settle();
    const l1 =
      (await handlers.get("before_agent_reply")(
        { cleanedBody: "連携" },
        beforeAgentReplyCtx(),
      )) ?? null;
    return {
      // 投稿成功時は従来どおり層1 が降り、投稿は 1 通のまま（過剰解放していない）。
      postCount: plugin.posts.length,
      layer1Handled: l1?.handled === true,
      guaranteeDelivered: infos.some((m) => m.includes("outcome=delivered")),
      standDown: plugin.logs.some((m) => m.includes("reason=already_attempted")),
      // 層1 が再度 oauth_connect を呼んでいないこと（state token の重複発行なし）。
      toolCallCount: plugin.mcpCalls.filter((c) => c.method === "tools/call").length,
    };
  })();

  // ⑧ チャンネルのスレッドで訊かれたらスレッドへ返す（会話面を移さない）。
  const channelThread = await (async () => {
    const plugin = makeGuaranteePlugin({});
    await plugin.handlers.get("message_received")(
      {
        from: `slack:channel:${CHANNEL}`,
        content: "連携",
        senderId: USER,
        messageId: "1785206299.000009",
        threadId: TS,
        metadata: {
          guildId: TEAM,
          to: `channel:${CHANNEL}`,
          originatingTo: `channel:${CHANNEL}`,
          threadId: TS,
        },
      },
      {
        channelId: "slack",
        conversationId: `channel:${CHANNEL}`,
        sessionKey: CHANNEL_SESSION_KEY,
        senderId: USER,
        messageId: "1785206299.000009",
      },
    );
    await plugin.settle();
    const post = plugin.posts[0] ?? null;
    return {
      postCount: plugin.posts.length,
      channel: post?.channel ?? null,
      threadTs: post?.thread_ts ?? null,
      // チャンネルは既に正準 id なので conversations.open は呼ばない。
      openCount: plugin.opens.length,
    };
  })();

  // ⑩ 一時失敗（429 / 5xx）は再試行で吸収する（保証の唯一の配信面なので無音にしない）。
  const transientRetries = await (async () => {
    const out = {};
    for (const mode of ["rate_limited_once", "http500_once"]) {
      const plugin = makeGuaranteePlugin({ slackMode: mode });
      await notifyInbound(plugin.handlers, "連携");
      await plugin.settle();
      out[mode] = {
        postCount: plugin.posts.length,
        postAttempts: plugin.attempts.filter((m) => m === "chat.postMessage").length,
        delivered: plugin.infos.some((m) => m.includes("outcome=delivered")),
      };
    }
    return out;
  })();

  // ⑪ thread_ts 付きが弾かれたら、スレッド無しで投げ直す（届かないより会話面のずれを取る）。
  const threadFallback = await (async () => {
    const plugin = makeGuaranteePlugin({ slackMode: "thread_rejected" });
    await plugin.handlers.get("message_received")(
      {
        from: `slack:channel:${CHANNEL}`,
        content: "連携",
        senderId: USER,
        messageId: "1785206500.000001",
        threadId: TS,
        metadata: {
          guildId: TEAM,
          to: `channel:${CHANNEL}`,
          originatingTo: `channel:${CHANNEL}`,
          threadId: TS,
        },
      },
      {
        channelId: "slack",
        conversationId: `channel:${CHANNEL}`,
        sessionKey: CHANNEL_SESSION_KEY,
        senderId: USER,
        messageId: "1785206500.000001",
      },
    );
    await plugin.settle();
    return {
      postCount: plugin.posts.length,
      threadTs: plugin.posts[0]?.thread_ts ?? null,
      channel: plugin.posts[0]?.channel ?? null,
    };
  })();

  // ⑨ DM は conversations.open で正準 `D…` を得てから claim を鋳造する
  //    （mcp の caller_claim は `^[CDG][A-Z0-9]{8,}$` を要求する）。
  const dmCanonicalChannel = await guaranteeScenario({ content: "連携" });

  return {
    run_binding_lost: runBindingLost,
    declared_channel_mismatch: declaredChannelMismatch,
    polluted_history_double_wrapped: pollutedHistory,
    slack_failure_rescue: slackFailureRescue,
    once_per_inbound: oncePerInbound,
    layer1_stands_down: layer1StandsDown,
    channel_thread: channelThread,
    transient_retries: transientRetries,
    thread_fallback: threadFallback,
    dm_canonical_channel: {
      postChannel: dmCanonicalChannel.postChannel,
      claimChannel: dmCanonicalChannel.claimChannel,
      toolName: dmCanonicalChannel.toolName,
      postCount: dmCanonicalChannel.postCount,
    },
  };
}

// ── (C1) run 束縛: 同じ会話の候補が複数でも落とさない ──────────────────────
// 本番実測 9 件の `trusted Slack run identity is missing or stale` の源。
// 従来は候補が 2 件以上あると run 全体を拒否し、以後 10 分間その run の
// すべてのツールが block されていた（連続送信・並行 run で普通に起きる）。
function bindNewestReport() {
  // ① 同じ DM で 2 通続けて送ってから run が始まる（＝候補 2 件）。
  const consecutive = (() => {
    const { handlers, logs } = makePlugin();
    // 曖昧化の 1 行は info で出る（makePlugin の logger は warn しか拾わないので console を見る）。
    const captured = captureConsole(() => {
      receiveDm(handlers, "今日の予定は？", { messageId: "1785206301.000001" });
      receiveDm(handlers, "やっぱり明日の予定を教えて", { messageId: "1785206302.000002" });
      startRun(handlers, "run-1");
      return callTool(handlers, "teamagent__search", "run-1");
    });
    const lines = captured.console.map((line) => line.text);
    return {
      toolBlocked: captured.value,
      rejected: logs.some((m) => m.includes("bind_agent_run rejected")),
      disambiguated: lines.some((line) => line.includes("bind_agent_run disambiguated")),
      disambiguatedLine:
        lines.find((line) => line.includes("bind_agent_run disambiguated")) ?? null,
    };
  })();

  // ② 安全側: 別の送信者 B の受信が pending にあっても、A の run は A の受信にしか
  //    束縛されない。matchesConversation が senderId で先に落とすため、B の受信は
  //    そもそも候補に入らない（＝最新を選ぶ規則は「誰か」を曖昧にしない）。
  const otherSender = (() => {
    const OTHER = "U0AAAAAAAAB";
    const { handlers } = makePlugin();
    const A_TS = "1785206303.000003";
    // ⚠️ senderId **だけ**が違う状況を作る（2026-09-04 レビュー指摘 中1）。
    // 旧版は B を receiveThreadMessage（CHANNEL_SESSION_KEY / channel:C…）で作る一方、
    // run ctx は DM（DM_SESSION_KEY / user:U…）だったため、sessionKey も channel も
    // 食い違っており **senderId 照合が無くても落ちた**＝この分岐を守っていなかった。
    // 実測: matchesConversation から senderId 照合を消す変異でこのテストは緑のままだった。
    // 同じチャンネル・同じ sessionKey・別 senderId・**B のほうが新しい** に揃えることで、
    // 「最新優先」が senderId を跨いだ瞬間に赤くなるようにする。
    receiveThreadMessage(handlers, "連携", { senderId: USER, messageId: A_TS });
    receiveThreadMessage(handlers, "連携", { senderId: OTHER, messageId: "1785206304.000004" });
    const rawId = `${CHANNEL.toLowerCase()}:thread:${TS}`;
    handlers.get("before_model_resolve")(
      { prompt: "probe" },
      {
        runId: "run-1",
        agentId: "teamagent",
        sessionKey: CHANNEL_SESSION_KEY,
        sessionId: "sid",
        trigger: "user",
        ...agentCtxFields(rawId),
        senderId: USER,
      },
    );
    const result = handlers.get("before_tool_call")(
      {
        toolName: "teamagent__oauth_connect",
        runId: "run-1",
        toolCallId: "tc-1",
        params: { _user_context: { slack_user_id: USER } },
      },
      {
        toolName: "teamagent__oauth_connect",
        runId: "run-1",
        toolCallId: "tc-1",
        sessionKey: CHANNEL_SESSION_KEY,
        channelId: rawId,
      },
    );
    const claim = result?.block
      ? null
      : JSON.parse(
          Buffer.from(
            result.params._user_context.caller_claim.split(".")[0],
            "base64url",
          ).toString(),
        );
    return {
      blocked: Boolean(result?.block),
      // 署名された送信者が A のままであること（B の受信を掴んでいない）。
      claimUser: claim?.sub ?? null,
      claimMessage: claim?.message ?? null,
      expectedMessage: A_TS,
    };
  })();

  // ③ 最新が選ばれること（束縛された受信の messageId が新しい方）。
  const newestWins = (() => {
    const { handlers } = makePlugin();
    receiveDm(handlers, "古い方", { messageId: "1785206305.000005" });
    receiveDm(handlers, "新しい方", { messageId: "1785206306.000006" });
    startRun(handlers, "run-1");
    const result = handlers.get("before_tool_call")(
      {
        toolName: "teamagent__search",
        runId: "run-1",
        toolCallId: "tc-1",
        params: { _user_context: { slack_user_id: USER } },
      },
      {
        toolName: "teamagent__search",
        runId: "run-1",
        toolCallId: "tc-1",
        sessionKey: DM_SESSION_KEY,
        channelId: `user:${USER}`,
      },
    );
    const claim = result?.block
      ? null
      : JSON.parse(
          Buffer.from(
            result.params._user_context.caller_claim.split(".")[0],
            "base64url",
          ).toString(),
        );
    return { blocked: Boolean(result?.block), claimMessage: claim?.message ?? null };
  })();

  return { consecutive, otherSender, newestWins };
}

// ── フックの観測性（本番でどのフックが呼ばれるかの一次証拠）─────────────────
// register の 1 行バナーと、各フック初回の `hook first_fired` を固定する。
// この 2 つの差分が「登録はしたが本番では呼ばれないフック」の一覧になる。
function hookObservabilityReport() {
  const captured = captureConsole(() => {
    const created = makePlugin();
    warmUpHooks(created.handlers);
    return created;
  });
  const lines = captured.console.map((line) => line.text);
  const firstFired = lines
    .map((line) => /hook first_fired name=(\S+)/u.exec(line)?.[1] ?? null)
    .filter((name) => name !== null);
  // 2 回目の呼び出しでは増えないこと（初回だけの行であること）。
  const before = consoleLines.length;
  warmUpHooks(captured.value.handlers);
  return {
    registeredHooks: [...REGISTERED_HOOKS],
    // 実際に api.on された名前（バナーがハードコード定数と非結合にならないことの固定）。
    handlerKeys: [...captured.value.handlers.keys()],
    bannerLine: lines.find((line) => line.includes("registered hooks=")) ?? null,
    firstFired,
    secondPassConsoleDelta: consoleLines.length - before,
    // 診断は stderr（console.warn）だけを使う。stdout は node ハーネスのデータ面で、
    // 混ぜると JSON.parse が壊れる（test_mcp_gateway_caller_claim.py の 3 本で実証）。
    consoleLevels: [...new Set(captured.console.map((line) => line.level))].sort(),
  };
}

// 受信に `content` が無い（上流の形が変わった疑い）ときは、TRACE と無関係に
// フックごとに 1 回だけ warn を残す。毎回出すと会話ごとの騒音になるので初回だけ。
async function contentAbsentReport() {
  const plugin = makeGuaranteePlugin({});
  const send = (hook) =>
    plugin.handlers.get(hook)(
      {
        from: `slack:${USER}`,
        senderId: USER,
        messageId: TS,
        metadata: { guildId: TEAM, to: `user:${USER}`, originatingTo: `user:${USER}` },
      },
      {
        channelId: "slack",
        conversationId: `user:${USER}`,
        sessionKey: DM_SESSION_KEY,
        senderId: USER,
        messageId: TS,
      },
    );
  await send("message_received");
  await send("message_received");
  await send("inbound_claim");
  await plugin.settle();
  const warned = plugin.logs.filter((m) => m.includes("reason=inbound_content_absent"));
  return {
    warnCount: warned.length,
    sources: warned
      .map((m) => /source=(\S+)/u.exec(m)?.[1] ?? null)
      .filter((v) => v !== null)
      .sort(),
    postCount: plugin.posts.length,
  };
}

// ── 二重返信の抑止（2026-09-04 本番実測 TD:45）───────────────────────────────
// 実測ログ: 保証経路が outcome=delivered で 1 通配信したあと、層2 の revise を経て
// モデル経路も同じ内容を 1 通返し、**利用者に同じ内容が 2 通**届いていた。
// 本番の並びをそのまま再現する:
//   message_received（保証経路が配信）→ before_model_resolve → before_agent_finalize
//   → reply_payload_sending（モデルの最終応答）
async function suppressionScenario({ slackMode = "ok", content = "連携" } = {}) {
  const plugin = makeGuaranteePlugin({ slackMode });
  const { handlers } = plugin;
  await notifyInbound(handlers, content);
  await plugin.settle();
  startRun(handlers);
  const finalize = finalizeRun(handlers, { lastAssistantMessage: SELF_MADE_REPLY });
  const delivery = deliverPayload(handlers, { text: SELF_MADE_REPLY });
  const replyCancelled = delivery?.cancel === true;
  return {
    // 保証経路が Slack へ投稿した通数。
    guaranteePosts: plugin.posts.length,
    // oauth_connect の呼び出し回数＝発行された state token の数。
    toolCallCount: plugin.mcpCalls.filter((c) => c.method === "tools/call").length,
    // 層2 が「oauth_connect を呼べ」と再要求したか（＝もう 1 個 token を出させるか）。
    layer2Revised: finalize?.action === "revise",
    // モデル側の最終応答が落とされたか。
    replyCancelled,
    cancelReason: delivery?.reason ?? null,
    // 利用者の目に触れる通数（保証経路の投稿 + 落とされなかったモデル応答）。
    userVisibleMessages: plugin.posts.length + (replyCancelled ? 0 : 1),
    suppressionLogged: plugin.infos.some((m) =>
      m.includes("suppressed model reply"),
    ),
    logs: plugin.logs,
    infos: plugin.infos,
  };
}

// 保証経路が **まだ配信中**（投稿の成否が確定していない）ときにモデルの最終応答が来る競合。
// ここで抑止してしまうと、その後に投稿が失敗した場合 **誰も答えない** 状態になる。
// 抑止の根拠を「配信成功」に限定していることの、唯一意味のある検証。
// （配信失敗のシナリオでは一回性の台帳も解放済みなので、両者の違いが出ない）
async function suppressionInFlightScenario() {
  const plugin = makeGuaranteePlugin({});
  const { handlers } = plugin;
  // settle しない＝保証経路は投稿の途中。一回性の旗だけが立っている状態。
  await notifyInbound(handlers, "連携");
  startRun(handlers);
  finalizeRun(handlers, { lastAssistantMessage: SELF_MADE_REPLY });
  const delivery = deliverPayload(handlers, { text: SELF_MADE_REPLY });
  const replyCancelled = delivery?.cancel === true;
  await plugin.settle();
  return {
    replyCancelled,
    // 配信途中で抑止しないので、利用者には最低 1 通は必ず届く。
    modelReplyDelivered: !replyCancelled,
    guaranteePosts: plugin.posts.length,
  };
}

// ── 規則の確度ごとの抑止可否（2026-09-04 レビュー指摘 重大1）─────────────────
// 曖昧な規則（leading_line）で一致した受信では、モデルの最終応答を**消さない**。
// 消すと「連携\n今日の予定を教えて」の **予定の回答が消える**（実測した回帰）。
async function suppressionByRuleReport() {
  const cases = {
    // (a) 全体一致。最も確実 → 抑止してよい。
    whole: "連携",
    // (b) 送信通知を除いた全体一致 → 抑止してよい。
    stripped: "連携\n_Slack を使用して送信されました_",
    // (c) 先頭行のみ一致・後続行は **別の依頼** → 抑止してはいけない。
    leading_line_other_request: "連携\n今日の予定を教えて",
    // (c) 先頭行のみ一致・後続行は未知の定型 → これも確度は低いので抑止しない。
    leading_line_unknown_boilerplate: "連携\n-- Acme Slack Bridge --",
  };
  const out = {};
  for (const [label, content] of Object.entries(cases)) {
    const r = await suppressionScenario({ content });
    out[label] = {
      rule: classifyConnectRequest(content),
      guaranteePosts: r.guaranteePosts,
      replyCancelled: r.replyCancelled,
      // 利用者が「自分の別の依頼への回答」を受け取れたか。
      modelAnswerDelivered: !r.replyCancelled,
      userVisibleMessages: r.userVisibleMessages,
    };
  }
  return out;
}

async function suppressionReport() {
  return {
    by_rule: await suppressionByRuleReport(),
    in_flight: await suppressionInFlightScenario(),
    // ① 保証経路が配信成功 → 利用者に届くのは 1 通・token 1 個。
    delivered: await suppressionScenario({}),
    // ② 保証経路が配信失敗 → 従来どおりモデル経路が返す（無言にしない）。
    post_failed: await suppressionScenario({ slackMode: "post_fails" }),
    // ③ 保証経路が未発火（連携依頼ではない）→ モデル経路は一切影響を受けない。
    not_connect_request: await suppressionScenario({ content: "今日の予定を教えて" }),
  };
}

// ── 受信本文の「形」だけを出す診断（2026-09-04 本番実測・レビュー指摘 1）─────
// 本番 OC TD:45 で「連携」が content_len=16 で届き not_connect_request で落ちたが、
// **16 文字の内訳がログから判らなかった**。本文は出せない（G7）ので、形だけを出す。
// ここでは (a) 指標そのものの正しさ (b) それが実ログ行に載ること (c) 本文が漏れないこと を固定する。
function connectShapeReport() {
  const units = [
    // 素の「連携」。全規則で通る。
    { label: "plain", text: "連携" },
    // 注記が別行・装飾つき。whole では落ち、除去後に通る。
    { label: "notice_line_decorated", text: "連携\n_Slack を使用して送信されました_" },
    // 注記が別行・装飾なし（本番の normalized_len==content_len と整合する形）。
    { label: "notice_line_plain", text: "連携\nSlack ワークフローを使用して送信されました" },
    // 長文。どの規則でも通らない。
    { label: "long_request", text: "〇〇社との連携について提案書を作ってください" },
    // 中身のある行が 2 本。混在は通さない。
    { label: "mixed_lines", text: "〇〇社との連携について提案書を\n連携" },
    // 語彙に無い未知の定型。通知除去では救えず、先頭行規則だけが救う。
    { label: "unknown_boilerplate", text: "連携\n-- Acme Slack Bridge --" },
    // 連携語を含まない通常の会話。
    { label: "unrelated", text: "今日の予定を教えて" },
  ].map((unit) => ({ ...unit, shape: connectRequestShape(unit.text) }));

  // 実ログ行に載ること（inbound recorded / not_connect_request の両方）。
  const logged = (() => {
    const { handlers } = makeConnectPlugin({
      env: { TEAMAGENT_CALLER_IDENTITY_TRACE: "1" },
    });
    const captured = captureConsole(() => {
      receiveDm(handlers, "〇〇社との連携について提案書を作ってください");
      return handlers.get("before_agent_reply")(
        { cleanedBody: "〇〇社との連携について提案書を作ってください" },
        beforeAgentReplyCtx(),
      );
    });
    const lines = captured.console.map((line) => line.text);
    return {
      inboundLine: lines.find((line) => line.includes("inbound recorded")) ?? null,
      notConnectLine:
        lines.find((line) => line.includes("reason=not_connect_request")) ?? null,
      allLines: lines,
    };
  })();

  return { units, logged };
}

// 「短い連携依頼ではない」文言では保証経路を起動しない（誤爆しない）。
async function guaranteeNegativeMatrix() {
  const rows = [];
  const negatives = [
    ...CONNECT_REQUEST_PHRASES.must_not_match.map((e) => ({ ...e, variant: "plain" })),
    ...CONNECT_REQUEST_PHRASES.must_not_match_with_slack_boilerplate.map((e) => ({
      ...e,
      variant: "slack_boilerplate",
    })),
  ];
  for (const entry of negatives) {
    const r = await guaranteeScenario({ content: entry.text });
    rows.push({ text: entry.text, variant: entry.variant, postCount: r.postCount });
  }
  return rows;
}

// (E) mcp の利用者向け失敗文面（CONNECT-I02）が、そのまま利用者へ届くこと。
const connectL1UserFacingError = await (async () => {
  const r = await deterministicScenario({ content: "連携", mode: "user_error" });
  return {
    handled: r.handled,
    replyText: r.replyText,
    toolCallCount: r.toolCallCount,
    mcpErrorText: MCP_USER_FACING_ERROR,
    infos: r.infos,
    logs: r.logs,
  };
})();
const connectPhraseRows = await connectPhraseMatrix();
const guaranteeMatrixRows = await guaranteeMatrix();
const guaranteeNegativeRows = await guaranteeNegativeMatrix();
const guaranteeCases = await guaranteeCaseReport();
const guaranteeOnce = await guaranteeOnceCases();
const guaranteeHookSources = await guaranteeHookSourceMatrix();
const contentAbsent = await contentAbsentReport();
const connectShape = connectShapeReport();
const suppression = await suppressionReport();
const hookObservability = hookObservabilityReport();
const bindNewest = bindNewestReport();
const connectL1NextMessage = await nextMessageAfterDeterministic();
const connectL1RepeatAfter30s = await repeatConnectAfterDeterministic();
const connectL1OtherSender = await otherSenderPendingInThread();

// ── ツール引数の二重包み（2026-09-03 実測）─────────────────────────────────
// EFS のセッション記録 166 ファイル・tool call 363 件のうち 83 件（23%）が
// before_tool_call で block されており、その 72 件が `_user_context must be a plain object`。
// 引数の実際の形は {"arguments":{"_user_context":{…}}} が 74 件、
// {"name":"teamagent__oauth_connect","arguments":{…}} が 2 件だった。
// ツールを問わず（oauth_connect 105 / search 95 / calendar_event 19 …）発生している。
function unwrapScenario({ params, toolName = "teamagent__search" }) {
  // register の 1 行バナー（`registered hooks=[…]`）とフック初回の
  // `hook first_fired` は、いずれもプロセス内 1 回だけの観測行。定常状態の騒音と
  // 混ぜないよう、セットアップ窓としてまとめて捕捉する。
  const setup = captureConsole(() => {
    const created = makePlugin();
    warmUpHooks(created.handlers);
    return created;
  });
  const { handlers, logs } = setup.value;
  const captured = captureConsole(() => {
    handlers.get("message_received")(
      {
        from: `slack:${USER}`,
        // 実際の受信は本文を持つ（連携依頼ではない通常の会話）。
        content: "テスト",
        senderId: USER,
        messageId: TS,
        metadata: { guildId: TEAM, to: `user:${USER}`, originatingTo: `user:${USER}` },
      },
      {
        channelId: "slack",
        conversationId: `user:${USER}`,
        sessionKey: DM_SESSION_KEY,
        senderId: USER,
        messageId: TS,
      },
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
    return handlers.get("before_tool_call")(
      { toolName, runId: "run-1", toolCallId: "tc-1", params },
      {
        toolName,
        runId: "run-1",
        toolCallId: "tc-1",
        sessionKey: DM_SESSION_KEY,
        channelId: `user:${USER}`,
      },
    );
  });
  const result = captured.value;
  const blocked = Boolean(result?.block);
  let claimPayload = null;
  let signedKeys = null;
  let signedContextKeys = null;
  let signedTop = null;
  let signedUserId = null;
  if (!blocked) {
    const context = result.params[USER_CONTEXT_KEY_NAME];
    signedUserId = context.slack_user_id;
    claimPayload = JSON.parse(
      Buffer.from(context.caller_claim.split(".")[0], "base64url").toString(),
    );
    signedKeys = Object.keys(result.params).toSorted();
    signedContextKeys = Object.keys(context).toSorted();
    signedTop = Object.fromEntries(
      Object.entries(result.params).filter(([key]) => key !== USER_CONTEXT_KEY_NAME),
    );
  }
  return {
    blocked,
    blockReason: result?.blockReason ?? null,
    diagCode: /診断: (CONNECT-P\d\d) /u.exec(result?.blockReason ?? "")?.[1] ?? null,
    diagLine:
      (result?.blockReason ?? "").split("\n").find((line) => line.startsWith("診断: ")) ?? null,
    signedKeys,
    signedContextKeys,
    signedTop,
    claimChannel: claimPayload?.channel ?? null,
    claimUser: claimPayload?.sub ?? null,
    signedUserId,
    claimPayload,
    warnings: logs,
    console: captured.console,
    warmUpConsole: setup.console,
  };
}

const USER_CONTEXT_KEY_NAME = "_user_context";
const AUTHENTIC_CONTEXT = { slack_user_id: USER };
// 利用者（モデル）由来の `_user_context` は authoritative 値で必ず上書きされる。
// 別人になりすました値を入れておき、署名結果が本物の送信者になることを固定する。
const SPOOFED_CONTEXT = { slack_user_id: "U00SPOOFED0" };

// unwrap そのものの単体確認（実装の export を直接叩く）。
function unwrapUnit() {
  const plain = { query: "q", _user_context: { slack_user_id: USER } };
  const plainResult = unwrapToolArguments(plain, "teamagent__search");
  const nested3 = { arguments: { arguments: { arguments: { _user_context: {} } } } };
  const nested3Result = unwrapToolArguments(nested3, "teamagent__search");
  const wrongName = {
    name: "teamagent__mail_summary",
    arguments: { _user_context: {} },
  };
  const wrongNameResult = unwrapToolArguments(wrongName, "teamagent__search");
  const bareName = { name: "search", arguments: { _user_context: {} } };
  const bareNameResult = unwrapToolArguments(bareName, "teamagent__search");
  const extraKey = {
    arguments: { _user_context: {} },
    query: "q",
  };
  const extraKeyResult = unwrapToolArguments(extraKey, "teamagent__search");
  const nonObject = { arguments: "not-an-object" };
  const nonObjectResult = unwrapToolArguments(nonObject, "teamagent__search");
  const twice = { arguments: { arguments: { _user_context: {}, query: "q" } } };
  const twiceResult = unwrapToolArguments(twice, "teamagent__search");
  return {
    // (d) 通常引数は無変更＝同一参照・バイト同一。
    plain_identical:
      plainResult.params === plain &&
      JSON.stringify(plainResult.params) === JSON.stringify(plain),
    plain_depth: plainResult.depth,
    plain_shape: plainResult.shape,
    // (c) 3 段は剥がさず元のまま（＝従来どおり block）。
    nested3_identical: nested3Result.params === nested3,
    nested3_depth: nested3Result.depth,
    // (c) 2 段は剥がす。
    twice_depth: twiceResult.depth,
    twice_shape: twiceResult.shape,
    twice_keys: Object.keys(twiceResult.params).toSorted(),
    // (b) name が呼び出し中のツールと違えば剥がさない（別ツールの引数を横流ししない）。
    wrong_name_identical: wrongNameResult.params === wrongName,
    // (b) `teamagent__` を外した素の名前も受ける。
    bare_name_depth: bareNameResult.depth,
    bare_name_shape: bareNameResult.shape,
    // (a) `arguments` 以外のキーが同居していたら包みではない。
    extra_key_identical: extraKeyResult.params === extraKey,
    // 値がオブジェクトでなければ包みではない。
    non_object_identical: nonObjectResult.params === nonObject,
  };
}

const unwrapReport = {
  // ①実測の主犯: {"arguments":{"_user_context":{…}}}（74 件）。block されず、
  //   mcp に届く引数は正規形（top に query、`_user_context` は authoritative 値）。
  single_arguments: unwrapScenario({
    params: { arguments: { query: "q", _user_context: AUTHENTIC_CONTEXT } },
  }),
  // ①' 信頼境界が動いていないことの固定: 包みの中で別人を騙っても、従来どおり拒否される。
  //   （unwrap は「どの階層を検査するか」を直すだけで、検査そのものは 1 つも緩めない）
  single_arguments_spoofed: unwrapScenario({
    params: { arguments: { query: "q", _user_context: SPOOFED_CONTEXT } },
  }),
  // ①'' 包まずに別人を騙った場合と同じ拒否になること（unwrap の有無で差が出ない）。
  plain_spoofed: unwrapScenario({
    params: { query: "q", _user_context: SPOOFED_CONTEXT },
  }),
  // ②{"name":…,"arguments":{…}}（2 件）。search でも効く＝全ツール共通であること。
  name_and_arguments: unwrapScenario({
    params: {
      name: "teamagent__search",
      arguments: { query: "q", _user_context: AUTHENTIC_CONTEXT },
    },
  }),
  // ②' oauth_connect でも同じく効く（実測 2 件はこのツール）。
  name_and_arguments_oauth: unwrapScenario({
    toolName: "teamagent__oauth_connect",
    params: {
      name: "teamagent__oauth_connect",
      arguments: { _user_context: AUTHENTIC_CONTEXT },
    },
  }),
  // ③2 段包みも剥がす。
  double_arguments: unwrapScenario({
    params: { arguments: { arguments: { query: "q", _user_context: AUTHENTIC_CONTEXT } } },
  }),
  // ④3 段包みは従来どおり block（診断 P06）。
  triple_arguments: unwrapScenario({
    params: { arguments: { arguments: { arguments: { _user_context: AUTHENTIC_CONTEXT } } } },
  }),
  // ⑤通常の引数はそのまま通る（回帰なし）。
  plain_arguments: unwrapScenario({
    params: { query: "q", _user_context: AUTHENTIC_CONTEXT },
  }),
  // ⑥unwrap 単体の性質。
  unit: unwrapUnit(),
};

// ── 拒否の観測性（2026-09-03）───────────────────────────────────────────
// 拒否したのにログが 1 行も出ない状態を作れないことを固定する。
function blockObservability({ ctxOverrides = {}, eventOverrides = {} } = {}) {
  const { handlers, logs } = makePlugin();
  warmUpHooks(handlers);
  const captured = captureConsole(() =>
    handlers.get("before_tool_call")(
      {
        toolName: "teamagent__search",
        runId: "run-1",
        toolCallId: "tc-1",
        params: { query: "q", _user_context: AUTHENTIC_CONTEXT },
        ...eventOverrides,
      },
      {
        toolName: "teamagent__search",
        runId: "run-1",
        toolCallId: "tc-1",
        sessionKey: DM_SESSION_KEY,
        channelId: `user:${USER}`,
        senderId: USER,
        ...ctxOverrides,
      },
    ),
  );
  const result = captured.value;
  return {
    blocked: Boolean(result?.block),
    blockReason: result?.blockReason ?? null,
    diagCode: /診断: (CONNECT-P\d\d) /u.exec(result?.blockReason ?? "")?.[1] ?? null,
    consoleWarnCount: captured.console.filter((line) => line.level === "warn").length,
    console: captured.console,
    warnings: logs,
  };
}

const blockDiagnostics = {
  // P01 native tool denied
  native_tool: blockObservability({
    eventOverrides: { toolName: "read" },
    ctxOverrides: { toolName: "read" },
  }),
  // P02 tool name binding（event と ctx が食い違う）
  tool_name_mismatch: blockObservability({
    ctxOverrides: { toolName: "teamagent__mail_summary" },
  }),
  // P03 run binding（event と ctx の runId 不一致）
  run_binding: blockObservability({ ctxOverrides: { runId: "run-2" } }),
  // P04 invocation binding（toolCallId 不一致）
  invocation_binding: blockObservability({ ctxOverrides: { toolCallId: "tc-2" } }),
  // P05 session-or-channel binding（channel を解決できない）
  session_binding: blockObservability({ ctxOverrides: { channelId: "slack" } }),
  // P03 実測 9 件: run は束縛されているが trusted ingress が無い。
  stale_run_identity: blockObservability({}),
};

// 正常系ではログが 1 行も増えないこと（拒否だけを観測する）。
const quietOnSuccess = (() => {
  const before = consoleLines.length;
  const r = unwrapScenario({ params: { query: "q", _user_context: AUTHENTIC_CONTEXT } });
  return {
    blocked: r.blocked,
    consoleLineCount: r.console.length,
    warningCount: r.warnings.length,
    // `hook first_fired` はプロセス内 1 回だけの観測行なので定常状態の騒音には数えない。
    // 実際に「1 回だけ」であることは hook_observability 側で別に固定する。
    totalConsoleDelta:
      consoleLines.length - before - r.warmUpConsole.length,
    warmUpConsole: r.warmUpConsole,
  };
})();

// ── 層1 の脱出経路の観測（2026-09-03 事故）─────────────────────────────────
// OC TD:43 着地直後、DM の「連携」で層1 が発火せずモデル経路になった
// （OC ログの `[agents/tool-policy] tool policy removed 26 tool(s)` ＝モデル起動）。
// plugin のログは CloudWatch に 1 行も無く、どの条件で落ちたか判別できなかった。
// 全脱出経路に理由を付け、trace ON/OFF の両モードを固定する。
async function layer1Trace({ trace, content = "連携", ctxOverrides = {}, skipInbound = false }) {
  const env = trace ? { TEAMAGENT_CALLER_IDENTITY_TRACE: "1" } : {};
  const { handlers, logs, infos } = makeConnectPlugin({ env });
  const captured = captureConsole(async () => {
    if (!skipInbound) receiveDm(handlers, content);
    return (
      (await handlers.get("before_agent_reply")(
        { prompt: content },
        { ...beforeAgentReplyCtx(), ...ctxOverrides },
      )) ?? null
    );
  });
  const result = await captured.value;
  const lines = captured.console.map((line) => line.text);
  // (D) 保証経路も `outcome=` を書くようになったので、層1 の行だけを見る。
  // 両者を混ぜると `outcome=skipped reason=no_slack_bot_token`（保証経路）を
  // 層1 の skip 理由と読み違える。
  const l1 = lines.filter((line) => line.includes("connect deterministic path"));
  return {
    handled: result?.handled === true,
    entered: lines.some((line) => line.includes("layer1 entered")),
    enteredLine: lines.find((line) => line.includes("layer1 entered")) ?? null,
    skippedReason:
      l1
        .find((line) => line.includes("outcome=skipped"))
        ?.match(/reason=(\S+)/u)?.[1] ?? null,
    skippedLine: l1.find((line) => line.includes("outcome=skipped")) ?? null,
    fallthroughReason:
      l1
        .find((line) => line.includes("outcome=fallthrough"))
        ?.match(/reason=(\S+)/u)?.[1] ?? null,
    inboundRecorded: lines.some((line) => line.includes("inbound recorded")),
    inboundLine: lines.find((line) => line.includes("inbound recorded")) ?? null,
    consoleLines: lines,
    warnings: logs,
    infos,
  };
}

const layer1TraceReport = {
  // trace ON: hook が呼ばれた事実が 1 行出て、handled まで進む。
  on_answered: await layer1Trace({ trace: true }),
  // trace OFF でも handled は変わらない（挙動は env に依存しない）。
  off_answered: await layer1Trace({ trace: false }),
  // 無言だった脱出経路 6 種（trace ON）。
  on_not_slack_provider: await layer1Trace({
    trace: true,
    ctxOverrides: { messageProvider: "discord" },
  }),
  on_trigger_not_user: await layer1Trace({
    trace: true,
    ctxOverrides: { trigger: "heartbeat" },
  }),
  on_missing_fields: await layer1Trace({
    trace: true,
    ctxOverrides: { sessionKey: "", senderId: "not-a-slack-id" },
  }),
  on_no_candidate_ingress: await layer1Trace({ trace: true, skipInbound: true }),
  on_not_connect_request: await layer1Trace({ trace: true, content: "こんにちは" }),
  on_already_attempted: await (async () => {
    // 1 通目で handled → 受信は消費される。同じ ctx で 2 度目を呼ぶ。
    const env = { TEAMAGENT_CALLER_IDENTITY_TRACE: "1" };
    const { handlers } = makeConnectPlugin({ env });
    receiveDm(handlers, "連携");
    // fetch を失敗させずに 1 度目を通し、消費前の状態で 2 度目を撃つため
    // mint 済みフラグだけが立つ経路（no_mcp_bearer）を使う。
    const { handlers: h2 } = makeConnectPlugin({
      env: { ...env, TEAMAGENT_MCP_BEARER: "" },
    });
    receiveDm(h2, "連携");
    await h2.get("before_agent_reply")({ prompt: "連携" }, beforeAgentReplyCtx());
    const captured = captureConsole(() =>
      h2.get("before_agent_reply")({ prompt: "連携" }, beforeAgentReplyCtx()),
    );
    await captured.value;
    const lines = captured.console.map((line) => line.text);
    return {
      skippedReason:
        lines
          .find((line) => line.includes("outcome=skipped"))
          ?.match(/reason=(\S+)/u)?.[1] ?? null,
    };
  })(),
  // trace OFF: 無言だった経路は既定でもやはり出さない（ノイズを増やさない）。
  off_not_connect_request: await layer1Trace({ trace: false, content: "こんにちは" }),
  off_no_candidate_ingress: await layer1Trace({ trace: false, skipInbound: true }),
  // trace OFF でも fallthrough（層1 に入ったが実行できなかった）は必ず出る。
  off_fallthrough: await (async () => {
    const { handlers } = makeConnectPlugin({ env: { TEAMAGENT_MCP_BEARER: "" } });
    warmUpHooks(handlers);
    const captured = captureConsole(() => {
      receiveDm(handlers, "連携");
      return handlers.get("before_agent_reply")({ prompt: "連携" }, beforeAgentReplyCtx());
    });
    await captured.value;
    const lines = captured.console.map((line) => line.text);
    // 層1 の行だけを数える。(D) 保証経路も同じ窓で 1 行書くので、混ぜると
    // 「層1 が fallthrough を 1 行だけ書く」という性質が測れなくなる。
    const l1 = lines.filter((line) => line.includes("connect deterministic path"));
    return {
      fallthroughReason:
        l1
          .find((line) => line.includes("outcome=fallthrough"))
          ?.match(/reason=(\S+)/u)?.[1] ?? null,
      lineCount: l1.length,
      lines,
    };
  })(),
};

// ── bind_agent_run / inbound rejected の G7（2026-09-03 レビュー指摘）─────────
// 旧実装は channelId の実値を両側とも出していた（`runChannelId=C0B0PQD83N2
// pendingChannelIds=[DM:U09CX1CCBLN]`）。DM では resolveSlackChannel が
// `DM:<senderId>` に解決するため、これは **Slack user id そのもの**が
// CloudWatch に落ちることを意味する。emitPluginLog が console へ二重書きする
// ようになった以上、ログレベルでの抑制も効かない。
// 同様に inbound 側の `foreignTeam=<T…> expected=<T…>` も実値だった。
function bindRunChannelMismatch() {
  const { handlers, logs } = makePlugin();
  const captured = captureConsole(() => {
    // DM の受信（pending は channelId=`DM:U09…`）。
    handlers.get("message_received")(
      {
        from: `slack:${USER}`,
        senderId: USER,
        messageId: TS,
        metadata: { guildId: TEAM, to: `user:${USER}`, originatingTo: `user:${USER}` },
      },
      {
        channelId: "slack",
        conversationId: `user:${USER}`,
        sessionKey: DM_SESSION_KEY,
        senderId: USER,
        messageId: TS,
      },
    );
    // 同じ sessionKey / senderId のまま、run ctx だけチャンネル会話を名乗る。
    // → candidates=0 かつ matchChannelId=0 で、実値を出していた分岐に入る。
    handlers.get("before_model_resolve")(
      { prompt: "probe" },
      {
        runId: "run-1",
        agentId: "teamagent",
        sessionKey: DM_SESSION_KEY,
        sessionId: "sid",
        trigger: "user",
        ...agentCtxFields(CHANNEL),
      },
    );
  });
  const line =
    captured.console.map((entry) => entry.text).find((text) => text.includes("bind_agent_run")) ??
    null;
  return { line, warnings: logs, console: captured.console.map((entry) => entry.text) };
}

function inboundForeignTeam() {
  const { handlers, logs } = makePlugin();
  const foreignTeam = "T99FOREIGN0";
  const captured = captureConsole(() => {
    handlers.get("message_received")(
      {
        from: `slack:${USER}`,
        senderId: USER,
        messageId: TS,
        metadata: {
          guildId: foreignTeam,
          to: `user:${USER}`,
          originatingTo: `user:${USER}`,
        },
      },
      {
        channelId: "slack",
        conversationId: `user:${USER}`,
        sessionKey: DM_SESSION_KEY,
        senderId: USER,
        messageId: TS,
      },
    );
  });
  const line =
    captured.console.map((entry) => entry.text).find((text) => text.includes("inbound rejected")) ??
    null;
  return { line, foreignTeam, warnings: logs };
}

// unwrap が throw しても block へ変換されること（fail-closed・2026-09-03 レビュー指摘 2）。
// JSON 由来の params では getter を持てないので本番では throw 不能だが、
// unwrap 呼び出しが try の外に出た瞬間にここが赤くなる。
function unwrapThrowsIsFailClosed() {
  const hostile = {};
  Object.defineProperty(hostile, "arguments", {
    enumerable: true,
    get() {
      throw new Error("hostile getter");
    },
  });
  let threw = null;
  let result = null;
  try {
    result = unwrapScenario({ params: hostile });
  } catch (error) {
    threw = String(error);
  }
  return {
    threw,
    blocked: result?.blocked ?? null,
    diagCode: result?.diagCode ?? null,
  };
}

const g7Report = {
  unwrap_throws_fail_closed: unwrapThrowsIsFailClosed(),
  bind_run_channel_mismatch: bindRunChannelMismatch(),
  inbound_foreign_team: inboundForeignTeam(),
};

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
  // 層1 ⑥「到達できなかった」失敗はすべて次の層へ落ちる（fail-open ではなく fail-to-next-layer）。
  connect_l1_failures: connectL1Failures,
  // 層1 ⑥' mcp が利用者向けに整形した失敗文面は、捨てずにそのまま届ける（(E)）。
  connect_l1_user_facing_error: connectL1UserFacingError,
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
  // ── (D)(E) 保証マトリクス（2026-09-04） ────────────────────────────────
  guarantee_matrix: guaranteeMatrixRows,
  guarantee_negative_matrix: guaranteeNegativeRows,
  guarantee_cases: guaranteeCases,
  // 一回性が pending の寿命に依存しないこと。
  guarantee_once: guaranteeOnce,
  // どちらの受信フックが発火しても保証が立つこと。
  guarantee_hook_sources: guaranteeHookSources,
  // 受信に content が無い場合の観測（フックごとに 1 回だけ）。
  guarantee_content_absent: contentAbsent,
  // 受信本文の「形」だけを出す診断（本文は 1 文字も出さない）。
  connect_shape: connectShape,
  // 二重返信の抑止（保証経路が配信成功したターンのモデル最終応答を落とす）。
  guarantee_suppression: suppression,
  // 各フックの入口 1 行と register バナー（本番でどのフックが呼ばれるかの一次証拠）。
  hook_observability: hookObservability,
  // (C1) 同じ会話の候補が複数でも run を落とさない。
  bind_newest: bindNewest,
  // ── ツール引数の二重包みを剥がす（2026-09-03） ────────────────────────
  unwrap: unwrapReport,
  // ── 拒否の観測性（診断行 + 必ず 1 行のログ） ──────────────────────────
  block_diagnostics: blockDiagnostics,
  block_quiet_on_success: quietOnSuccess,
  // ── 層1 の脱出経路の観測（trace ON/OFF の両モード） ────────────────────
  layer1_trace: layer1TraceReport,
  // ── bind_agent_run / inbound rejected の G7 ────────────────────────────
  g7: g7Report,
};

process.stdout.write(JSON.stringify(report, null, 2) + "\n");
