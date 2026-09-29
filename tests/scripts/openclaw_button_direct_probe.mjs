// 朝ダイジェストのボタン押下を「AI を通さず直接処理する」経路（2026-09-29 裁定）の端から端まで。
// 呼び出し元は tests/test_openclaw_button_direct.py。
//
//   押下（上流 OpenClaw 2026.7.1 が interactive handler に渡す ctx の形そのまま）
//     → caller-identity plugin（本物の dist/index.js）が捕捉して {handled:true} を返す
//     → 切り離した処理が mcp へ直接 initialize → initialized → tools/call（署名 claim つき）
//        ここは **本物の HTTP**: Python 側が立てた本番と同じ streamable-http アプリ
//        （scripts/run_mcp_http_server.py の build_app・bearer 認証・SSE 応答）へ届き、
//        本物の dispatch_tool・caller claim 検証（HMAC・本人・one-use nonce）を通る
//     → 結果を Slack（ここだけ偽物）へ chat.postMessage
//
// 上流の再現点（openclaw/openclaw@v2026.7.1・file:line）:
//   - handler へ渡す ctx: extensions/slack/src/monitor/events/interactions.block-actions.ts:640-694
//     interactionId = [user, channel, messageTs, triggerId, actionId, value].join(":")（同 :394-414）
//     data = `${actionId}:${value}`・namespace = data の最初の ":" より前（src/plugins/interactive-shared.ts:22-47）
//   - Slack への ack は handler より前（interactions.block-actions.ts:905-906）
//   - handled = resolved?.handled ?? true。handled でなければ system event + heartbeat（同 :957-986）
//   - 同じ interactionId の再送は上流が dedupe する（src/plugins/interactive.ts の claim/commit）。
//     ここでは plugin 自身の守り（押下の指紋）を見るため、再押下は trigger を変えて送る。
//
// 偽物の Slack は本番の失敗の形を返す: HTTP 200 + {"ok":false,"error":"channel_not_found"}、HTTP 500、
// conversations.open の HTTP 200 + {"ok":false,"error":"internal_error"}、スレッドへの投稿の
// {"ok":false,"error":"cannot_reply_to_message"}、受け付けた後の時間切れ（AbortSignal.timeout が
// fetch を落とす形＝DOMException "TimeoutError"）。
// 上流の ctx.respond.reply（Slack の response_url への ephemeral）は例外を握らないので、失敗は throw で返す。
// 入力: env PROBE_INPUT（JSON）。出力: stdout に JSON 1 行。
import {Buffer} from "node:buffer";

const input = JSON.parse(process.env.PROBE_INPUT);
const mod = await import(input.pluginUrl);
const realFetch = globalThis.fetch;
const nowMs = input.nowMs;

let unhandledRejections = 0;
process.on("unhandledRejection", () => {
  unhandledRejections += 1;
});

const DM_OF = {
  [input.userA]: input.dmA,
  [input.userB]: input.dmB,
  [input.userC]: input.dmC,
};

let postSeq = 0;

function claimOf(args) {
  const claim = args?._user_context?.caller_claim;
  if (typeof claim !== "string") return null;
  return JSON.parse(Buffer.from(claim.split(".")[0], "base64url").toString("utf8"));
}

