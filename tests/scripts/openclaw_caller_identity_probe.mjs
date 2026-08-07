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

const CHANNEL_SESSION_KEY =
  `agent:teamagent:slack:channel:${CHANNEL.toLowerCase()}:thread:${TS}`;
const DM_SESSION_KEY = `agent:teamagent:slack:direct:${USER.toLowerCase()}`;

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
};

process.stdout.write(JSON.stringify(report, null, 2) + "\n");
