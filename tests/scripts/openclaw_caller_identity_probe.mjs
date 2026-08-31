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
};

process.stdout.write(JSON.stringify(report, null, 2) + "\n");