// plugin 1 個（= OpenClaw プロセス 1 個）。state は mcp の上下・Slack の失敗モード・時計・
// response_url（ctx.respond.reply）の成否を切り替える。
function makePlugin({mcpUrl, slackMode = "ok", buttonTimeoutMs, withBotToken = true}) {
  const state = {mcpDown: false, slackMode, nowMs, respondMode: "ok", respondDelayMs: 0};
  // 本人に見える順（DM への投稿と一時表示を、届いた順に 1 列で）。
  const timeline = [];
  const registrations = new Map();
  const tasks = [];
  const logs = [];
  const posts = [];
  const slackCalls = [];
  const mcpRequests = [];
  const toolCalls = [];
  const ephemerals = [];
  const fetchFn = async (url, init = {}) => {
    const href = String(url);
    if (href.startsWith("https://slack.com/api/")) {
      const method = href.slice("https://slack.com/api/".length);
      const body = JSON.parse(init.body);
      slackCalls.push(method);
      if (init.headers?.Authorization !== `Bearer ${input.botToken}`) {
        return new Response(JSON.stringify({ok: false, error: "invalid_auth"}), {status: 200});
      }
      if (method === "conversations.open") {
        if (state.slackMode === "open_api_error") {
          return new Response(JSON.stringify({ok: false, error: "internal_error"}), {status: 200});
        }
        if (state.slackMode === "open_http500") {
          return new Response("upstream error", {status: 500});
        }
        const id = DM_OF[body.users];
        return new Response(
          JSON.stringify(id ? {ok: true, channel: {id}} : {ok: false, error: "user_not_found"}),
          {status: 200, headers: {"content-type": "application/json"}},
        );
      }
      if (method === "chat.postMessage") {
        if (state.slackMode === "api_error") {
          return new Response(JSON.stringify({ok: false, error: "channel_not_found"}), {status: 200});
        }
        if (state.slackMode === "http500") {
          return new Response("upstream error", {status: 500});
        }
        if (state.slackMode === "thread_rejected" && body.thread_ts !== undefined) {
          return new Response(JSON.stringify({ok: false, error: "cannot_reply_to_message"}), {
            status: 200,
          });
        }
        postSeq += 1;
        const ts = `1784425000.${String(100000 + postSeq)}`;
        posts.push({...body, ts});
        timeline.push({kind: "post", text: body.text});
        if (state.slackMode === "post_timeout_once") {
          // Slack は受け付けた（投稿は届いた）が、こちらの待ち（AbortSignal.timeout）が先に切れた。
          state.slackMode = "ok";
          throw new DOMException("The operation was aborted due to timeout", "TimeoutError");
        }
        return new Response(JSON.stringify({ok: true, channel: body.channel, ts}), {
          status: 200,
          headers: {"content-type": "application/json"},
        });
      }
      return new Response(JSON.stringify({ok: false, error: "unknown_method"}), {status: 200});
    }
    if (href === mcpUrl) {
      const body = JSON.parse(init.body);
      mcpRequests.push(body.method);
      if (body.method === "tools/call") {
        toolCalls.push({
          name: body.params.name,
          argumentKeys: Object.keys(body.params.arguments).sort(),
          arguments: body.params.arguments,
          claim: claimOf(body.params.arguments),
        });
      }
      // mcp が落ちている（本物の接続拒否: 閉じたポートへ本物の fetch）。
      return realFetch(state.mcpDown ? input.closedMcpUrl : href, init);
    }
    // 想定外の宛先へは 1 バイトも出さない。
    throw new Error(`probe: unexpected egress ${href}`);
  };
  const plugin = mod.createCallerIdentityPlugin({
    env: {
      TEAMAGENT_CALLER_CLAIM_SECRET: input.secret,
      SLACK_TEAM_ID: input.teamId,
      TEAMAGENT_MCP_BEARER: input.bearer,
      TEAMAGENT_MCP_URL: mcpUrl,
      ...(withBotToken ? {SLACK_BOT_TOKEN: input.botToken} : {}),
    },
    now: () => state.nowMs,
    fetchFn,
    onBackgroundTask: task => tasks.push(task),
    sleepFn: async () => {},
    ...(buttonTimeoutMs ? {buttonTimeoutMs} : {}),
  });
  plugin.register({
    on: () => {},
    registerInteractiveHandler: registration => {
      registrations.set(`${registration.channel}:${registration.namespace}`, registration);
    },
    logger: {
      warn: message => logs.push(String(message)),
      info: message => logs.push(String(message)),
    },
  });
  let triggerSeq = 0;
  // 上流 handleSlackBlockAction → dispatchSlackPluginInteraction の該当経路。
  async function press({
    actionId,
    value,
    userId,
    channelId,
    messageTs,
    threadTs,
    authorized = true,
    blockId = "digestRow1",
    interactionValue,
  }) {
    triggerSeq += 1;
    const triggerId = `1784424000.${String(200000 + triggerSeq)}.probe`;
    const data = `${actionId}:${value}`;
    const separator = data.indexOf(":");
    const namespace = data.slice(0, separator);
    const payload = data.slice(separator + 1);
    const registration = registrations.get(`slack:${namespace}`);
    // interactionValue: 押下の id を別の値で組んだもの（部品の組み替え）。
    const interactionId = [
      userId,
      channelId,
      messageTs,
      triggerId,
      actionId,
      interactionValue ?? value,
    ].join(":");
    if (!registration) return {matched: false, handlerResult: null, enqueued: true};
    const handlerResult =
      (await registration.handler({
        accountId: "default",
        interactionId,
        conversationId: channelId,
        parentConversationId: undefined,
        threadId: threadTs,
        senderId: userId,
        senderUsername: undefined,
        auth: {isAuthorizedSender: authorized},
        channel: "slack",
        interaction: {
          kind: "button",
          actionId,
          blockId,
          messageTs,
          threadTs,
          value,
          selectedValues: undefined,
          selectedLabels: undefined,
          triggerId,
          responseUrl: "https://hooks.slack.com/actions/T0/1/probe",
          data,
          namespace,
          payload,
        },
        // 上流の respond（interactions.block-actions.ts:665-693）。reply は Bolt の respond
        // （response_url）をそのまま await し、失敗を握らない。
        respond: {
          acknowledge: async () => {},
          reply: async ({text, responseType}) => {
            if (!text) return;
            // response_url への POST の往復（本番は Slack までの HTTP。chat.postMessage と順序の保証は無い）。
            if (state.respondDelayMs > 0) {
              await new Promise(resolve => setTimeout(resolve, state.respondDelayMs));
            }
            if (state.respondMode === "fail") {
              throw new Error("Request failed with status code 404 (response_url expired)");
            }
            ephemerals.push({text, responseType: responseType ?? "ephemeral", userId, channelId});
            timeline.push({kind: "ephemeral", text});
          },
          followUp: async () => {},
          editMessage: async () => {},
        },
      })) ?? null;
    // dispatchPluginInteractiveHandler: handled = resolved?.handled ?? true。
    // handled でないときだけ上流は system event を積んで heartbeat を起こす。
    return {matched: true, handlerResult, enqueued: !(handlerResult?.handled ?? true)};
  }
  async function drain() {
    while (tasks.length > 0) {
      const batch = tasks.splice(0, tasks.length);
      await Promise.all(batch);
    }
  }
  function report() {
    return {
      ephemerals: ephemerals.map(item => ({...item})),
      timeline: timeline.map(item => ({...item})),
      posts: posts.map(post => ({...post})),
      slackCalls: [...slackCalls],
      mcpRequests: [...mcpRequests],
      toolCalls: toolCalls.map(call => ({
        name: call.name,
        argumentKeys: call.argumentKeys,
        tokenArgument:
          call.arguments.event_token ??
          call.arguments.schedule_token ??
          call.arguments.draft_token ??
          call.arguments.ack_token ??
          null,
        context: Object.fromEntries(
          Object.entries(call.arguments._user_context ?? {}).filter(([key]) => key !== "caller_claim"),
        ),
        claim: call.claim,
      })),
      logs: logs.filter(line => line.includes("button ")),
      banner: logs.find(line => line.includes("registered hooks=[")) ?? null,
    };
  }
  return {press, drain, report, state, posts};
}

