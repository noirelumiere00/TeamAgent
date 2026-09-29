// 朝ダイジェストのボタン（mail_draft / calendar_event / schedule_propose / digest_ack）を、
// 上流 OpenClaw 2026.7.1 が実際に渡す形のまま caller-identity plugin へ通すプローブ。
// 呼び出し元は tests/test_openclaw_action_bindings.py（本物の HMAC トークンを env で渡し、
// 署名済み引数を本物の mcp dispatch_tool へ流す）。
//
// 上流の再現点（openclaw/openclaw@v2026.7.1 の実物・file:line）:
//   - interactive handler の ctx と data/namespace/payload:
//       extensions/slack/src/monitor/events/interactions.block-actions.ts:351-371（data = `${actionId}:${value}`）
//       同 :394-414（interactionId = [user, channel, messageTs, triggerId, actionId, value].join(":")）
//       同 :614-697（handler へ渡す ctx）・extensions/slack/src/interactive-dispatch.ts:108-139
//       src/plugins/interactive-shared.ts:22-47（namespace = data の最初の ":" より前）
//   - handler が handled:false（または namespace 未登録）なら system event + heartbeat:
//       interactions.block-actions.ts:957-986, 765-818
//   - system event の文字列は 160 字で切り詰め（159 字＋"…"）・triggerId/responseUrl は伏字:
//       extensions/slack/src/monitor/events/interactions.ts:12-24,26-64,133-159 / extensions/slack/src/truncate.ts
//     ⇒ 📅（event トークン 197 字〜）・一括確認（ackall）の value は system event では必ず切れる。
//   - heartbeat run の hook ctx: DM の session 鍵（…:direct:<user>）は会話 id を持たないので
//     channelId/chatId は messageTo（user:U…）由来の `U…` になる:
//       src/plugins/hook-agent-context.ts:67-120 / src/sessions/session-key-utils.ts:408-447
//     （計画書 09-08 の実 dist 実行でも「DM では heartbeat run の channelId が U…」を確認済み）
//
// 入力: env PROBE_INPUT（JSON）。出力: stdout に JSON 1 行（診断は stderr）。
import {Buffer} from "node:buffer";

const input = JSON.parse(process.env.PROBE_INPUT);
const mod = await import(input.pluginUrl);
const hooks = {};
const registrations = new Map();
const nowMs = input.nowMs;
mod
  .createCallerIdentityPlugin({
    env: {
      TEAMAGENT_CALLER_CLAIM_SECRET: input.secret,
      SLACK_TEAM_ID: input.teamId,
    },
    now: () => nowMs,
    randomBytesFn: () => Buffer.alloc(16, 7),
  })
  .register({
    on: (name, callback) => {
      hooks[name] = callback;
    },
    registerInteractiveHandler: registration => {
      registrations.set(`${registration.channel}:${registration.namespace}`, registration);
    },
    logger: {warn: () => {}, info: () => {}},
  });

// ── 上流の system event 整形（interactions.ts の移植・ASCII トークン前提）──────────
const SYSTEM_EVENT_PREFIX = "Slack interaction: ";
const SYSTEM_EVENT_MAX_CHARS = 2400;
const SYSTEM_EVENT_STRING_MAX_CHARS = 160;
const REDACTED_KEYS = new Set([
  "triggerId",
  "responseUrl",
  "workflowTriggerUrl",
  "privateMetadata",
  "viewHash",
]);

function truncateSlackText(value, max) {
  const trimmed = value.trim();
  if (trimmed.length <= max) return trimmed;
  // 上流は sliceUtf16Safe。トークンは ASCII なので slice と同じ。
  return `${trimmed.slice(0, max - 1)}…`;
}

