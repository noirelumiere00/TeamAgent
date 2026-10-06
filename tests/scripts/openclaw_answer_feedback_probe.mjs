// 回答評価ボタン（AF）の plugin 経路を、本物の caller-identity plugin（dist/index.js）と
// 本物の mcp（tests/test_openclaw_answer_feedback.py が立てる streamable-http）で通すプローブ。
//
// hook の event/ctx の形は tests/scripts/openclaw_caller_identity_probe.mjs と同じ
// （上流 OpenClaw 2026.7.1 の実測: 本番の順は before_tool_call → agent_end → reply_payload_sending）。
// 押下の ctx は tests/scripts/openclaw_button_direct_probe.mjs と同じ形（interactions.block-actions の実測）。
// Slack Web API だけ偽物（conversations.open / chat.postMessage / chat.update を記録して ok を返す）。
const input = JSON.parse(process.env.PROBE_INPUT);
const { createCallerIdentityPlugin } = await import(input.pluginUrl);

let tsCounter = 0;
const nextTs = () => `17844239${String(10 + tsCounter++).padStart(2, "0")}.000100`;
const dmFor = user => `D${user.slice(1)}`;

function makePlugin({ enabled = true, slackFail = {} } = {}) {
  const handlers = new Map();
  const interactive = new Map();
  const logs = [];
  const slack = [];
  const mcpCalls = [];
  const ephemeral = [];
  const background = [];
  const sleeps = [];
  const realFetch = globalThis.fetch;
  const fetchFn = async (url, init) => {
    if (String(url).startsWith("https://slack.com/api/")) {
      const method = String(url).split("/").pop();
      const body = JSON.parse(init.body);
      slack.push({ method, body });
      if (slackFail[method]) {
        return new Response(JSON.stringify({ ok: false, error: slackFail[method] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      const result =
        method === "conversations.open"
          ? { ok: true, channel: { id: dmFor(body.users) } }
          : { ok: true, ts: nextTs(), channel: body.channel };
      return new Response(JSON.stringify(result), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    const body = JSON.parse(init.body);
    if (body.method === "tools/call") mcpCalls.push({ name: body.params.name, arguments: body.params.arguments });
    return realFetch(url, init);
  };
  createCallerIdentityPlugin({
    env: {
      TEAMAGENT_CALLER_CLAIM_SECRET: input.secret,
      SLACK_TEAM_ID: input.teamId,
      TEAMAGENT_MCP_BEARER: input.bearer,
      TEAMAGENT_MCP_URL: input.mcpUrl,
      SLACK_BOT_TOKEN: input.botToken,
      ...(enabled ? { TEAMAGENT_ANSWER_FEEDBACK: "1" } : {}),
    },
    now: () => input.nowMs,
    fetchFn,
    onBackgroundTask: task => background.push(task),
    sleepFn: async ms => {
      sleeps.push(ms);
    },
  }).register({
    registerInteractiveHandler(registration) {
      interactive.set(registration.namespace, registration.handler);
    },
    logger: {
      warn: m => logs.push(String(m)),
      info: m => logs.push(String(m)),
    },
    on: (name, fn) => handlers.set(name, fn),
  });
  const settle = async () => {
    while (background.length) await background.shift();
  };
  return { handlers, interactive, logs, slack, mcpCalls, ephemeral, settle, sleeps };
}

let runCounter = 0;

// DM の 1 ターン（本番の順）: message_received → before_model_resolve → [before_tool_call] →
// agent_end → reply_payload_sending（final）。返り値は reply_payload_sending の戻り値。
async function dmTurn(plugin, user, { tools = [], text = "回答本文です", kind = "final" } = {}) {
  const runId = `run-${++runCounter}`;
  const messageId = nextTs();
  const sessionKey = `agent:teamagent:slack:direct:${user.toLowerCase()}`;
  plugin.handlers.get("message_received")(
    {
      from: `slack:${user}`,
      content: "JAL の過去提案ある？",
      senderId: user,
      messageId,
      metadata: { guildId: input.teamId, to: `user:${user}`, originatingTo: `user:${user}` },
    },
    { channelId: "slack", conversationId: `user:${user}`, sessionKey, senderId: user, messageId },
  );
  const agentCtx = {
    runId,
    agentId: "teamagent",
    sessionKey,
    sessionId: "sid",
    trigger: "user",
    channel: "slack",
    messageProvider: "slack",
    channelId: user,
    chatId: user,
    senderId: user,
  };
  plugin.handlers.get("before_model_resolve")({ prompt: "probe" }, agentCtx);
  const toolResults = tools.map(([toolName, params], index) =>
    plugin.handlers.get("before_tool_call")(
      { toolName, runId, toolCallId: `tc-${index}`, params: { ...params, _user_context: {} } },
      { toolName, runId, toolCallId: `tc-${index}`, sessionKey, channelId: `user:${user}` },
    ),
  );
  plugin.handlers.get("agent_end")({ runId, messages: [], success: true, durationMs: 1 }, agentCtx);
  const delivered = plugin.handlers.get("reply_payload_sending")(
    { payload: { text }, kind, channel: "slack", sessionKey, runId },
    { channelId: "slack", conversationId: `user:${user}`, sessionKey, runId },
  );
  // 分割 payload の 2 通目（同じ run）。評価は 1 回だけ。
  plugin.handlers.get("reply_payload_sending")(
    { payload: { text: "続き" }, kind, channel: "slack", sessionKey, runId },
    { channelId: "slack", conversationId: `user:${user}`, sessionKey, runId },
  );
  await plugin.settle();
  return { runId, messageId, delivered: delivered ?? null, toolResults };
}

// チャンネルの 1 ターン。threadTs が null なら本流の発言（返信は受信メッセージのスレッドに付く）。
async function channelTurn(plugin, user, { threadTs = null } = {}) {
  const runId = `run-${++runCounter}`;
  const messageId = nextTs();
  const thread = threadTs ?? messageId;
  const sessionKey = `agent:teamagent:slack:channel:${input.channel.toLowerCase()}:thread:${thread}`;
  plugin.handlers.get("message_received")(
    {
      from: `slack:channel:${input.channel}`,
      content: "JAL の過去提案ある？",
      senderId: user,
      messageId,
      ...(threadTs ? { threadId: threadTs } : {}),
      metadata: {
        guildId: input.teamId,
        to: `channel:${input.channel}`,
        originatingTo: `channel:${input.channel}`,
        ...(threadTs ? { threadId: threadTs } : {}),
      },
    },
    { channelId: "slack", conversationId: `channel:${input.channel}`, sessionKey, senderId: user, messageId },
  );
  const rawId = `${input.channel.toLowerCase()}:thread:${thread}`;
  const agentCtx = {
    runId,
    agentId: "teamagent",
    sessionKey,
    sessionId: "sid",
    trigger: "user",
    channel: "slack",
    messageProvider: "slack",
    channelId: rawId,
    chatId: input.channel,
    senderId: user,
  };
  plugin.handlers.get("before_model_resolve")({ prompt: "probe" }, agentCtx);
  const toolResult = plugin.handlers.get("before_tool_call")(
    { toolName: "teamagent__search", runId, toolCallId: "tc-0", params: { query: "JAL 過去提案", _user_context: {} } },
    { toolName: "teamagent__search", runId, toolCallId: "tc-0", sessionKey, channelId: rawId },
  );
  plugin.handlers.get("agent_end")({ runId, messages: [], success: true, durationMs: 1 }, agentCtx);
  plugin.handlers.get("reply_payload_sending")(
    { payload: { text: "回答" }, kind: "final", channel: "slack", sessionKey, runId },
    { channelId: "slack", conversationId: `channel:${input.channel}`, sessionKey, runId },
  );
  await plugin.settle();
  return { runId, messageId, toolBlocked: Boolean(toolResult?.block) };
}

function feedbackPosts(plugin) {
  return plugin.slack.filter(
    call => call.method === "chat.postMessage" && Array.isArray(call.body.blocks),
  );
}

function tokenOf(post) {
  return post.body.blocks[1].elements[0].value;
}

// 押下（interactions.block-actions の形）。presser は押した人、channel / messageTs は評価メッセージ。
async function press(plugin, { actionId, presser, channel, messageTs, value, threadTs = null }) {
  const triggerId = `trigger-${++runCounter}`;
  const handler = plugin.interactive.get(actionId);
  if (!handler) return { registered: false };
  const result = await handler({
    channel: "slack",
    senderId: presser,
    conversationId: channel,
    threadId: threadTs ?? undefined,
    interactionId: [presser, channel, messageTs, triggerId, actionId, value].join(":"),
    auth: { isAuthorizedSender: true },
    interaction: {
      kind: "button",
      actionId,
      namespace: actionId,
      blockId: "aico_answer_feedback",
      messageTs,
      threadTs: threadTs ?? undefined,
      triggerId,
      value,
      payload: value,
      data: `${actionId}:${value}`,
    },
    respond: {
      reply: async ({ text, responseType }) => {
        plugin.ephemeral.push({ text, responseType });
      },
    },
  });
  await plugin.settle();
  return { registered: true, result };
}

const report = {};
const A = input.userA;
const B = input.userB;

// 1. DM で search を使った返信 → 評価メッセージが 1 通（返信の後・DM・スレッド無し）
{
  const p = makePlugin();
  const turn = await dmTurn(p, A, { tools: [["teamagent__search", { query: "JAL 過去提案\n事例" }]] });
  const posts = feedbackPosts(p);
  report.dmSearch = { turn, posts, sleeps: p.sleeps.slice(), logs: p.logs.slice() };

  // 2. 質問した本人が 👍 → mcp が記録 → 評価メッセージを置き換える。続けて 👎（最後の値で上書き）
  const post = posts[0];
  const target = { channel: post.body.channel, messageTs: "1784424100.000100", value: tokenOf(post) };
  const up = await press(p, { ...target, actionId: "answer_feedback_up", presser: A });
  const down = await press(p, { ...target, actionId: "answer_feedback_down", presser: A });
  report.pressOwner = {
    up,
    down,
    mcpCalls: p.mcpCalls.slice(),
    updates: p.slack.filter(call => call.method === "chat.update"),
    ephemeral: p.ephemeral.slice(),
    logs: p.logs.slice(),
  };

  // 3. 別の人が押す → 本人にだけ「質問した方だけ」・mcp は呼ばない
  const before = p.mcpCalls.length;
  const other = await press(p, { ...target, actionId: "answer_feedback_up", presser: B });
  report.pressOther = { other, mcpCallsAdded: p.mcpCalls.length - before, ephemeral: p.ephemeral.slice(-1) };

  // 4. 改ざんしたトークン → mcp は呼ばない
  const [segment, sig] = target.value.split(".");
  const payload = JSON.parse(Buffer.from(segment, "base64url").toString("utf8"));
  payload.q = "改ざんした質問";
  const forged = `${Buffer.from(JSON.stringify(payload)).toString("base64url")}.${sig}`;
  const before2 = p.mcpCalls.length;
  await press(p, { ...target, actionId: "answer_feedback_up", presser: A, value: forged });
  report.pressForged = { mcpCallsAdded: p.mcpCalls.length - before2, ephemeral: p.ephemeral.slice(-1) };

  // 5. mcp の保存が失敗する人（resolver が fail 用 email に解決）→ 置き換えず、本人にだけ失敗の案内
  const failUser = input.userFail;
  const turnFail = await dmTurn(p, failUser, { tools: [["teamagent__search", { query: "失敗する人" }]] });
  const failPost = feedbackPosts(p).at(-1);
  const updatesBefore = p.slack.filter(call => call.method === "chat.update").length;
  await press(p, {
    channel: failPost.body.channel,
    messageTs: "1784424200.000100",
    value: tokenOf(failPost),
    actionId: "answer_feedback_down",
    presser: failUser,
  });
  report.storeFailure = {
    turnFail,
    updatesAdded: p.slack.filter(call => call.method === "chat.update").length - updatesBefore,
    ephemeral: p.ephemeral.slice(-1),
    logs: p.logs.filter(line => line.includes("answer feedback press")),
  };
}

// 6. search を使わない返信（別のツール・ツール無し）には付かない
{
  const p = makePlugin();
  await dmTurn(p, A, { tools: [["teamagent__knowledge_deliver", { query: "x" }]] });
  await dmTurn(p, A, { tools: [] });
  report.noSearch = { posts: feedbackPosts(p), mcpCalls: p.mcpCalls };
}

// 7. チャンネル: 本流の発言 → 受信メッセージのスレッドへ・スレッド内の発言 → そのスレッドへ
{
  const p = makePlugin();
  const top = await channelTurn(p, A);
  const inThread = await channelTurn(p, A, { threadTs: "1784423000.000100" });
  report.channel = { top, inThread, posts: feedbackPosts(p) };
}

// 8. flag OFF: ボタンも受け口も無い
{
  const p = makePlugin({ enabled: false });
  await dmTurn(p, A, { tools: [["teamagent__search", { query: "JAL" }]] });
  report.disabled = {
    posts: feedbackPosts(p),
    interactive: [...p.interactive.keys()],
    banner: p.logs.find(line => line.includes("registered hooks=")) ?? null,
  };
}

// 9. モデル経路から回答評価のツール・予約 ID は呼べない
{
  const p = makePlugin();
  const turn = await dmTurn(p, A, {
    tools: [
      ["teamagent__answer_feedback_record", { feedback_token: "x", rating: 1 }],
      ["teamagent__search", { query: "x" }],
    ],
  });
  const byId = p.handlers.get("before_tool_call")(
    { toolName: "teamagent__search", runId: "run-x", toolCallId: `aico-fb-${"0".repeat(32)}`, params: {} },
    { toolName: "teamagent__search", runId: "run-x", toolCallId: `aico-fb-${"0".repeat(32)}`, sessionKey: "s" },
  );
  report.llmPath = { byName: turn.toolResults[0], byId };
}

// 10. 評価メッセージの投稿に失敗しても返信には影響しない（ログだけ）
{
  const p = makePlugin({ slackFail: { "chat.postMessage": "channel_not_found" } });
  const turn = await dmTurn(p, A, { tools: [["teamagent__search", { query: "x" }]] });
  report.postFailure = { delivered: turn.delivered, logs: p.logs.filter(line => line.includes("answer feedback")) };
}

// 11. バナー
{
  const p = makePlugin();
  report.banner = p.logs.find(line => line.includes("registered hooks=")) ?? null;
  report.interactive = [...p.interactive.keys()];
}

process.stdout.write(JSON.stringify(report));