const A = input.userA;
const B = input.userB;
const C = input.userC;
const T = input.tokens;
const out = {};
let tsSeq = 0;
const nextTs = () => `1784424000.${String(300000 + (tsSeq += 1))}`;

// ── 1. 📅 DM で押す → mcp 1 回 → 本人の DM に 1 通。再押下（trigger 違い）は何もしない ──────
const main = makePlugin({mcpUrl: input.mcpUrl});
{
  const messageTs = "1784424000.000101";
  const first = await main.press({
    actionId: "calendar_event",
    value: T.event,
    userId: A,
    channelId: input.dmA,
    messageTs,
  });
  await main.drain();
  const replay = await main.press({
    actionId: "calendar_event",
    value: T.event,
    userId: A,
    channelId: input.dmA,
    messageTs,
  });
  await main.drain();
  out.calendar = {first, replay, ...main.report()};
}

// ── 2. 🗓・✏️ を同じ行（同じ draft トークン）で押す → それぞれ自分のツールだけ ──────────────
{
  const p = makePlugin({mcpUrl: input.mcpUrl});
  const messageTs = nextTs();
  const schedule = await p.press({
    actionId: "schedule_propose",
    value: T.draft,
    userId: A,
    channelId: input.dmA,
    messageTs,
    blockId: "digestRow2",
  });
  const mailDraft = await p.press({
    actionId: "mail_draft",
    value: T.draft,
    userId: A,
    channelId: input.dmA,
    messageTs,
    blockId: "digestRow2",
  });
  await p.drain();
  out.sameRow = {schedule, mailDraft, ...p.report()};
}

// ── 3. ☑️ → 確認済み＋「↩︎ 取り消す」ボタン → そのボタンを押す → 取り消し ───────────────────
{
  const p = makePlugin({mcpUrl: input.mcpUrl});
  const ackMessageTs = nextTs();
  const ack = await p.press({
    actionId: "digest_ack",
    value: T.ack,
    userId: A,
    channelId: input.dmA,
    messageTs: ackMessageTs,
  });
  await p.drain();
  const ackPost = p.posts[0] ?? null;
  const undoButton = ackPost?.blocks?.[0]?.accessory ?? null;
  let undo = null;
  if (undoButton) {
    undo = await p.press({
      actionId: undoButton.action_id,
      value: undoButton.value,
      userId: A,
      channelId: input.dmA,
      messageTs: ackPost.ts,
      blockId: "undoBlock",
    });
    await p.drain();
  }
  // 取り消した後に、元の ☑️（同じ押下）をもう一度押す → 実行しない・本人にだけ一時表示で返す（無言にしない）。
  const reAck = await p.press({
    actionId: "digest_ack",
    value: T.ack,
    userId: A,
    channelId: input.dmA,
    messageTs: ackMessageTs,
  });
  await p.drain();
  out.ackThenUndo = {ack, undo, reAck, ...p.report()};
}

// ── 4. ☑️ を押したが mcp に digest_ack が無い（本番 OFF）→「このボタンはいま使えません」 ─────
{
  const p = makePlugin({mcpUrl: input.noAckMcpUrl});
  const pressed = await p.press({
    actionId: "digest_ack",
    value: T.ackAll,
    userId: A,
    channelId: input.dmA,
    messageTs: nextTs(),
  });
  await p.drain();
  out.ackDisabled = {pressed, ...p.report()};
}