function sanitize(value, key) {
  if (value === undefined) return undefined;
  if (key && REDACTED_KEYS.has(key)) {
    if (typeof value !== "string" || value.trim().length === 0) return undefined;
    return "[redacted]";
  }
  if (typeof value === "string") return truncateSlackText(value, SYSTEM_EVENT_STRING_MAX_CHARS);
  if (!value || typeof value !== "object") return value;
  const output = {};
  for (const [entryKey, entryValue] of Object.entries(value)) {
    const sanitized = sanitize(entryValue, entryKey);
    if (sanitized === undefined) continue;
    if (typeof sanitized === "string" && sanitized.length === 0) continue;
    output[entryKey] = sanitized;
  }
  return output;
}

function formatSystemEvent(payload) {
  const text = `${SYSTEM_EVENT_PREFIX}${JSON.stringify(sanitize(payload))}`;
  if (text.length > SYSTEM_EVENT_MAX_CHARS) {
    // 上流はここで compact 版へ落とす。このプローブの入力はそこまで長くならない。
    throw new Error("probe input exceeds the modelled system event size");
  }
  return text;
}

function systemEventValue(systemEvent) {
  return JSON.parse(systemEvent.slice(SYSTEM_EVENT_PREFIX.length)).value;
}

// ── ボタン押下（上流 handleSlackBlockAction の該当経路）──────────────────────────
let triggerSeq = 0;
async function press({
  actionId,
  value,
  userId,
  channelId,
  messageTs,
  threadTs,
  authorized = true,
  blockId = "digestRow1",
}) {
  triggerSeq += 1;
  const triggerId = `1784424000.${String(100000 + triggerSeq)}.probe`;
  const responseUrl = "https://hooks.slack.com/actions/T0/1/probe";
  const summary = {actionType: "button", inputKind: "text", value, inputValue: value};
  const data = `${actionId}:${value}`;
  const separator = data.indexOf(":");
  const namespace = data.slice(0, separator);
  const payload = data.slice(separator + 1);
  const registration = registrations.get(`slack:${namespace}`);
  const interactionId = [userId, channelId, messageTs, triggerId, actionId, value].join(":");
  let handlerResult = null;
  let handled = false;
  if (registration) {
    handlerResult =
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
          responseUrl,
          data,
          namespace,
          payload,
        },
        respond: {
          acknowledge: async () => {},
          reply: async () => {},
          followUp: async () => {},
          editMessage: async () => {},
        },
      })) ?? null;
    // dispatchPluginInteractiveHandler: handled = resolved?.handled ?? true
    handled = handlerResult?.handled ?? true;
  }
  // 上流が handled でないときに積む system event（テストでは handled でも組んでおき、
  // 「捕捉されていない押下の system event では束縛されない」ことの検査にも使う）。
  const systemEvent = formatSystemEvent({
    interactionType: "block_action",
    actionId,
    blockId,
    ...summary,
    userId,
    teamId: input.teamId,
    triggerId,
    responseUrl,
    channelId,
    messageTs,
    threadTs,
  });
  return {
    matched: Boolean(registration),
    handlerResult,
    enqueued: !handled,
    systemEvent,
    systemEventValue: systemEventValue(systemEvent),
  };
}

function dmSession(userId) {
  return `agent:main:slack:direct:${userId.toLowerCase()}`;
}

function channelSession(channelId, threadTs) {
  return `agent:main:slack:channel:${channelId.toLowerCase()}${threadTs ? `:thread:${threadTs}` : ""}`;
}

function channelHookId(channelId, threadTs) {
  // parseRawSessionConversationRef の rawId（小文字・:thread: 付き）。
  return `${channelId.toLowerCase()}${threadTs ? `:thread:${threadTs}` : ""}`;
}

function heartbeat({runId, systemEvent, sessionKey, hookChannelId}) {
  return hooks.before_model_resolve(
    {
      prompt:
        "Read HEARTBEAT.md if it exists (workspace context). Follow it strictly. " +
        "Do not infer or repeat old tasks from prior chats. If nothing needs attention, reply HEARTBEAT_OK.\n" +
        `System: [2026-07-19 12:00:00 JST] ${systemEvent}`,
    },
    {
      runId,
      sessionKey,
      messageProvider: "slack",
      trigger: "heartbeat",
      // heartbeat の送信者欄は権威ではない（plugin は見ない）。
      senderId: "U8888888888",
      channel: "slack",
      channelId: hookChannelId,
      chatId: hookChannelId,
    },
  );
}

