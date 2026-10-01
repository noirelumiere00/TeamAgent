// OpenClaw 本体の受信処理に、Slack DM と同じ形の入力を渡してターンを回す（雛形取り出し用）。
//
// `openclaw agent --local` は Slack を通らないので、本番の要求にある
//   - system の「Inbound Context (trusted metadata)」（channel=slack・chat_type=direct・書式の指示）
//   - user 発話の後ろの「Conversation info (untrusted metadata)」（送信者 ID・message id 等）
// が欠ける。そこで @openclaw/slack 2026.7.1 の受信処理（dist/pipeline.runtime-*.js の
// prepareSlackMessage → dispatchChannelInboundReply）が組み立てる値を、同じ SDK 関数で組んで
// dispatch する。Slack API には触れない。
//
// 前提として固定するもの（README にも記載）:
//   - 初めての DM で Slack 側の DM 履歴は無い（本番は記録が無いとき conversations.history で
//     直前 dmHistoryLimit 件を前置きするが、ここでは 0 件とみなす）
//   - スレッドではない DM・メンション無し・添付無し
//
// usage（OpenClaw 公式イメージ内・capture.sh から）: node slack_dm_driver.mjs <turns.json>
//   turns.json = 同じ DM で順に送る発話の配列。最後が評価の発話（目印）。
import { readFileSync } from "node:fs";
import {
  buildChannelInboundEventContext,
  formatInboundEnvelope,
  resolveEnvelopeFormatOptions,
} from "openclaw/plugin-sdk/channel-inbound";
import { dispatchReplyWithBufferedBlockDispatcher } from "openclaw/plugin-sdk/reply-runtime";
import { resolveAgentRoute } from "openclaw/plugin-sdk/routing";

const turnsPath = process.argv[2];
if (!turnsPath) throw new Error("usage: node slack_dm_driver.mjs <turns.json>");
const utterances = JSON.parse(readFileSync(turnsPath, "utf8"));

const cfg = JSON.parse(readFileSync(process.env.OPENCLAW_CONFIG_PATH, "utf8"));

// 実在しない ID（評価専用）。本番の Slack ID は使わない。
const user = "U0ROUTEVAL1";
const channel = "D0ROUTEVAL1";
const senderName = "評価ユーザー";
// 評価の発話は 2026-10-01 01:00 UTC（木曜 10:00 JST）。前置きは 5 分おきに前へずらす。
const TARGET_EPOCH_S = 1790816400;

const route = resolveAgentRoute({
  cfg,
  channel: "slack",
  accountId: "default",
  peer: { kind: "direct", id: user },
});
console.error(`[driver] sessionKey=${route.sessionKey} agent=${route.agentId} turns=${utterances.length}`);

async function sendOne(rawBody, epochS) {
  const ts = `${epochS}.000100`;
  const timestamp = epochS * 1000;
  const textWithId = `${rawBody}\n[slack message id: ${ts} channel: ${channel}]`;
  const combinedBody = formatInboundEnvelope({
    channel: "Slack",
    from: senderName,
    timestamp,
    body: textWithId,
    chatType: "direct",
    sender: { name: senderName, id: user },
    previousTimestamp: undefined,
    envelope: resolveEnvelopeFormatOptions(cfg),
  });
  const ctxPayload = buildChannelInboundEventContext({
    channel: "slack",
    accountId: route.accountId,
    messageId: ts,
    timestamp,
    from: `slack:${user}`,
    sender: { id: user, name: senderName, displayLabel: senderName },
    conversation: { kind: "direct", id: channel, label: senderName, nativeChannelId: channel },
    route: { agentId: route.agentId, accountId: route.accountId, routeSessionKey: route.sessionKey },
    reply: { to: `user:${user}`, nativeChannelId: channel },
    message: {
      inboundEventKind: "user_request",
      body: combinedBody,
      bodyForAgent: rawBody,
      rawBody,
      commandBody: rawBody.trim(),
    },
    access: {
      mentions: {
        canDetectMention: false,
        wasMentioned: false,
        hasAnyMention: false,
        implicitMentionKinds: [],
        requireMention: false,
        effectiveWasMentioned: false,
      },
      commands: { authorized: true },
    },
    supplemental: { thread: {} },
    extra: {},
  });
  ctxPayload.ReplyToMode = "off";
  await dispatchReplyWithBufferedBlockDispatcher({
    ctx: ctxPayload,
    cfg,
    dispatcherOptions: {
      deliver: async (payload) => {
        console.error(`[driver] deliver: ${JSON.stringify(payload).slice(0, 200)}`);
      },
      onError: (err) => console.error(`[driver] deliver error: ${String(err)}`),
    },
    replyOptions: {},
  });
}

for (const [i, text] of utterances.entries()) {
  const epochS = TARGET_EPOCH_S - (utterances.length - 1 - i) * 300;
  console.error(`[driver] turn ${i + 1}/${utterances.length}`);
  await sendOne(text, epochS);
}
console.error("[driver] done");
// Slack plugin 等のハンドルが残ってプロセスが終わらないので明示的に終える。
process.exit(0);