// ── 5. 件名 60 字の 📅（ダイジェストの上限いっぱい）→ 完全なトークンが mcp の schema を通る ───
{
  const p = makePlugin({mcpUrl: input.mcpUrl});
  const pressed = await p.press({
    actionId: "calendar_event",
    value: T.eventLongTitle,
    userId: A,
    channelId: input.dmA,
    messageTs: nextTs(),
  });
  await p.drain();
  out.longTitle = {pressed, ...p.report()};
}

// ── 6. 二重押下（1 回目の処理中に 2 回目）→ mcp 1 回・投稿 1 通 ────────────────────────
{
  const p = makePlugin({mcpUrl: input.mcpUrl});
  const messageTs = nextTs();
  const first = await p.press({
    actionId: "calendar_event",
    value: T.eventDouble,
    userId: A,
    channelId: input.dmA,
    messageTs,
  });
  const second = await p.press({
    actionId: "calendar_event",
    value: T.eventDouble,
    userId: A,
    channelId: input.dmA,
    messageTs,
  });
  await p.drain();
  out.doublePress = {first, second, ...p.report()};
}

// ── 7. plugin の再起動（台帳が空）後に 1. と同じ押下 → mcp の one-use nonce で止まる ─────────
{
  const p = makePlugin({mcpUrl: input.mcpUrl});
  const pressed = await p.press({
    actionId: "calendar_event",
    value: T.event,
    userId: A,
    channelId: input.dmA,
    messageTs: "1784424000.000101",
  });
  await p.drain();
  out.restartReplay = {pressed, ...p.report()};
}

// ── 8. 他人の押下 ───────────────────────────────────────────────────────────────
{
  // 8a. B が自分の DM で A 宛てのトークンのボタンを押す → mcp が本人照合で無効（expired の文）
  const own = makePlugin({mcpUrl: input.mcpUrl});
  const pressed = await own.press({
    actionId: "calendar_event",
    value: T.eventForeign,
    userId: B,
    channelId: input.dmB,
    messageTs: nextTs(),
  });
  await own.drain();
  out.crossUserToken = {pressed, ...own.report()};
  // 8b. B の押下が A の DM から来た（本人の DM ではない）→ 実行しない・案内は B の DM へ
  const foreign = makePlugin({mcpUrl: input.mcpUrl});
  const foreignPress = await foreign.press({
    actionId: "calendar_event",
    value: T.eventForeign,
    userId: B,
    channelId: input.dmA,
    messageTs: nextTs(),
  });
  await foreign.drain();
  out.foreignDm = {pressed: foreignPress, ...foreign.report()};
  // 8c. チャンネルのスレッドで押す → 実行しない・案内は押した本人の DM へ（チャンネルには出さない）
  const channel = makePlugin({mcpUrl: input.mcpUrl});
  const channelPress = await channel.press({
    actionId: "calendar_event",
    value: T.event,
    userId: A,
    channelId: input.channel,
    messageTs: nextTs(),
    threadTs: "1784423000.000001",
  });
  await channel.drain();
  out.channelPress = {pressed: channelPress, ...channel.report()};
}

// ── 9. 形の違う値・組み替え・未認可 ───────────────────────────────────────────────
{
  const p = makePlugin({mcpUrl: input.mcpUrl});
  const cases = {
    draftOnCalendar: {actionId: "calendar_event", value: T.draft},
    eventOnSchedule: {actionId: "schedule_propose", value: T.event},
    tooLong: {actionId: "calendar_event", value: T.tooLong},
    mailDraftOver160: {actionId: "mail_draft", value: T.event},
    garbage: {actionId: "digest_ack", value: "not-a-token"},
  };
  out.shape = {};
  for (const [name, spec] of Object.entries(cases)) {
    out.shape[name] = await p.press({...spec, userId: A, channelId: input.dmA, messageTs: nextTs()});
  }
  await p.drain();
  out.shape.report = p.report();

  const q = makePlugin({mcpUrl: input.mcpUrl});
  out.silent = {
    // 押下の id が別の値で組まれている（部品の組み替え）→ 誰の押下か確かでない＝何もしない
    recombined: await q.press({
      actionId: "calendar_event",
      value: T.event,
      interactionValue: T.eventDouble,
      userId: A,
      channelId: input.dmA,
      messageTs: nextTs(),
    }),
    unauthorized: await q.press({
      actionId: "calendar_event",
      value: T.event,
      userId: A,
      channelId: input.dmA,
      messageTs: nextTs(),
      authorized: false,
    }),
  };
  await q.drain();
  out.silent.report = q.report();
}