function call({runId, toolCallId, tool, params, sessionKey, hookChannelId, declaredUser}) {
  const toolName = `teamagent__${tool}`;
  return (
    hooks.before_tool_call(
      {
        toolName,
        runId,
        toolCallId,
        params: {...params, _user_context: {slack_user_id: declaredUser}},
      },
      {
        toolName,
        runId,
        toolCallId,
        sessionKey,
        messageProvider: "slack",
        channel: "slack",
        channelId: hookChannelId,
        chatId: hookChannelId,
      },
    ) ?? null
  );
}

function claimOf(result) {
  const claim = result?.params?._user_context?.caller_claim;
  if (typeof claim !== "string") return null;
  return JSON.parse(Buffer.from(claim.split(".")[0], "base64url").toString("utf8"));
}

function summarize(result) {
  return {
    block: result?.block === true,
    blockReason: result?.blockReason ?? null,
    params: result?.params ?? null,
    claim: claimOf(result),
  };
}

const A = input.userA;
const B = input.userB;
const T = input.tokens;
const out = {
  registeredNamespaces: [...registrations.values()].map(r => r.namespace).sort(),
  exportedBindings: mod.ACTION_BINDINGS ?? null,
};

// ── A. 📅 DM で押す（heartbeat の hook channel は U…）→ 呼べる・一回だけ ─────────────
{
  const messageTs = "1784424000.000101";
  const pressed = await press({
    actionId: "calendar_event",
    value: T.event,
    userId: A,
    channelId: input.dmA,
    messageTs,
  });
  const runId = "a1111111-1111-4111-8111-111111111111";
  const run = {runId, sessionKey: dmSession(A), hookChannelId: A};
  heartbeat({...run, systemEvent: pressed.systemEvent});
  // モデルは system event に見えている（切れた）value をそのまま渡してくる。
  const first = call({
    ...run,
    toolCallId: "toolu_cal_dm_first_0001",
    tool: "calendar_event",
    params: {event_token: pressed.systemEventValue},
    declaredUser: A,
  });
  const second = call({
    ...run,
    toolCallId: "toolu_cal_dm_second_0001",
    tool: "calendar_event",
    params: {event_token: pressed.systemEventValue},
    declaredUser: A,
  });
  const replayPress = await press({
    actionId: "calendar_event",
    value: T.event,
    userId: A,
    channelId: input.dmA,
    messageTs,
  });
  const replayRunId = "a2222222-2222-4222-8222-222222222222";
  const replayRun = {runId: replayRunId, sessionKey: dmSession(A), hookChannelId: A};
  heartbeat({...replayRun, systemEvent: pressed.systemEvent});
  const crossRun = call({
    ...replayRun,
    toolCallId: "toolu_cal_dm_cross_run_0001",
    tool: "calendar_event",
    params: {event_token: pressed.systemEventValue},
    declaredUser: A,
  });
  out.dmCalendar = {
    press: {
      matched: pressed.matched,
      handlerResult: pressed.handlerResult,
      enqueued: pressed.enqueued,
      systemEventValue: pressed.systemEventValue,
    },
    first: summarize(first),
    second: summarize(second),
    replayPress: {handlerResult: replayPress.handlerResult, enqueued: replayPress.enqueued},
    crossRun: summarize(crossRun),
  };
}

