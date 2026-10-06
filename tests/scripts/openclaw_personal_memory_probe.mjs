// 本人メモ（M8）の plugin 経路を、本物の caller-identity plugin（dist/index.js）と
// 本物の mcp（tests/test_openclaw_personal_memory.py が立てる streamable-http）で通すプローブ。
//
// hook の event/ctx の形は tests/scripts/openclaw_caller_identity_probe.mjs と同じ
// （上流 OpenClaw 2026.7.1 の実測値: DM のセッション鍵は kind=direct、before_agent_reply の
// ctx.chatId は Slack の D…、message_received の ctx に D… は無い）。
// Slack Web API だけ偽物（chat.postMessage を記録して ok を返す）。
const input = JSON.parse(process.env.PROBE_INPUT);
const { createCallerIdentityPlugin } = await import(input.pluginUrl);

const TS = "1784423990.000100";
let tsCounter = 0;
const nextTs = () => `17844239${String(90 + tsCounter++).padStart(2, "0")}.000100`;

function sessionKeyFor(user) {
  return `agent:teamagent:slack:direct:${user.toLowerCase()}`;
}

function makePlugin({ enabled = true, mcpUrl = input.mcpUrl } = {}) {
  const handlers = new Map();
  const logs = [];
  const slackPosts = [];
  const mcpCalls = [];
  const background = [];
  const realFetch = globalThis.fetch;
  const fetchFn = async (url, init) => {
    if (String(url).startsWith("https://slack.com/api/")) {
      const method = String(url).split("/").pop();
      slackPosts.push({ method, body: JSON.parse(init.body) });
      return new Response(JSON.stringify({ ok: true, ts: "1784424000.000200", channel: "D" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    const body = JSON.parse(init.body);
    if (body.method === "tools/call") mcpCalls.push({ name: body.params.name });
    return realFetch(url, init);
  };
  createCallerIdentityPlugin({
    env: {
      TEAMAGENT_CALLER_CLAIM_SECRET: input.secret,
      SLACK_TEAM_ID: input.teamId,
      TEAMAGENT_MCP_BEARER: input.bearer,
      TEAMAGENT_MCP_URL: mcpUrl,
      SLACK_BOT_TOKEN: input.botToken,
      ...(enabled ? { TEAMAGENT_PERSONAL_MEMORY: "1" } : {}),
    },
    now: () => input.nowMs,
    fetchFn,
    onBackgroundTask: task => background.push(task),
    sleepFn: async () => {},
  }).register({
    registerInteractiveHandler() {},
    logger: {
      warn: m => logs.push(String(m)),
      info: m => logs.push(String(m)),
    },
    on: (name, fn) => handlers.set(name, fn),
  });
  const settle = async () => {
    while (background.length) await background.shift();
  };
  return { handlers, logs, slackPosts, mcpCalls, settle };
}

// 本番の 1 ターン: message_received（runId つき＝その場で run に束縛）→ … → agent_end（台帳を掃除）。
// 掃除しないと前のターンの受信が残り、次のターンで「どの受信か」が曖昧になる（層1 と同じ規律）。
let runCounter = 0;
function endTurn(plugin, runId) {
  plugin.handlers.get("agent_end")({ runId }, { runId });
}

function receiveDm(plugin, user, content, { messageId = nextTs(), threadId, metadata = {}, runId } = {}) {
  plugin.handlers.get("message_received")(
    {
      from: `slack:${user}`,
      content,
      senderId: user,
      messageId,
      ...(runId ? { runId } : {}),
      ...(threadId ? { threadId } : {}),
      metadata: {
        guildId: input.teamId,
        to: `user:${user}`,
        originatingTo: `user:${user}`,
        ...(threadId ? { threadId } : {}),
        ...metadata,
      },
    },
    {
      channelId: "slack",
      conversationId: `user:${user}`,
      sessionKey: sessionKeyFor(user),
      senderId: user,
      messageId,
      ...(runId ? { runId } : {}),
    },
  );
  return messageId;
}

// 1 ターン分（受信 → hook → 終了）。ctx にも同じ runId を載せる。
async function turn(plugin, user, dm, text, fn) {
  const runId = `run-${++runCounter}`;
  receiveDm(plugin, user, text, { runId });
  const out = await fn({ ...dmCtx(user, dm), runId });
  endTurn(plugin, runId);
  return out;
}

function receiveChannel(plugin, user, content) {
  const messageId = nextTs();
  const sessionKey = `agent:teamagent:slack:channel:${input.channel.toLowerCase()}`;
  plugin.handlers.get("message_received")(
    {
      from: `slack:channel:${input.channel}`,
      content,
      senderId: user,
      messageId,
      metadata: {
        guildId: input.teamId,
        to: `channel:${input.channel}`,
        originatingTo: `channel:${input.channel}`,
      },
    },
    {
      channelId: "slack",
      conversationId: `channel:${input.channel}`,
      sessionKey,
      senderId: user,
      messageId,
    },
  );
  return sessionKey;
}

function dmCtx(user, dm) {
  return {
    agentId: "teamagent",
    sessionKey: sessionKeyFor(user),
    sessionId: "sid",
    workspaceDir: "/w",
    trigger: "user",
    channel: "slack",
    messageProvider: "slack",
    channelId: user,
    chatId: dm,
    senderId: user,
    channelContext: { sender: { id: user }, chat: { id: dm } },
  };
}

function channelCtx(user, sessionKey) {
  return {
    agentId: "teamagent",
    sessionKey,
    sessionId: "sid",
    workspaceDir: "/w",
    trigger: "user",
    channel: "slack",
    messageProvider: "slack",
    channelId: input.channel.toLowerCase(),
    chatId: input.channel,
    senderId: user,
  };
}

async function reply(plugin, ctx, text) {
  return (await plugin.handlers.get("before_agent_reply")({ cleanedBody: text }, ctx)) ?? null;
}

async function prompt(plugin, ctx) {
  const started = Date.now();
  const out = (await plugin.handlers.get("before_prompt_build")({ prompt: "x", messages: [] }, ctx)) ?? null;
  return { out, elapsedMs: Date.now() - started };
}

const report = {};

// 1. DM の普段の発話 → observe（返事は止めない・モデル経路のまま）
{
  const p = makePlugin();
  receiveDm(p, input.userA, "資料は短めが好き");
  const out = await reply(p, dmCtx(input.userA, input.dmA), "資料は短めが好き");
  await p.settle();
  report.observe = { out, mcpCalls: p.mcpCalls, logs: p.logs };
}

// 2. 添付つきの発話 → has_attachment=true で渡す（サーバの guard が落とす）
{
  const p = makePlugin();
  receiveDm(p, input.userA, "これ見て", { metadata: { mediaUrls: ["https://files.slack.com/x"] } });
  await reply(p, dmCtx(input.userA, input.dmA), "これ見て");
  await p.settle();
  report.observeMedia = { mcpCalls: p.mcpCalls };
}

// 3. 初回: context が告知を求める → DM へ告知を投稿し、告知済みを記録（返事への差し込みは無し）
{
  const p = makePlugin();
  receiveDm(p, input.userA, "おはよう");
  const first = await prompt(p, dmCtx(input.userA, input.dmA));
  await p.settle();
  report.notice = { first: first.out, slackPosts: p.slackPosts, mcpCalls: p.mcpCalls, logs: p.logs };
}

// 4. 告知済みの後: 覚えた内容を system 側にだけ差し込む。60 秒以内の 2 回目は mcp を呼ばない
{
  const p = makePlugin();
  receiveDm(p, input.userA, "今日の資料をお願い");
  const first = await prompt(p, dmCtx(input.userA, input.dmA));
  const second = await prompt(p, dmCtx(input.userA, input.dmA));
  report.inject = { first: first.out, second: second.out, mcpCalls: p.mcpCalls };
}

// 4b. 新しい会話の 1 通目（FM）と両立: bare reset 文なら本人メモ（system 側）と 1 通目の戻し
//     （appendContext）が 1 つの結果に両方入る。片方が他方を上書きしない
{
  const p = makePlugin();
  receiveDm(p, input.userA, "トレンダーズ");
  const out =
    (await p.handlers.get("before_prompt_build")(
      { prompt: input.bareResetPrompt, messages: [] },
      dmCtx(input.userA, input.dmA),
    )) ?? null;
  report.firstMessage = { out, mcpCalls: p.mcpCalls };
}

// 4c. 1 通目の戻しが想定外の形で投げても（ここでは prompt の読み出しで例外）、本人メモは届く
{
  const p = makePlugin();
  receiveDm(p, input.userA, "トレンダーズ");
  const throwingEvent = {
    messages: [],
    get prompt() {
      throw new Error("unexpected event shape");
    },
  };
  let out = null;
  let threw = false;
  try {
    out =
      (await p.handlers.get("before_prompt_build")(throwingEvent, dmCtx(input.userA, input.dmA))) ??
      null;
  } catch {
    threw = true;
  }
  report.firstMessageError = { out, threw };
}

// 5. コマンド（全文一致）はモデルを通さず本人メモの返事で答える。どのコマンドでもキャッシュは捨てる
{
  const p = makePlugin();
  const A = [input.userA, input.dmA];
  await turn(p, ...A, "今日の資料をお願い", ctx => prompt(p, ctx));
  const list = await turn(p, ...A, "何を覚えてる？", ctx => reply(p, ctx, "何を覚えてる？"));
  const forget = await turn(p, ...A, "3番を忘れて", ctx => reply(p, ctx, "3番を忘れて"));
  await turn(p, ...A, "今日の資料をお願い", ctx => prompt(p, ctx));
  const notCommand = await turn(p, ...A, "何を覚えてるか教えて", ctx =>
    reply(p, ctx, "何を覚えてるか教えて"),
  );
  await p.settle();
  report.commands = { list, forget, notCommand, mcpCalls: p.mcpCalls };
}

// 6. チャンネル・DM のスレッドは対象外（mcp を 1 回も呼ばない）
{
  const p = makePlugin();
  const sessionKey = receiveChannel(p, input.userA, "何を覚えてる？");
  const channelOut = await reply(p, channelCtx(input.userA, sessionKey), "何を覚えてる？");
  const channelPrompt = await prompt(p, channelCtx(input.userA, sessionKey));
  const threadPlugin = makePlugin();
  receiveDm(threadPlugin, input.userA, "何を覚えてる？", { threadId: "1784423000.000100" });
  const threadOut = await reply(threadPlugin, dmCtx(input.userA, input.dmA), "何を覚えてる？");
  await p.settle();
  await threadPlugin.settle();
  report.outOfScope = {
    channelOut,
    channelPrompt: channelPrompt.out,
    threadOut,
    mcpCalls: [...p.mcpCalls, ...threadPlugin.mcpCalls],
  };
}

// 7. 許可されていない人: mcp が拒否 → モデル経路へ（本人メモの返事は出さない）
{
  const p = makePlugin();
  receiveDm(p, input.userB, "何を覚えてる？");
  const out = await reply(p, dmCtx(input.userB, input.dmB), "何を覚えてる？");
  receiveDm(p, input.userB, "今日もよろしく");
  const promptOut = await prompt(p, dmCtx(input.userB, input.dmB));
  await p.settle();
  report.notAllowed = { out, prompt: promptOut.out, mcpCalls: p.mcpCalls, slackPosts: p.slackPosts };
}

// 8. flag OFF: 何もしない
{
  const p = makePlugin({ enabled: false });
  receiveDm(p, input.userA, "何を覚えてる？");
  const out = await reply(p, dmCtx(input.userA, input.dmA), "何を覚えてる？");
  const promptOut = await prompt(p, dmCtx(input.userA, input.dmA));
  await p.settle();
  report.disabled = { out, prompt: promptOut.out, mcpCalls: p.mcpCalls, logs: p.logs };
}

// 9. mcp が遅い: 1.2 秒で諦めて返事を止めない
{
  const p = makePlugin();
  receiveDm(p, input.userSlow, "急ぎでお願い");
  const slow = await prompt(p, dmCtx(input.userSlow, input.dmSlow));
  report.slow = { out: slow.out, elapsedMs: slow.elapsedMs, logs: p.logs };
}

// 10. mcp に届かない: コマンドは定型の案内（無言にしない）
{
  const p = makePlugin({ mcpUrl: input.closedMcpUrl });
  receiveDm(p, input.userA, "覚えるのを止めて");
  const out = await reply(p, dmCtx(input.userA, input.dmA), "覚えるのを止めて");
  report.unreachable = { out };
}

// 11. モデル経路から本人メモのツール・予約 ID は呼べない
{
  const p = makePlugin();
  const call = (toolName, toolCallId) =>
    p.handlers.get("before_tool_call")(
      { toolName, toolCallId, runId: "run-1", params: {} },
      { ...dmCtx(input.userA, input.dmA), toolName, toolCallId, runId: "run-1" },
    );
  report.llmPath = {
    byName: call("teamagent__personal_memory_context", "toolu_01"),
    byId: call("teamagent__search", `aico-pm-ctx-${"0".repeat(32)}`),
  };
}

// 12. 登録とバナー
{
  const p = makePlugin();
  report.banner = p.logs.find(line => line.includes("registered hooks=")) ?? null;
}

process.stdout.write(JSON.stringify(report));