// ── 10. mcp の門が拒否（本人を解決できない利用者）→ 定型文（内部語を出さない）─────────────
{
  const p = makePlugin({mcpUrl: input.mcpUrl});
  const pressed = await p.press({
    actionId: "calendar_event",
    value: T.eventUnknownUser,
    userId: C,
    channelId: input.dmC,
    messageTs: nextTs(),
  });
  await p.drain();
  out.unknownUser = {pressed, ...p.report()};
}

// ── 11. mcp が落ちている → 「もう一度押すか…」→ 復旧後にもう一度押す → 実行される ──────────
{
  const p = makePlugin({mcpUrl: input.mcpUrl});
  p.state.mcpDown = true;
  const messageTs = nextTs();
  const down = await p.press({
    actionId: "calendar_event",
    value: T.eventRetry,
    userId: A,
    channelId: input.dmA,
    messageTs,
  });
  await p.drain();
  const afterDown = p.report();
  p.state.mcpDown = false;
  const again = await p.press({
    actionId: "calendar_event",
    value: T.eventRetry,
    userId: A,
    channelId: input.dmA,
    messageTs,
  });
  await p.drain();
  out.mcpDown = {down, afterDown, again, ...p.report()};
}

// ── 12. ツールを渡した後に応答が途切れる（タイムアウト）→「確認できませんでした」・再押下は何もしない ─
{
  const p = makePlugin({mcpUrl: input.mcpUrl, buttonTimeoutMs: input.shortTimeoutMs});
  const messageTs = nextTs();
  const slow = await p.press({
    actionId: "mail_draft",
    value: T.draftSlow,
    userId: A,
    channelId: input.dmA,
    messageTs,
  });
  await p.drain();
  const again = await p.press({
    actionId: "mail_draft",
    value: T.draftSlow,
    userId: A,
    channelId: input.dmA,
    messageTs,
  });
  await p.drain();
  out.mcpTimeout = {slow, again, ...p.report()};
}

// ── 13. Slack への投稿が失敗する（ok:false・HTTP 500）→ 例外を上げず、実行は 1 回のまま ───────
{
  const apiError = makePlugin({mcpUrl: input.mcpUrl, slackMode: "api_error"});
  const apiErrorSpec = {
    actionId: "calendar_event",
    value: T.eventSlackApiError,
    userId: A,
    channelId: input.dmA,
    messageTs: nextTs(),
  };
  const pressed = await apiError.press(apiErrorSpec);
  await apiError.drain();
  // 結果が DM に残っていない押下の押し直し → 実行しない・「確かめてください」を一時表示。
  const repressed = await apiError.press(apiErrorSpec);
  await apiError.drain();
  out.slackApiError = {pressed, repressed, ...apiError.report()};
  const http500 = makePlugin({mcpUrl: input.mcpUrl, slackMode: "http500"});
  const pressed500 = await http500.press({
    actionId: "calendar_event",
    value: T.eventSlack500,
    userId: A,
    channelId: input.dmA,
    messageTs: nextTs(),
  });
  await http500.drain();
  out.slackHttp500 = {pressed: pressed500, ...http500.report()};
  // 13c. 投稿も一時表示も失敗（response_url の失効）→ 例外を上げず、ログに残す。
  const bothFail = makePlugin({mcpUrl: input.mcpUrl, slackMode: "api_error"});
  bothFail.state.respondMode = "fail";
  const pressedBoth = await bothFail.press({
    actionId: "calendar_event",
    value: T.eventBothFail,
    userId: A,
    channelId: input.dmA,
    messageTs: nextTs(),
  });
  await bothFail.drain();
  out.slackAndRespondFail = {pressed: pressedBoth, ...bothFail.report()};
}

// ── 14. 直接実行が無効な環境（bot token 無し）は従来の経路（handled:false）のまま ────────────
{
  const legacy = makePlugin({mcpUrl: input.mcpUrl, withBotToken: false});
  const pressed = await legacy.press({
    actionId: "calendar_event",
    value: T.event,
    userId: A,
    channelId: input.dmA,
    messageTs: nextTs(),
  });
  await legacy.drain();
  out.legacy = {pressed, ...legacy.report()};
  out.mainBanner = main.report().banner;
}

// ── 16. 10 分を超えてからの押し直し（2026-09-29 レビュー指摘）→ plugin の台帳（24h）で止まる ─────
// 以前は台帳が 10 分で消え、押し直しが mcp の one-use nonce に「再生」として拒否され、
// 失敗文（自由文での頼み直し＝二重登録の入口）が届いていた。
{
  const p = makePlugin({mcpUrl: input.mcpUrl});
  const messageTs = nextTs();
  const spec = {
    actionId: "calendar_event",
    value: T.eventLate,
    userId: A,
    channelId: input.dmA,
    messageTs,
  };
  const first = await p.press(spec);
  await p.drain();
  p.state.nowMs += 11 * 60 * 1000;
  const after11min = await p.press(spec);
  await p.drain();
  p.state.nowMs += 23 * 60 * 60 * 1000;
  const after23h = await p.press(spec);
  await p.drain();
  out.lateRepress = {first, after11min, after23h, ...p.report()};
}