// ── B. 📅 DM（hook channel が D… のまま来る場合）→ そのまま一致 ───────────────────
{
  const pressed = await press({
    actionId: "calendar_event",
    value: T.event,
    userId: A,
    channelId: input.dmA,
    messageTs: "1784424000.000102",
  });
  const run = {
    runId: "b1111111-1111-4111-8111-111111111111",
    sessionKey: dmSession(A),
    hookChannelId: input.dmA,
  };
  heartbeat({...run, systemEvent: pressed.systemEvent});
  out.dmCalendarDirect = summarize(
    call({
      ...run,
      toolCallId: "toolu_cal_dm_direct_0001",
      tool: "calendar_event",
      params: {event_token: pressed.systemEventValue},
      declaredUser: A,
    }),
  );
}

// ── C. 📅 の押下で別ツールを呼ぶ → 止まる（止められた後も正しいツールは 1 回呼べる）──
{
  const pressed = await press({
    actionId: "calendar_event",
    value: T.event,
    userId: A,
    channelId: input.dmA,
    messageTs: "1784424000.000103",
  });
  const run = {
    runId: "c1111111-1111-4111-8111-111111111111",
    sessionKey: dmSession(A),
    hookChannelId: A,
  };
  heartbeat({...run, systemEvent: pressed.systemEvent});
  out.wrongTool = {
    mailDraft: summarize(
      call({
        ...run,
        toolCallId: "toolu_wrong_mail_draft_0001",
        tool: "mail_draft",
        params: {draft_token: T.draft},
        declaredUser: A,
      }),
    ),
    schedulePropose: summarize(
      call({
        ...run,
        toolCallId: "toolu_wrong_schedule_0001",
        tool: "schedule_propose",
        params: {schedule_token: T.draft},
        declaredUser: A,
      }),
    ),
    search: summarize(
      call({
        ...run,
        toolCallId: "toolu_wrong_search_0001",
        tool: "echo",
        params: {q: "wrong tool"},
        declaredUser: A,
      }),
    ),
    thenRight: summarize(
      call({
        ...run,
        toolCallId: "toolu_wrong_then_right_0001",
        tool: "calendar_event",
        params: {event_token: pressed.systemEventValue},
        declaredUser: A,
      }),
    ),
  };
}

// ── D. 値の形が違う押下 → 捕捉しない（system event が来ても束縛されない）─────────────
{
  const cases = {
    draftTypedOnCalendar: {actionId: "calendar_event", value: T.draft},
    eventTypedOnSchedule: {actionId: "schedule_propose", value: T.event},
    draftTypedOnAck: {actionId: "digest_ack", value: T.draft},
    garbage: {actionId: "calendar_event", value: "not-a-token"},
    tooLong: {actionId: "calendar_event", value: T.tooLong},
    // mail_draft の上限は従来どおり 160（160 字を超える値は、形が合っていても捕捉しない）。
    mailDraftOver160: {actionId: "mail_draft", value: T.event},
  };
  out.shape = {};
  let index = 0;
  for (const [name, spec] of Object.entries(cases)) {
    index += 1;
    const pressed = await press({
      ...spec,
      userId: A,
      channelId: input.dmA,
      messageTs: `1784424000.0002${String(index).padStart(2, "0")}`,
    });
    const run = {
      runId: `d${index}111111-1111-4111-8111-111111111111`,
      sessionKey: dmSession(A),
      hookChannelId: A,
    };
    heartbeat({...run, systemEvent: pressed.systemEvent});
    const tokenParam = {
      mail_draft: "draft_token",
      calendar_event: "event_token",
      schedule_propose: "schedule_token",
      digest_ack: "ack_token",
    }[spec.actionId];
    out.shape[name] = {
      handlerResult: pressed.handlerResult,
      call: summarize(
        call({
          ...run,
          toolCallId: `toolu_shape_${name}_0001`,
          tool: spec.actionId,
          params: {[tokenParam]: pressed.systemEventValue},
          declaredUser: A,
        }),
      ),
    };
  }
  const unauthorized = await press({
    actionId: "calendar_event",
    value: T.event,
    userId: A,
    channelId: input.dmA,
    messageTs: "1784424000.000299",
    authorized: false,
  });
  out.shape.unauthorized = {handlerResult: unauthorized.handlerResult};
}

// ── E. メッセージ由来（自由文）: 📅 は今までどおり通る・ボタン専用ツールは止まる ─────
{
  const messageId = "1784424000.000301";
  const sessionKey = dmSession(A);
  hooks.message_received(
    {
      content: "明日15時にA社と打合せ、カレンダーに入れといて",
      messageId,
      senderId: A,
      metadata: {guildId: input.teamId, to: `user:${A}`, messageId, senderId: A},
    },
    {channelId: "slack", sessionKey, senderId: A, conversationId: `user:${A}`, messageId},
  );
  const runId = "e1111111-1111-4111-8111-111111111111";
  // 本番実測（plugin の bindAgentRun 注記・2026-08-03）: DM の通常 run は D… を名乗る。
  const run = {runId, sessionKey, hookChannelId: input.dmA};
  hooks.before_model_resolve(
    {prompt: "明日15時にA社と打合せ、カレンダーに入れといて"},
    {
      runId,
      sessionKey,
      messageProvider: "slack",
      trigger: "user",
      senderId: A,
      channel: "slack",
      channelId: input.dmA,
      chatId: input.dmA,
    },
  );
  const freeform = {title: "A社と打合せ", start: "2026-07-20T15:00:00+09:00"};
  out.message = {
    freeformPlain: summarize(
      call({
        ...run,
        toolCallId: "toolu_msg_freeform_0001",
        tool: "calendar_event",
        params: freeform,
        declaredUser: A,
      }),
    ),
    freeformForgedToken: summarize(
      call({
        ...run,
        toolCallId: "toolu_msg_forged_event_0001",
        tool: "calendar_event",
        params: {...freeform, event_token: T.event},
        declaredUser: A,
      }),
    ),
    schedulePropose: summarize(
      call({
        ...run,
        toolCallId: "toolu_msg_schedule_0001",
        tool: "schedule_propose",
        params: {schedule_token: T.draft},
        declaredUser: A,
      }),
    ),
    digestAck: summarize(
      call({
        ...run,
        toolCallId: "toolu_msg_ack_0001",
        tool: "digest_ack",
        params: {ack_token: T.ack},
        declaredUser: A,
      }),
    ),
    mailDraft: summarize(
      call({
        ...run,
        toolCallId: "toolu_msg_mail_draft_0001",
        tool: "mail_draft",
        params: {draft_token: T.draft},
        declaredUser: A,
      }),
    ),
  };
}

// ── F. 🗓（draft 形式・160 字以内）と、同じ行の ✏️（同じ value）を別々に押す ──────────
{
  const messageTs = "1784424000.000401";
  const schedulePressed = await press({
    actionId: "schedule_propose",
    value: T.draft,
    userId: A,
    channelId: input.dmA,
    messageTs,
  });
  const scheduleRun = {
    runId: "f1111111-1111-4111-8111-111111111111",
    sessionKey: dmSession(A),
    hookChannelId: A,
  };
  heartbeat({...scheduleRun, systemEvent: schedulePressed.systemEvent});
  const schedule = call({
    ...scheduleRun,
    toolCallId: "toolu_schedule_dm_0001",
    tool: "schedule_propose",
    params: {schedule_token: schedulePressed.systemEventValue},
    declaredUser: A,
  });
  const mailPressed = await press({
    actionId: "mail_draft",
    value: T.draft,
    userId: A,
    channelId: input.dmA,
    messageTs,
  });
  const mailRun = {
    runId: "f2222222-2222-4222-8222-222222222222",
    sessionKey: dmSession(A),
    hookChannelId: A,
  };
  heartbeat({...mailRun, systemEvent: mailPressed.systemEvent});
  const mail = call({
    ...mailRun,
    toolCallId: "toolu_mail_draft_dm_0001",
    tool: "mail_draft",
    params: {draft_token: "model-forged-token"},
    declaredUser: A,
  });
  out.sameRow = {
    scheduleHandler: schedulePressed.handlerResult,
    schedule: summarize(schedule),
    mailHandler: mailPressed.handlerResult,
    mailDraft: summarize(mail),
  };
}