// ── 17. 本人の DM を確かめられない（conversations.open の失敗）→ 無言にしない ─────────────────
{
  // 17a. HTTP 200 + ok:false internal_error（callSlackApi は再試行しない）→ 押下の会話で本人だけに
  //      「もう一度押して」を一時表示。台帳から外れているので、Slack が戻ってから押し直すと実行される。
  const p = makePlugin({mcpUrl: input.mcpUrl, slackMode: "open_api_error"});
  const messageTs = nextTs();
  const spec = {
    actionId: "calendar_event",
    value: T.eventOpenFail,
    userId: A,
    channelId: input.dmA,
    messageTs,
  };
  const failed = await p.press(spec);
  await p.drain();
  const afterFail = p.report();
  p.state.slackMode = "ok";
  const again = await p.press(spec);
  await p.drain();
  out.openApiError = {failed, afterFail, again, ...p.report()};
  // 17b. HTTP 500（2 回再送して諦める）＋ response_url も失効 → 例外を上げず、ログに残す。
  const q = makePlugin({mcpUrl: input.mcpUrl, slackMode: "open_http500"});
  q.state.respondMode = "fail";
  const pressed = await q.press({
    actionId: "calendar_event",
    value: T.eventOpenFail,
    userId: A,
    channelId: input.dmA,
    messageTs: nextTs(),
  });
  await q.drain();
  out.openHttp500 = {pressed, ...q.report()};
}

// ── 18. 結果の投稿を Slack が受け付けた後に時間切れ → 再送しない（2 通にしない） ───────────────
{
  const p = makePlugin({mcpUrl: input.mcpUrl, slackMode: "post_timeout_once"});
  const pressed = await p.press({
    actionId: "calendar_event",
    value: T.eventPostTimeout,
    userId: A,
    channelId: input.dmA,
    messageTs: nextTs(),
  });
  await p.drain();
  out.postTimeout = {pressed, ...p.report()};
}

// ── 19. DM のスレッドの中で押す → claim・_user_context・投稿のすべてが押下のスレッド ─────────────
{
  const thread = "1784423500.000001";
  const p = makePlugin({mcpUrl: input.mcpUrl});
  const pressed = await p.press({
    actionId: "calendar_event",
    value: T.eventThread,
    userId: A,
    channelId: input.dmA,
    messageTs: nextTs(),
    threadTs: thread,
  });
  await p.drain();
  out.threadPress = {thread, pressed, ...p.report()};
  // 19b. スレッドへの投稿が弾かれた（ok:false）→ スレッド無しで 1 回だけ投げ直す
  const r = makePlugin({mcpUrl: input.mcpUrl, slackMode: "thread_rejected"});
  const rejected = await r.press({
    actionId: "calendar_event",
    value: T.eventThreadRejected,
    userId: A,
    channelId: input.dmA,
    messageTs: nextTs(),
    threadTs: thread,
  });
  await r.drain();
  out.threadRejected = {thread, pressed: rejected, ...r.report()};
  // 19c. スレッドへの投稿を受け付けた後に時間切れ → スレッド無しでも投げ直さない
  const t = makePlugin({mcpUrl: input.mcpUrl, slackMode: "post_timeout_once"});
  const timedOut = await t.press({
    actionId: "calendar_event",
    value: T.eventThreadTimeout,
    userId: A,
    channelId: input.dmA,
    messageTs: nextTs(),
    threadTs: thread,
  });
  await t.drain();
  out.threadTimeout = {thread, pressed: timedOut, ...t.report()};
}

// ── 20. 形の合わない値の案内は、同じボタンにつき 1 回だけ（押した回数だけ届けない） ──────────────
{
  const p = makePlugin({mcpUrl: input.mcpUrl});
  const spec = {
    actionId: "calendar_event",
    value: T.tooLong,
    userId: A,
    channelId: input.dmA,
    messageTs: nextTs(),
  };
  const first = await p.press(spec);
  const second = await p.press(spec);
  await p.drain();
  out.staleTwice = {first, second, ...p.report()};
  // 20b. 案内が DM にも一時表示にも届かなかったら、押し直しでもう一度案内する（無言のまま固定しない）
  const q = makePlugin({mcpUrl: input.mcpUrl, slackMode: "api_error"});
  q.state.respondMode = "fail";
  const staleSpec = {
    actionId: "digest_ack",
    value: "not-a-token",
    userId: A,
    channelId: input.dmA,
    messageTs: nextTs(),
  };
  await q.press(staleSpec);
  await q.drain();
  q.state.slackMode = "ok";
  q.state.respondMode = "ok";
  await q.press(staleSpec);
  await q.drain();
  await q.press(staleSpec);
  await q.drain();
  out.staleNoticeRetry = q.report();
  // 20c. 案内の DM 投稿に失敗しても一時表示で届いたら、それで案内済み（押し直しは一時表示だけ）
  const r = makePlugin({mcpUrl: input.mcpUrl, slackMode: "api_error"});
  const staleSpec2 = {...staleSpec, messageTs: nextTs()};
  await r.press(staleSpec2);
  await r.drain();
  r.state.slackMode = "ok";
  await r.press(staleSpec2);
  await r.drain();
  out.staleNoticeFallback = r.report();
}