// ── G. ☑️ 一括（ackall・160 字超）/ ☑️ でツールが無効（別ツールを試みる）─────────────
{
  const pressed = await press({
    actionId: "digest_ack",
    value: T.ackAll,
    userId: A,
    channelId: input.dmA,
    messageTs: "1784424000.000501",
  });
  const run = {
    runId: "g1111111-1111-4111-8111-111111111111",
    sessionKey: dmSession(A),
    hookChannelId: A,
  };
  heartbeat({...run, systemEvent: pressed.systemEvent});
  out.ackAll = {
    systemEventValue: pressed.systemEventValue,
    call: summarize(
      call({
        ...run,
        toolCallId: "toolu_ack_all_0001",
        tool: "digest_ack",
        params: {ack_token: pressed.systemEventValue},
        declaredUser: A,
      }),
    ),
  };
  const single = await press({
    actionId: "digest_ack",
    value: T.ack,
    userId: A,
    channelId: input.dmA,
    messageTs: "1784424000.000502",
  });
  const disabledRun = {
    runId: "g2222222-2222-4222-8222-222222222222",
    sessionKey: dmSession(A),
    hookChannelId: A,
  };
  heartbeat({...disabledRun, systemEvent: single.systemEvent});
  // digest_ack が mcp で無効（tools/list に無い）とき、モデルが代わりに他のツールを呼んでも止まる。
  out.ackDisabled = {
    search: summarize(
      call({
        ...disabledRun,
        toolCallId: "toolu_ack_disabled_search_0001",
        tool: "echo",
        params: {q: "代わりに何か"},
        declaredUser: A,
      }),
    ),
    calendar: summarize(
      call({
        ...disabledRun,
        toolCallId: "toolu_ack_disabled_calendar_0001",
        tool: "calendar_event",
        params: {title: "代わり", start: "2026-07-20T15:00:00+09:00"},
        declaredUser: A,
      }),
    ),
  };
}

// ── H. チャンネルのスレッドで 📅（従来の C… 経路）──────────────────────────────
{
  const threadTs = "1784423000.000001";
  const pressed = await press({
    actionId: "calendar_event",
    value: T.event,
    userId: A,
    channelId: input.channel,
    messageTs: "1784424000.000601",
    threadTs,
  });
  const run = {
    runId: "h1111111-1111-4111-8111-111111111111",
    sessionKey: channelSession(input.channel, threadTs),
    hookChannelId: channelHookId(input.channel, threadTs),
  };
  heartbeat({...run, systemEvent: pressed.systemEvent});
  out.channelThread = summarize(
    call({
      ...run,
      toolCallId: "toolu_cal_channel_thread_0001",
      tool: "calendar_event",
      params: {event_token: pressed.systemEventValue},
      declaredUser: A,
    }),
  );
}