// ── 21. DM 以外で押された案内の投稿に失敗（2026-09-29 レビュー指摘）→ 24h 無言にしない ──────────
{
  // 21a. DM への案内が ok:false でも、押下の会話（チャンネル）で本人にだけ一時表示で届く。
  //      押し直しは一時表示で同じ案内だけ（DM へは繰り返さない・実行しない）。
  const p = makePlugin({mcpUrl: input.mcpUrl, slackMode: "api_error"});
  const spec = {
    actionId: "calendar_event",
    value: T.event,
    userId: A,
    channelId: input.channel,
    messageTs: nextTs(),
  };
  const first = await p.press(spec);
  await p.drain();
  p.state.slackMode = "ok";
  const again = await p.press(spec);
  await p.drain();
  out.notOwnDmFallback = {first, again, ...p.report()};
  // 21b. DM への案内も一時表示も失敗 → 印を外す → Slack が戻ってから押し直すと DM へ案内が届く。
  const q = makePlugin({mcpUrl: input.mcpUrl, slackMode: "api_error"});
  q.state.respondMode = "fail";
  const spec2 = {...spec, messageTs: nextTs()};
  const failed = await q.press(spec2);
  await q.drain();
  const afterFail = q.report();
  q.state.slackMode = "ok";
  q.state.respondMode = "ok";
  const retried = await q.press(spec2);
  await q.drain();
  const third = await q.press(spec2);
  await q.drain();
  out.notOwnDmRetry = {failed, afterFail, retried, third, ...q.report()};
}

// ── 22. 「作っています」の一時表示は結果より先に出る（response_url の往復が遅くても） ──────────────
{
  // mcp が落ちている＝結果（もう一度押して）がすぐ出る。response_url の往復は 300ms。
  const p = makePlugin({mcpUrl: input.mcpUrl});
  p.state.mcpDown = true;
  p.state.respondDelayMs = 300;
  const pressed = await p.press({
    actionId: "mail_draft",
    value: T.draft,
    userId: A,
    channelId: input.dmA,
    messageTs: nextTs(),
  });
  await p.drain();
  out.pendingOrder = {pressed, ...p.report()};
}

// ── 15. 文面の組み立て（renderButtonResult）の境界: 記法の無害化・リンクの門・取り消しボタンの門 ──
{
  const B_ = mod.ACTION_BINDINGS;
  const asResult = data => ({content: [{type: "text", text: JSON.stringify(data)}], isError: false});
  out.render = {
    // ツールの文に Slack 記法（<!here>・<@U…>）や & が混じっても、記法として効かせない。
    escaped: mod.renderButtonResult(B_.calendar_event, "calendar_event", asResult({
      message: "<!here> A&B <@U0123456789> 登録しました",
      event_url: "",
    })),
    // リンク欄の URL が記法を壊す・https でない・google.com でない → リンクにしない（文だけ）。
    pipeLink: mod.renderButtonResult(B_.calendar_event, "calendar_event", asResult({
      message: "登録しました",
      event_url: "https://www.google.com/calendar/event?eid=a|<!channel>",
    })),
    httpLink: mod.renderButtonResult(B_.schedule_propose, "schedule_propose", asResult({
      message: "作成しました",
      open_url: "http://mail.google.com/mail/u/0/#all/abc",
    })),
    foreignHost: mod.renderButtonResult(B_.mail_draft, "mail_draft", asResult({
      message: "作成しました",
      open_url: "https://mail.google.com.evil.example/#all/abc",
    })),
    ampLink: mod.renderButtonResult(B_.mail_draft, "mail_draft", asResult({
      message: "作成しました",
      open_url: "https://mail.google.com/mail/u/0/?a=1&b=2#all/abc",
    })),
    // WHATWG は `\` を `/` と読む（hostname=calendar.google.com）が、Slack 側は userinfo@evil と
    // 読みうる。検査した値と出す値がずれるものは通さない（2026-09-29 レビュー指摘）。
    backslashLink: mod.renderButtonResult(B_.calendar_event, "calendar_event", asResult({
      message: "登録しました",
      event_url: "https://calendar.google.com\\@evil.example/x",
    })),
    // クエリの `\` は WHATWG でも正規化されない（href と一致する）＝拒否文字の検査だけが止める。
    backslashQuery: mod.renderButtonResult(B_.calendar_event, "calendar_event", asResult({
      message: "登録しました",
      event_url: "https://calendar.google.com/x?a=\\@evil.example",
    })),
    userinfoLink: mod.renderButtonResult(B_.calendar_event, "calendar_event", asResult({
      message: "登録しました",
      event_url: "https://evil.example@calendar.google.com/x",
    })),
    nonCanonicalLink: mod.renderButtonResult(B_.calendar_event, "calendar_event", asResult({
      message: "登録しました",
      event_url: "https://Calendar.Google.com/x",
    })),
    // 📅 の message は URL を生で含む。リンクにできなかった URL は文からも消す（Slack の自動リンクを防ぐ）。
    rawUrlInMessage: mod.renderButtonResult(B_.calendar_event, "calendar_event", asResult({
      message: "📅 登録しました\n🔗 https://calendar.google.com\\@evil.example/x",
      event_url: "https://calendar.google.com\\@evil.example/x",
    })),
    // リンク欄が URL の形でない値なら、文は削らない。
    notAUrl: mod.renderButtonResult(B_.calendar_event, "calendar_event", asResult({
      message: "登録しました",
      event_url: "登",
    })),
    // 取り消しボタンは unack トークンのときだけ（ack トークンや形の違う値をボタンにしない）。
    undoWrongType: mod.renderButtonResult(B_.digest_ack, "digest_ack", asResult({
      message: "☑️ 確認済みにしました。",
      undo_token: T.ack,
    })),
    undoGarbage: mod.renderButtonResult(B_.digest_ack, "digest_ack", asResult({
      message: "☑️ 確認済みにしました。",
      undo_token: "not-a-token",
    })),
    // mcp の門の拒否（英語の理由・診断コード）→ 定型文。例外名もコードも出さない。
    gatewayError: mod.renderButtonResult(B_.schedule_propose, "schedule_propose", asResult({
      error: "Caller authorization failed. 診断: CONNECT-I01a 2026-09-29 12:00 JST",
      code: "CALLER_IDENTITY_REJECTED",
    })),
    // 本物の形の I01c（案内＋診断行）→ texts.unknown の後ろに定型の 2 行だけ添える。
    identityDiag: mod.renderButtonResult(B_.calendar_event, "calendar_event", asResult({
      error:
        "Caller authorization failed. 利用者側の操作では直りません。管理者へご連絡ください。\n" +
        "解決しない場合は、次の 1 行をそのまま管理者（小俣）へ送ってください:\n" +
        "診断: CONNECT-I01c 2026-09-29 12:00 JST U0CCCCCCCCC",
      code: "CALLER_IDENTITY_REJECTED",
    })),
    // 定型から外れた行（記法の混入・余計な語・別系統のコード）は 1 行も出さない。
    identityDiagMarkup: mod.renderButtonResult(B_.calendar_event, "calendar_event", asResult({
      error:
        "Caller authorization failed. x\n" +
        "解決しない場合は、次の 1 行をそのまま管理者（<!channel>）へ送ってください:\n" +
        "診断: CONNECT-I01a 2026-09-29 12:00 JST -",
      code: "CALLER_IDENTITY_REJECTED",
    })),
    identityDiagLink: mod.renderButtonResult(B_.calendar_event, "calendar_event", asResult({
      error:
        "Caller authorization failed. x\n" +
        "解決しない場合は、次の 1 行をそのまま管理者（小俣）へ送ってください:\n" +
        "診断: CONNECT-I01a 2026-09-29 12:00 JST - <https://evil.example|x>",
      code: "CALLER_IDENTITY_REJECTED",
    })),
    identityDiagOtherCode: mod.renderButtonResult(B_.calendar_event, "calendar_event", asResult({
      error:
        "Caller authorization failed. x\n" +
        "解決しない場合は、次の 1 行をそのまま管理者（小俣）へ送ってください:\n" +
        "診断: CONNECT-S01 2026-09-29 12:00 JST -",
      code: "CALLER_IDENTITY_REJECTED",
    })),
    // CALLER_IDENTITY_REJECTED 以外の門の拒否（ツールは走っていない）は従来どおり texts.failed。
    gatewayOther: mod.renderButtonResult(B_.schedule_propose, "schedule_propose", asResult({
      error: "Tool input is invalid.",
      code: "TOOL_INPUT_INVALID",
    })),
    exception: mod.renderButtonResult(B_.mail_draft, "mail_draft", asResult({
      error: "RuntimeError: boom", request_id: "req-123",
    })),
    // 別のツール名の unknown tool は「使えません」にしない（束縛先のツールのときだけ）。
    otherUnknownTool: mod.renderButtonResult(B_.calendar_event, "calendar_event", asResult({
      error: "unknown tool: digest_ack",
    })),
    isError: mod.renderButtonResult(B_.calendar_event, "calendar_event", {
      content: [{type: "text", text: "Input validation error: 'x' is too long"}],
      isError: true,
    }),
    brokenJson: mod.renderButtonResult(B_.digest_ack, "digest_ack", {
      content: [{type: "text", text: "{not json"}],
      isError: false,
    }),
  };
}

out.unhandledRejections = unhandledRejections;
process.stdout.write(JSON.stringify(out));