// ── I. 他人の DM の heartbeat run で A の押下を使おうとする → 止まる ───────────────
{
  const pressed = await press({
    actionId: "calendar_event",
    value: T.event,
    userId: A,
    channelId: input.dmA,
    messageTs: "1784424000.000701",
  });
  const run = {
    runId: "i1111111-1111-4111-8111-111111111111",
    sessionKey: dmSession(B),
    hookChannelId: B,
  };
  heartbeat({...run, systemEvent: pressed.systemEvent});
  const foreignRun = call({
    ...run,
    toolCallId: "toolu_cross_user_dm_0001",
    tool: "calendar_event",
    params: {event_token: pressed.systemEventValue},
    declaredUser: B,
  });
  // B の DM に「B が押した」と書き換えた system event（捕捉は A の分しか無い）。
  const forgedEvent = pressed.systemEvent.replace(`"userId":"${A}"`, `"userId":"${B}"`);
  const forgedRun = {
    runId: "i2222222-2222-4222-8222-222222222222",
    sessionKey: dmSession(B),
    hookChannelId: B,
  };
  heartbeat({...forgedRun, systemEvent: forgedEvent});
  const forgedSender = call({
    ...forgedRun,
    toolCallId: "toolu_cross_user_forged_0001",
    tool: "calendar_event",
    params: {event_token: pressed.systemEventValue},
    declaredUser: B,
  });
  // 本人の DM の run なら同じ押下はまだ使える（他人の run に消費されていない）。
  const ownRun = {
    runId: "i3333333-3333-4333-8333-333333333333",
    sessionKey: dmSession(A),
    hookChannelId: A,
  };
  heartbeat({...ownRun, systemEvent: pressed.systemEvent});
  const own = call({
    ...ownRun,
    toolCallId: "toolu_cross_user_own_0001",
    tool: "calendar_event",
    params: {event_token: pressed.systemEventValue},
    declaredUser: A,
  });
  out.crossUser = {
    foreignRun: summarize(foreignRun),
    forgedSender: summarize(forgedSender),
    own: summarize(own),
  };
}

// ── J. 切り詰め後の value が同じ 2 件（同じ件名の定例が 2 行）が同時に待っている ──────────
//   J1: 行が違う（block_id が違う）→ それぞれ自分の行の完全なトークンに束縛される
//   J2: block_id でも見分けられない → どちらか決められないので止める（fail-closed）
{
  const messageTs = "1784424000.000801";
  const twinA = await press({
    actionId: "calendar_event",
    value: T.eventTwinA,
    userId: A,
    channelId: input.dmA,
    messageTs,
    blockId: "digestRow1",
  });
  const twinB = await press({
    actionId: "calendar_event",
    value: T.eventTwinB,
    userId: A,
    channelId: input.dmA,
    messageTs,
    blockId: "digestRow2",
  });
  const runB = {
    runId: "j2222222-2222-4222-8222-222222222222",
    sessionKey: dmSession(A),
    hookChannelId: A,
  };
  // 後から押した行の heartbeat が先に来ても、行（block_id）で取り違えない。
  heartbeat({...runB, systemEvent: twinB.systemEvent});
  const callB = call({
    ...runB,
    toolCallId: "toolu_twin_b_0001",
    tool: "calendar_event",
    params: {event_token: twinB.systemEventValue},
    declaredUser: A,
  });
  const runA = {
    runId: "j1111111-1111-4111-8111-111111111111",
    sessionKey: dmSession(A),
    hookChannelId: A,
  };
  heartbeat({...runA, systemEvent: twinA.systemEvent});
  const callA = call({
    ...runA,
    toolCallId: "toolu_twin_a_0001",
    tool: "calendar_event",
    params: {event_token: twinA.systemEventValue},
    declaredUser: A,
  });

  const sameRowMessageTs = "1784424000.000802";
  const twinSameA = await press({
    actionId: "calendar_event",
    value: T.eventTwinA,
    userId: A,
    channelId: input.dmA,
    messageTs: sameRowMessageTs,
    blockId: "digestRowX",
  });
  await press({
    actionId: "calendar_event",
    value: T.eventTwinB,
    userId: A,
    channelId: input.dmA,
    messageTs: sameRowMessageTs,
    blockId: "digestRowX",
  });
  const runSame = {
    runId: "j3333333-3333-4333-8333-333333333333",
    sessionKey: dmSession(A),
    hookChannelId: A,
  };
  heartbeat({...runSame, systemEvent: twinSameA.systemEvent});
  out.twins = {
    sameSystemEventValue: twinA.systemEventValue === twinB.systemEventValue,
    first: summarize(callB),
    second: summarize(callA),
    ambiguous: summarize(
      call({
        ...runSame,
        toolCallId: "toolu_twin_ambiguous_0001",
        tool: "calendar_event",
        params: {event_token: twinSameA.systemEventValue},
        declaredUser: A,
      }),
    ),
  };
}

process.stdout.write(JSON.stringify(out));
