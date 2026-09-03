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

// ── 連携依頼の 3 層防御（2026-09-03） ─────────────────────────────────────
// 本番実測（2026-09-03）: 利用者が DM で「連携」とだけ送っても、Aico がツールを一度も
// 呼ばず「未登録／管理者に問い合わせ」と自作回答する事故が同一 DM で 5 回以上続いた。
// mcp 側には一切届いていない（mcp_connect_intent がゼロ）ため、MCP 境界の決定論分岐も
// 上の URL 検出（応答に URL が無い）も効かない。ここでは 3 層に分けて塞ぐ:
//   層1: before_agent_reply で短い連携依頼を検出し、モデルを通さず oauth_connect を呼ぶ。
//        {handled:true, reply} を返すとハーネスはモデルを起動しない（get-reply:5599-5623）。
//   層2: before_agent_finalize で「0 tool call × 短い連携依頼」を revise で再パスさせる。
//   層3: 再パス後も 0 tool call なら reply_payload_sending で定型文に置換する
//        （event.runId / ctx.runId が agent run と同じ id で渡る: dispatch:2528-2545）。
// 「短い連携依頼」の判定は誤爆を避けるため厳格にする（設計書 §2 の残差法の教訓）:
// 正規化後の本文が 12 文字以下で、連携語＋任意の助詞だけで構成されるものに限る。
// 「〇〇社との連携について提案書を」は長さと構成の両方で外れる。
const OAUTH_CONNECT_TOOL = "oauth_connect";
const CONNECT_REQUEST_MAX_LENGTH = 12;
const CONNECT_REQUEST_SCAN_LIMIT = 512;
// 正規化後の本文がこの形だけで構成されるときに限り「短い連携依頼」とみなす。
const CONNECT_REQUEST_CORE_RE =
  /^(?:再)?(?:google|グーグル|slack|スラック)?[\s\u3000]*(?:再)?(?:連携|接続|connect)[\s\u3000]*[をにのがはへとも]?$/iu;
// 敬語末尾・依頼末尾。長いものから順に、変化が無くなるまで剥がす。
const CONNECT_REQUEST_SUFFIXES = [
  "よろしくお願いいたします",
  "よろしくお願い致します",
  "よろしくお願いします",
  "よろしく",
  "してほしいです",
  "して欲しいです",
  "してほしい",
  "して欲しい",
  "してもらえますか",
  "してもらえる",
  "してくれますか",
  "してくれる",
  "させてください",
  "させて下さい",
  "させて",
  "できますか",
  "できる",
  "してください",
  "して下さい",
  "したいです",
  "したい",
  "して",
  "お願いいたします",
  "お願い致します",
  "おねがいします",
  "お願いします",
  "お願い",
  "ください",
  "下さい",
  "です",
  "する",
  "を",
];
// 前後の空白・句読点・括弧・引用符。
const CONNECT_REQUEST_EDGE_RE =
  /^[\s\u3000、。．，,.!！?？…・:：;；「」『』()（）【】\[\]<>"'`~〜]+|[\s\u3000、。．，,.!！?？…・:：;；「」『』()（）【】\[\]<>"'`~〜]+$/gu;
// 絵文字（Unicode）・Slack の :emoji: コード・Slack マークアップ（<@U…> <!here> <#C…>）。
const CONNECT_REQUEST_EMOJI_RE =
  /\p{Extended_Pictographic}|\p{Emoji_Modifier}|\uFE0F|\u200D|[\u{1F1E6}-\u{1F1FF}]/gu;
const CONNECT_REQUEST_SLACK_EMOJI_RE = /:[a-z0-9_+-]{1,64}:/giu;
const CONNECT_REQUEST_SLACK_MARKUP_RE = /<[@!#][^>]{0,64}>/gu;
const CONNECT_ZERO_TOOL_RETRY_KEY = "connect-zero-tool";
const CONNECT_ZERO_TOOL_REASON =
  "利用者の短い連携依頼に対し、ツールを 1 つも呼ばずに回答しようとしています。";
// 固定文（依頼仕様どおり・変更しない）。
const CONNECT_ZERO_TOOL_INSTRUCTION =
  "利用者は Google/Slack 連携を依頼しています。`oauth_connect` ツールを必ず呼び、" +
  "その戻り値の message とリンクを一字も変えずに提示してください。" +
  "自分で原因を推測したり、管理者への問い合わせを案内したりしてはいけません。";
const CONNECT_DIAGNOSTIC_CODE = "CONNECT-Z01";
const CONNECT_FALLBACK_CANCEL_REASON = "connect zero-tool fallback already delivered";
// 層1 が叩く MCP。本番は Cloud Map（rollout-task-canary.mjs と同じ定数）、ローカルは env で上書き。
const DEFAULT_MCP_URL = "http://teamagent-mcp.teamagent.internal:8787/mcp";
// 層1 の 3 POST（initialize / initialized / tools/call）で共有する全体予算。
// claim TTL（60s）と同長にしない: 超過は fallthrough でモデル経路へ渡す。
const MCP_REQUEST_TIMEOUT_MS = 15_000;
const MCP_PROTOCOL_VERSION = "2025-03-26";
const MCP_CLIENT_NAME = "teamagent-caller-identity-connect";
const CONNECT_L1_INVOCATION_PREFIX = "connect-l1";
const SLACK_CANONICAL_CHANNEL_RE = /^[CDG][A-Z0-9]{8,}$/u;
const SLACK_DM_CHANNEL_RE = /^D[A-Z0-9]{8,}$/u;

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

// ── 拒否の観測性と利用者向け診断行（2026-09-03 実測） ─────────────────────────
// 実測（OpenClaw の EFS 上のセッション記録 166 ファイル・tool call 363 件を読み取り専用の
// Fargate プローブで集計）:
//   83 件（23%）が before_tool_call で block（toolResult details.status="blocked",
//   deniedReason="plugin-before-tool-call"）。内訳は
//     `_user_context must be a plain object`                     72
//     `trusted Slack run identity is missing or stale`             9
//     `declared channel_id does not match the bound ingress`       2
//   全滅セッション（その run の tool call が全部 block）が 7 本以上。ツールも問わない
//   （oauth_connect / search / calendar_event / tiktok_* / mail_summary / slack_summary …）。
// それでも CloudWatch にはこの plugin の warn が 14 日間 1 行も無かった。理由は単純で、
// signToolCall だけ logger を受け取っておらず（register の before_tool_call だけが
// api.logger を渡していなかった）、block 経路は 1 行も書いていなかった。
// 利用者側にはブロックされた toolResult を見たモデルの自作回答（「技術的な問題」
// 「管理者へお問い合わせ」）だけが届き、原因が誰にも見えていなかった。
//
// ここで直すのは 2 つ:
//   (1) 利用者へ: block 文の末尾に固定の診断行 `診断: CONNECT-P<nn> <時刻 JST>` を付ける。
//       SOUL(#380) が「診断: 行は一字も変えず提示」を規定しているのでそのまま転送される。
//   (2) 管理者へ: 拒否ごとに必ず 1 行ログを出す。値は載せず「形」だけ（id_shape）。
// コード体系の流儀は src/teamagent/connect_diagnostics.py（ConnectDiag / DIAG_SPECS）に
// 合わせ、意味・ログの引き方・対処は docs/runbooks/connect_diagnostics.md の P コード節が正本。
// 系統 P = plugin（OpenClaw の before_tool_call・本人特定 plugin）。
export const BLOCK_DIAG = Object.freeze({
  // 母艦ネイティブのツール（message/filesystem/session 系）は署名対象外なので常に拒否。
  NATIVE_TOOL_DENIED: "CONNECT-P01",
  // event と ctx のツール名が食い違う / mail_draft の権威が無い。
  TOOL_NAME_BINDING: "CONNECT-P02",
  // run の束縛が無い・古い（`trusted Slack run identity is missing or stale` を含む）。
  RUN_BINDING: "CONNECT-P03",
  // toolCallId の束縛が無い・再生（replay）。
  INVOCATION_BINDING: "CONNECT-P04",
  // session/channel の束縛（`declared channel_id does not match the bound ingress` を含む）。
  SESSION_OR_CHANNEL_BINDING: "CONNECT-P05",
  // `_user_context` の形が不正（unwrap しても直らなかった場合）。
  USER_CONTEXT_SHAPE: "CONNECT-P06",
  // plugin 内部の署名失敗（nonce 生成・claim 鋳造）。利用者操作では直らない。
  SIGNING_FAILED: "CONNECT-P07",
});

// connect_diagnostics.py の admin_name() と同じ既定・同じ env 名。
const ADMIN_NAME_ENV = "CONNECT_ADMIN_NAME";
const DEFAULT_ADMIN_NAME = "小俣";

function adminForwardHint(adminName) {
  return `解決しない場合は、次の 1 行をそのまま管理者（${adminName}）へ送ってください:`;
}

// 利用者に届く block 文。1 行目は従来どおりの理由、そのあとに転送用の 2 行。
// user id・本文・URL は載せない（G7）。管理者は runId ではなくコード＋時刻で突合する。
export function formatBlockReason(reason, code, nowMs, adminName = DEFAULT_ADMIN_NAME) {
  return (
    `${PLUGIN_ID}: ${reason}\n` +
    `${adminForwardHint(adminName)}\n` +
    `診断: ${code} ${formatJstMinute(nowMs)}`
  );
}

function isPlainObject(value) {
  return (
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    Object.getPrototypeOf(value) === Object.prototype
  );
}

// ── 拒否ログに載せてよい「形」だけの手掛かり（G7） ───────────────────────────
// 値（Slack user id・channel id・ts）は出さず、先頭 1 文字や構造の有無だけを出す。
// Enterprise Grid の `W…` user id など、想定外の id 形で拒否が出ていないかを
// 本番ログから値を見ずに切り分けるためのもの。
const ID_SHAPE_TOKEN_RE = /^[A-Za-z][A-Za-z0-9]{8,}$/u;

function shapeOfSlackId(value) {
  if (typeof value !== "string" || value.trim() === "") return "absent";
  const trimmed = value.trim();
  return ID_SHAPE_TOKEN_RE.test(trimmed) ? trimmed[0].toUpperCase() : "other";
}

// channel は `C0B0PQD83N2` / `user:U09…` / `c0b0pqd83n2:thread:<ts>` / `slack` の
// いずれも来る。`:thread:` を落とし、最後のセグメントの先頭 1 文字だけを見る。
function shapeOfChannel(value) {
  if (typeof value !== "string" || value.trim() === "") return "absent";
  const stripped = value.trim().replace(SLACK_SESSION_THREAD_SUFFIX_RE, "");
  const tail = stripped.split(":").pop() ?? "";
  return ID_SHAPE_TOKEN_RE.test(tail) ? tail[0].toUpperCase() : "other";
}

export function idShape(fields) {
  const parts = [];
  if (Object.hasOwn(fields, "sender")) {
    parts.push(`sender:${shapeOfSlackId(fields.sender)}`);
  }
  if (Object.hasOwn(fields, "channel")) {
    parts.push(`channel:${shapeOfChannel(fields.channel)}`);
  }
  if (Object.hasOwn(fields, "message")) {
    const message = fields.message;
    parts.push(
      `message:${
        typeof message !== "string" || message.trim() === ""
          ? "absent"
          : SLACK_TS_RE.test(message.trim())
            ? "ts"
            : "other"
      }`,
    );
  }
  if (Object.hasOwn(fields, "session")) {
    const session = fields.session;
    parts.push(
      `session:${
        typeof session !== "string" || session.trim() === ""
          ? "absent"
          : session.includes(":thread:")
            ? "thread"
            : "plain"
      }`,
    );
  }
  if (Object.hasOwn(fields, "team")) {
    const team = fields.team;
    parts.push(
      `team:${
        typeof team !== "string" || team.trim() === ""
          ? "absent"
          : normalizeSlackId(team, SLACK_TEAM_RE) === fields.expectedTeam
            ? "match"
            : "mismatch"
      }`,
    );
  }
  return `id_shape=${parts.join(",")}`;
}

// この plugin の観測ログはすべてここを通る。
// 一次検証の結論は docs/design/connect_third_layer_defense.md §11（file:line つき）。
// logger 側の到達が logging 設定（consoleLevel / OPENCLAW_LOG_LEVEL）に左右されるため、
// 拒否の観測をその設定に依存させない目的で console にも同じ 1 行を書く。
// 出してよいのは理由コード・件数・形（id_shape）だけ。本文・URL・Slack user id・
// channel id・claim・bearer は載せない（G7）。
// 詳細トレースの env。既定 OFF。ON にすると「hook が呼ばれた事実」と、
// 従来は無言で return していた全脱出経路が 1 行ずつ出る。
// 常時出すと通常の会話 1 通ごとに数行増える（not_connect_request が毎回出る）ため、
// 事故の切り分け中だけ OC のタスク定義で `TEAMAGENT_CALLER_IDENTITY_TRACE=1` を注入する。
const TRACE_ENV = "TEAMAGENT_CALLER_IDENTITY_TRACE";

export function emitPluginLog(logger, level, message) {
  const line = `${PLUGIN_ID}: ${message}`;
  logger?.[level]?.(line);
  if (level === "warn") console.warn(line);
  else console.info(line);
}

// ── ツール引数の二重包みを剥がす（2026-09-03 実測・本 PR の主眼） ─────────────
// モデル（Bedrock jp.anthropic.claude-haiku-4-5-20251001-v1:0）が tool の引数を
// もう一段包んで送る癖があり、実測 363 件中 76 件がこの形だった:
//   {"arguments":{"_user_context":{…}}}                               74 件
//   {"name":"teamagent__oauth_connect","arguments":{…}}                2 件
// この形は `_user_context` がトップに無いので `_user_context must be a plain object`
// で block され、利用者には「連携できない」としか見えていなかった（72 件がこれ）。
// セッションを作り直しても再発するので履歴汚染ではなくモデル側の癖と見る。
//
//   (a) トップのキーが `arguments` 1 つだけで、その値がプレーンオブジェクト → その値を採用
//   (b) キー集合が {name, arguments} で `name` が呼び出し中のツール名
//       （`teamagent__<tool>` または `<tool>`）と一致し、`arguments` がプレーンオブジェクト
//       → `arguments` を採用
//   (c) 上記を最大 2 段まで再帰。2 段剥がしてもまだ包みなら、剥がさず元のまま返す
//       （＝3 段以上は従来どおり block。診断 P06）
//   (d) それ以外は無変更（同じオブジェクト参照をそのまま返す＝バイト同一）
//
// 信頼境界は動かない: unwrap したあとも `_user_context` は mintCallerClaim が
// authoritative な署名済み値で上書きするため、利用者・モデル由来の `_user_context` は
// 元々すべて破棄される。ここで剥がすのは「どの階層を検査するか」だけ。
const TOOL_ARGUMENTS_UNWRAP_MAX_DEPTH = 2;

export function unwrapToolArguments(params, toolName) {
  const acceptedNames = new Set();
  if (typeof toolName === "string" && toolName !== "") {
    acceptedNames.add(toolName);
    const canonical = canonicalToolName(toolName);
    if (canonical !== null) acceptedNames.add(canonical);
  }
  const wrapperOf = value => {
    if (!isPlainObject(value)) return null;
    const keys = Object.keys(value);
    if (keys.length === 1 && keys[0] === "arguments" && isPlainObject(value.arguments)) {
      return { kind: "arguments", inner: value.arguments };
    }
    if (
      keys.length === 2 &&
      keys.includes("name") &&
      keys.includes("arguments") &&
      isPlainObject(value.arguments) &&
      typeof value.name === "string" &&
      acceptedNames.has(value.name)
    ) {
      return { kind: "name_arguments", inner: value.arguments };
    }
    return null;
  };

  let current = params;
  const kinds = [];
  for (let depth = 0; depth < TOOL_ARGUMENTS_UNWRAP_MAX_DEPTH; depth += 1) {
    const wrapper = wrapperOf(current);
    if (wrapper === null) break;
    kinds.push(wrapper.kind);
    current = wrapper.inner;
  }
  // 包みでなかった、または 2 段剥がしてもまだ包み（3 段以上）→ 無変更で返す。
  if (kinds.length === 0 || wrapperOf(current) !== null) {
    return { params, depth: 0, shape: null };
  }
  return {
    params: current,
    depth: kinds.length,
    shape: [...new Set(kinds)].join("+"),
  };
}

function normalizeConnectRequest(text) {
  if (typeof text !== "string") return null;
  // 長文は正規化する前に落とす（判定コストを固定し、長文が誤って通る余地も残さない）。
  if (text.length > CONNECT_REQUEST_SCAN_LIMIT) return null;
  let value = text
    .normalize("NFKC")
    .replace(CONNECT_REQUEST_SLACK_MARKUP_RE, " ")
    .replace(CONNECT_REQUEST_SLACK_EMOJI_RE, " ")
    .replace(CONNECT_REQUEST_EMOJI_RE, " ");
  let previous = null;
  while (previous !== value) {
    previous = value;
    value = value.replace(CONNECT_REQUEST_EDGE_RE, "");
    for (const suffix of CONNECT_REQUEST_SUFFIXES) {
      if (value.endsWith(suffix) && value.length > suffix.length) {
        value = value.slice(0, -suffix.length);
        break;
      }
    }
  }
  return value;
}

// 「短い連携依頼」判定。部分一致ではなく、正規化後の本文全体が
// 連携語（＋任意の助詞）だけで構成され、かつ 12 文字以下のときに限り真。
export function isShortConnectRequest(text) {
  const normalized = normalizeConnectRequest(text);
  if (normalized === null || normalized.length === 0) return false;
  if ([...normalized].length > CONNECT_REQUEST_MAX_LENGTH) return false;
  return CONNECT_REQUEST_CORE_RE.test(normalized);
}

// JST の "YYYY-MM-DD HH:MM JST"。Intl に依存せず決定論的に組む。
function formatJstMinute(nowMs) {
  const jst = new Date(nowMs + 9 * 60 * 60 * 1000);
  const pad = value => String(value).padStart(2, "0");
  return (
    `${jst.getUTCFullYear()}-${pad(jst.getUTCMonth() + 1)}-${pad(jst.getUTCDate())} ` +
    `${pad(jst.getUTCHours())}:${pad(jst.getUTCMinutes())} JST`
  );
}

// 層3 の定型文。URL も秘匿値も含めない。診断行は利用者→管理者へ転記される前提。
export function buildConnectFallbackText({ senderId, nowMs }) {
  return (
    "連携リンクの発行に失敗しました。もう一度『連携』と送ってください。" +
    "解決しない場合は次の 1 行を管理者（小俣）へ送ってください: " +
    `診断: ${CONNECT_DIAGNOSTIC_CODE} ${formatJstMinute(nowMs)} ${senderId}`
  );
}

class ConnectPathError extends Error {
  constructor(code) {
    super(code);
    this.name = "ConnectPathError";
    this.code = code;
  }
}

function connectPathReason(error) {
  if (error instanceof ConnectPathError) return error.code;
  if (error && typeof error === "object" && error.name === "TimeoutError") return "timeout";
  if (error && typeof error === "object" && error.name === "AbortError") return "timeout";
  return "unexpected";
}

function parseJsonRpcPayload(text, contentType, expectedId) {
  if ((contentType ?? "").toLowerCase().includes("text/event-stream")) {
    for (const block of text.split(/\r?\n\r?\n/u)) {
      for (const line of block.split(/\r?\n/u)) {
        if (!line.startsWith("data:")) continue;
        const payload = JSON.parse(line.slice(5).trim());
        if (payload?.id === expectedId) return payload;
      }
    }
    throw new ConnectPathError("mcp_sse_missing_id");
  }
  return JSON.parse(text);
}

// 層1 の MCP クライアント。rollout-task-canary.mjs と同じ手順（initialize →
// notifications/initialized → tools/call）で、既存の bearer と署名 claim をそのまま使う。
// 新しい信頼境界は作らない: mcp 側は before_tool_call 経由と同じ検証を通す。
async function callMcpTool({ fetchFn, mcpUrl, bearer, name, toolArguments, timeoutMs }) {
  // 全体予算を 1 本の signal で共有する（POST ごとに timeoutMs を持たない）。
  const signal = AbortSignal.timeout(timeoutMs);
  const buildHeaders = sessionId => ({
    Accept: "application/json, text/event-stream",
    Authorization: `Bearer ${bearer}`,
    "Content-Type": "application/json",
    ...(sessionId ? { "Mcp-Session-Id": sessionId } : {}),
  });
  const post = async (body, sessionId) => {
    let response;
    try {
      response = await fetchFn(mcpUrl, {
        method: "POST",
        headers: buildHeaders(sessionId),
        body: JSON.stringify(body),
        signal,
      });
    } catch (error) {
      throw error?.name === "TimeoutError" || error?.name === "AbortError"
        ? new ConnectPathError("timeout")
        : new ConnectPathError("fetch_failed");
    }
    if (!response?.ok) throw new ConnectPathError(`mcp_http_${response?.status ?? "unknown"}`);
    return response;
  };
  const readResult = async (response, expectedId) => {
    let payload;
    try {
      payload = parseJsonRpcPayload(
        await response.text(),
        response.headers?.get?.("content-type"),
        expectedId,
      );
    } catch (error) {
      if (error instanceof ConnectPathError) throw error;
      throw new ConnectPathError("mcp_invalid_json");
    }
    if (payload?.id !== expectedId) throw new ConnectPathError("mcp_rpc_id_mismatch");
    if (payload.error) throw new ConnectPathError("mcp_rpc_error");
    return payload.result;
  };
  const initialized = await post({
    jsonrpc: "2.0",
    id: 1,
    method: "initialize",
    params: {
      protocolVersion: MCP_PROTOCOL_VERSION,
      capabilities: {},
      clientInfo: { name: MCP_CLIENT_NAME, version: "1" },
    },
  });
  const sessionId = initialized.headers?.get?.("mcp-session-id") || null;
  await readResult(initialized, 1);
  await post({ jsonrpc: "2.0", method: "notifications/initialized", params: {} }, sessionId);
  const called = await post(
    { jsonrpc: "2.0", id: 2, method: "tools/call", params: { name, arguments: toolArguments } },
    sessionId,
  );
  return readResult(called, 2);
}

// tools/call の戻り値（TextContent の JSON 文字列）から oauth_connect の message を取り出す。
function extractConnectMessage(result) {
  if (!result || typeof result !== "object" || result.isError === true) {
    throw new ConnectPathError("mcp_tool_error");
  }
  const first = Array.isArray(result.content)
    ? result.content.find(item => item?.type === "text" && typeof item.text === "string")
    : null;
  if (!first) throw new ConnectPathError("mcp_invalid_result");
  let data;
  try {
    data = JSON.parse(first.text);
  } catch {
    throw new ConnectPathError("mcp_invalid_result");
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    throw new ConnectPathError("mcp_invalid_result");
  }
  if (typeof data.error === "string") throw new ConnectPathError("mcp_tool_error");
  const message = typeof data.message === "string" ? data.message.trim() : "";
  if (!message) throw new ConnectPathError("mcp_invalid_result");
  return message;
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
  fetchFn = globalThis.fetch,
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
  // 診断行の転送先。connect_diagnostics.admin_name() と同じ env 名・同じ既定。
  const adminName =
    (typeof env[ADMIN_NAME_ENV] === "string" ? env[ADMIN_NAME_ENV].trim() : "") ||
    DEFAULT_ADMIN_NAME;
  // 既定 OFF。ON のときだけ「hook が呼ばれた事実」と無言の脱出経路を 1 行ずつ出す。
  const traceEnabled = String(env[TRACE_ENV] ?? "").trim() === "1";
  function emitTrace(logger, message) {
    if (!traceEnabled) return;
    emitPluginLog(logger, "warn", message);
  }
  // 層1 の MCP 接続情報。bearer が無い環境では層1 だけを畳み、層2/3 は生かす
  // （署名経路そのものは bearer に依存しないので fail させない）。
  const rawBearer = env.TEAMAGENT_MCP_BEARER;
  const mcpBearer =
    typeof rawBearer === "string" && rawBearer.trim() && !rawBearer.includes("${")
      ? rawBearer.trim()
      : null;
  const rawMcpUrl = env.TEAMAGENT_MCP_URL;
  const mcpUrl =
    typeof rawMcpUrl === "string" && /^https?:\/\//u.test(rawMcpUrl.trim())
      ? rawMcpUrl.trim()
      : DEFAULT_MCP_URL;

  const pendingByMessage = new Map();
  const pendingActions = new Map();
  const seenActions = new Map();
  const ingressByRun = new Map();
  const rejectedRuns = new Map();
  const consumedInvocations = new Map();
  const toolCallsByRun = new Map();
  const connectRevisionsByRun = new Map();
  // 層3: revise 予算を使い切っても 0 tool call のままだった run。reply_payload_sending で
  // 本文を定型文に置換する。agent_end より後に配信が走りうるので releaseAgentRun では消さず、
  // 他の台帳と同じ TTL/上限掃除に任せる。
  const connectFallbackByRun = new Map();

  function pruneConnectGuardState(nowMs) {
    for (const ledger of [toolCallsByRun, connectRevisionsByRun, connectFallbackByRun]) {
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
      if (matches) {
        // 同一 ingress の再通知（content を伴う側が後から来る経路）でも判定を失わない。
        if (ingress.connectRequest === true) existing.connectRequest = true;
        removePending(ingress);
      } else rejectRun(runId, now(), ingress);
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
      // 2026-09-03 レビュー指摘: team id も実値を出さない。かつては「運用者が
      // どのワークスペースから来たか見えないと直せない」として例外扱いしていたが、
      // emitPluginLog が console へ二重書きする以上、実値を出す面は最小にする。
      // 一致/不一致は id_shape の `team:` で判り、実値が要る調査は Slack 側で行う。
      const missing = [];
      if (!sessionKey) missing.push("sessionKey");
      if (!senderId) missing.push("senderId");
      if (!teamId) missing.push("teamId");
      if (!channelId) missing.push("channelId");
      if (!messageId) missing.push("messageId");
      if (suppliedRunIds.length > 0 && !runId) missing.push("runId");
      const mismatch = teamId && teamId !== expectedTeamId;
      emitPluginLog(
        logger,
        "warn",
        "inbound rejected reason=incomplete_or_foreign" +
          ` missing=[${missing.join(",")}]` +
          `${mismatch ? " foreign_team=true" : ""}` +
          ` suppliedRunIds=${suppliedRunIds.length}` +
          ` ${idShape({
            sender: senderId,
            channel: channelId,
            message: messageId,
            session: sessionKey,
            team: teamId,
            expectedTeam: expectedTeamId,
          })}`,
      );
      return;
    }
    const nowMs = now();
    pruneState(nowMs);
    const pendingKey = JSON.stringify([sessionKey, messageId]);
    // event.content は上流が BodyForCommands ?? RawBody ?? Body から作る利用者の生本文
    // （message-hook-mappers:23 / Slack は commandBody ?? rawBody = 封筒無しの本文）。
    // 本文そのものは保持せず、判定結果の真偽だけを ingress に載せる（G7）。
    const connectRequest =
      typeof event?.content === "string" && isShortConnectRequest(event.content);
    // 層1 の `not_connect_request` を切り分けるための「長さだけ」の手掛かり（G7）。
    // 本文は保持しない。normalizeConnectRequest は走査上限を超える長文で null を返すので、
    // 正規化後の長さ（null＝上限超）と生の長さの両方を持つ。片方だけだと
    // 「空だった」と「長すぎて判定対象外だった」が区別できない。
    const normalizedContent =
      typeof event?.content === "string" ? normalizeConnectRequest(event.content) : null;
    const connectNormalizedLength =
      normalizedContent === null ? null : [...normalizedContent].length;
    const connectContentLength =
      typeof event?.content === "string" ? [...event.content].length : null;
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
      connectRequest,
      connectNormalizedLength,
      connectContentLength,
    };
    const existing = pendingByMessage.get(pendingKey);
    if (existing && !sameIngress(existing, ingress)) {
      pendingByMessage.delete(pendingKey);
      emitPluginLog(logger, "warn", "inbound rejected reason=conflicting_message_identity");
      return;
    }
    pendingByMessage.set(pendingKey, ingress);
    if (runId && !bindRun(runId, ingress)) {
      pendingByMessage.delete(pendingKey);
      emitPluginLog(logger, "warn", "inbound rejected reason=conflicting_run_binding");
      return;
    }
    // 受理側も観測できないと、層1 の no_candidate_ingress が
    // 「受信を記録できていない」のか「照合が外れた」のか区別できない（2026-09-03）。
    emitTrace(
      logger,
      `inbound recorded connect_request=${connectRequest}` +
        ` normalized_len=${connectNormalizedLength === null ? "na" : connectNormalizedLength}` +
        ` content_len=${connectContentLength === null ? "na" : connectContentLength}` +
        ` bound_run=${runId ? "yes" : "no"}` +
        ` pending=${pendingByMessage.size} bound=${ingressByRun.size}` +
        ` ${idShape({
          sender: senderId,
          channel: channelId,
          message: messageId,
          session: sessionKey,
          team: teamId,
          expectedTeam: expectedTeamId,
        })}`,
    );
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

  // 会話（sessionKey × sender × channel）と鮮度で ingress を照合する。
  // DM は inbound 側が `DM:<U…>`、run 側が `D…` と名乗るため、送信者で固定した別名を許す。
  function matchesConversation(ingress, { sessionKey, senderId, channelId, nowMs }) {
    const dmAlias = SLACK_DM_CHANNEL_RE.test(channelId) ? `DM:${senderId}` : null;
    const channelMatches =
      ingress.channelId === channelId || (dmAlias !== null && ingress.channelId === dmAlias);
    return (
      ingress.sessionKey === sessionKey &&
      ingress.senderId === senderId &&
      channelMatches &&
      nowMs - ingress.receivedAtMs <= INBOUND_CONTEXT_TTL_MS
    );
  }

  // 層1（決定論の最前段）。before_agent_reply は利用者トリガの通常応答経路で、
  // モデル起動より前に走る（get-reply:5599）。{handled:true, reply} を返すと
  // ハーネスはその reply をそのまま返し、モデルを起動しない（get-reply:5620-5623）。
  // ここで短い連携依頼を検出したら、既存の署名 claim を oauth_connect 向けに鋳造して
  // mcp の /mcp へ直接 tools/call し、戻り値の message をそのまま Slack へ返す。
  // 失敗はすべて「次の層へ落とす」（undefined を返す＝モデル経路へ進み層2/3 が受ける）。
  // ── 層1 の脱出経路をすべて観測可能にする（2026-09-03 実測） ─────────────────
  // 事故: OC TD:43 着地直後、DM の「連携」で層1 が発火せずモデル経路になった
  // （OC ログに `[agents/tool-policy] tool policy removed 26 tool(s)` ＝モデル起動）。
  // 層1 が handled を返していればモデルは起動しない。しかしどの条件で落ちたかは
  // ログから判別できなかった: fallthrough() を通る 3 経路以外はすべて無言の
  // `return undefined` だったため。以後、全脱出経路に理由を付ける。
  //   outcome=skipped     … 前提条件で層1 に入らなかった（trace ON のときだけ出す）
  //   outcome=fallthrough … 層1 に入ったが実行できずモデル経路へ渡した（常時出す）
  //   outcome=answered    … 層1 が handled で応答した（常時出す）
  // `layer1 entered` が 1 行も出なければ、before_agent_reply hook 自体が
  // 呼ばれていないと確定できる（上流側の問題と切り分けられる）。
  async function answerShortConnectRequest(_event, ctx, logger) {
    const invocationId =
      `${CONNECT_L1_INVOCATION_PREFIX}-${randomBytesFn(16).toString("hex")}`;
    const skipped = (reason, extra = "") => {
      emitTrace(
        logger,
        `connect deterministic path invocation=${invocationId} ` +
          `outcome=skipped reason=${reason}${extra ? ` ${extra}` : ""}`,
      );
      return undefined;
    };
    // hook が呼ばれた事実そのもの。provider / trigger は識別子ではないので値を出す。
    emitTrace(
      logger,
      `layer1 entered provider=${String(ctx?.messageProvider ?? "none").toLowerCase()} ` +
        `trigger=${String(ctx?.trigger ?? "none")}`,
    );
    if (String(ctx?.messageProvider ?? "").toLowerCase() !== "slack") {
      return skipped("not_slack_provider");
    }
    if (ctx?.trigger !== "user") {
      return skipped("trigger_not_user", `trigger=${String(ctx?.trigger ?? "none")}`);
    }
    const sessionKey = nonBlank(ctx?.sessionKey, 2048);
    const senderId = normalizeSlackId(ctx?.senderId, SLACK_USER_RE);
    // ctx.chatId は identity fields（get-reply:5610-5615）が NativeChannelId ?? ChatId
    // （Slack は conversation.id = message.channel、DM では `D…`）で上書きするため、
    // channelId 側（`user:U…` 由来）と食い違いうる。会話照合には channelId 系だけを使い、
    // chatId は DM の正準 `D…` を得る用途にだけ使う。
    const channelId = consistentSlackChannel([ctx?.conversationId, ctx?.channelId, ctx?.channel]);
    if (!sessionKey || !senderId || !channelId) {
      const missing = [];
      if (!sessionKey) missing.push("sessionKey");
      if (!senderId) missing.push("senderId");
      if (!channelId) missing.push("channelId");
      return skipped(
        "missing_session_or_sender_or_channel",
        `missing=[${missing.join(",")}] ${idShape({
          sender: ctx?.senderId,
          channel: ctx?.channelId,
          session: ctx?.sessionKey,
        })}`,
      );
    }
    const nowMs = now();
    pruneState(nowMs);
    // message_received が runId を伴うと ingress は既に run へ束縛され pending から消える
    // （rememberInbound → bindRun → removePending）。束縛済み・未束縛の両方を見る。
    const seen = new Set();
    const candidates = [];
    for (const ingress of [...pendingByMessage.values(), ...ingressByRun.values()]) {
      if (ingress.ingressKind !== "message" || seen.has(ingress.pendingKey)) continue;
      if (!matchesConversation(ingress, { sessionKey, senderId, channelId, nowMs })) continue;
      seen.add(ingress.pendingKey);
      candidates.push(ingress);
    }
    const fallthrough = reason => {
      // G7: 本文・URL・Slack 識別子は載せない。
      emitPluginLog(
        logger,
        "warn",
        `connect deterministic path invocation=${invocationId} ` +
          `outcome=fallthrough reason=${reason}`,
      );
      return undefined;
    };
    if (candidates.length === 0) {
      // 受信が 1 件も照合できない。rememberInbound の `inbound recorded` 行の有無で
      // 「記録できていない」のか「照合が外れた」のかを切り分ける。
      return skipped(
        "no_candidate_ingress",
        `pending=${pendingByMessage.size} bound=${ingressByRun.size}`,
      );
    }
    // 同じ会話に新鮮な受信が 2 件以上あると、どの本文が「連携」かを権威的に決められない。
    // 無言で不発にせず、観測可能な理由でモデル経路へ渡す（bindAgentRun も同じ理由で拒否する）。
    if (candidates.length > 1) return fallthrough("ambiguous_ingress");
    const ingress = candidates[0];
    if (ingress.connectRequest !== true) {
      // 語彙不一致。本文は出さず、正規化後の文字数だけ（G7）。
      const lengthOf = value => (value === null || value === undefined ? "na" : value);
      return skipped(
        "not_connect_request",
        `normalized_len=${lengthOf(ingress.connectNormalizedLength)}` +
          ` content_len=${lengthOf(ingress.connectContentLength)}`,
      );
    }
    // 同じ受信に対して 2 度は鋳造しない（重複発行・往復の防止）。
    if (ingress.connectDeterministicAttempted === true) {
      return skipped("already_attempted");
    }
    ingress.connectDeterministicAttempted = true;
    // mcp の claim 検証は channel に実 Slack 会話 id（^[CDG]…）を要求する
    // （caller_claim.py: _SLACK_CHANNEL_RE）。DM の内部別名 `DM:U…` では通らないので、
    // ctx.chatId の `D…` を、この送信者の DM に限って正準 id として採る（bindAgentRun と同じ規律）。
    const chatChannel = resolveSlackChannel(ctx?.chatId);
    let claimChannel = null;
    if (SLACK_CANONICAL_CHANNEL_RE.test(ingress.channelId)) claimChannel = ingress.channelId;
    else if (
      ingress.channelId === `DM:${senderId}` &&
      chatChannel !== null &&
      SLACK_DM_CHANNEL_RE.test(chatChannel)
    ) {
      claimChannel = chatChannel;
    }
    if (claimChannel === null) return fallthrough("no_canonical_channel");
    if (mcpBearer === null) return fallthrough("no_mcp_bearer");
    if (typeof fetchFn !== "function") return fallthrough("no_fetch");
    try {
      const nonceBytes = randomBytesFn(16);
      if (!Buffer.isBuffer(nonceBytes) || nonceBytes.length !== 16) {
        throw new ConnectPathError("nonce_failed");
      }
      let signed;
      try {
        signed = mintCallerClaim({
          trusted: { ...ingress, channelId: claimChannel },
          runId: invocationId,
          toolCallId: invocationId,
          tool: OAUTH_CONNECT_TOOL,
          params: { [USER_CONTEXT_KEY]: {} },
          nowMs,
          nonceBytes,
        });
      } catch {
        throw new ConnectPathError("claim_failed");
      }
      const result = await callMcpTool({
        fetchFn,
        mcpUrl,
        bearer: mcpBearer,
        name: OAUTH_CONNECT_TOOL,
        toolArguments: signed.params,
        timeoutMs: MCP_REQUEST_TIMEOUT_MS,
      });
      const message = extractConnectMessage(result);
      // handled で返すとモデルは起動せず before_model_resolve も走らないため、この受信を
      // ここで消費する。残すと同じ DM の次の受信で bindAgentRun が candidates=2 で run を拒否し、
      // 以後 10 分間すべてのツールが「trusted Slack run identity is missing or stale」で
      // ブロックされる（レビュー実証 2026-09-03）。fallthrough 分岐では残す（モデル経路が束縛に使う）。
      removePending(ingress);
      for (const [boundRunId, bound] of ingressByRun) {
        if (bound === ingress) ingressByRun.delete(boundRunId);
      }
      emitPluginLog(
        logger,
        "info",
        `connect deterministic path invocation=${invocationId} ` +
          `outcome=answered tool_calls=1`,
      );
      return { handled: true, reply: { text: message } };
    } catch (error) {
      return fallthrough(connectPathReason(error));
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
      emitPluginLog(
        logger,
        "warn",
        `bind_agent_run rejected reason=incomplete missing=[${missing.join(",")}]${resolution}` +
          ` ${idShape({
            sender: ctx?.senderId,
            channel: ctx?.channelId,
            session: ctx?.sessionKey,
            team: ctx?.teamId,
            expectedTeam: expectedTeamId,
          })}`,
      );
      return;
    }
    const nowMs = now();
    pruneState(nowMs);
    if (rejectedRuns.has(runId)) {
      emitPluginLog(
        logger,
        "warn",
        "bind_agent_run rejected reason=already_rejected" +
          ` ${idShape({ sender: senderId, channel: channelId, session: sessionKey })}`,
      );
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
        emitPluginLog(
          logger,
          "warn",
          "bind_agent_run rejected reason=mismatched_repeat" +
            ` ${idShape({ sender: senderId, channel: channelId, session: sessionKey })}`,
        );
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
    const candidates = [...pendingByMessage.values()].filter(ingress =>
      matchesConversation(ingress, { sessionKey, senderId, channelId, nowMs }),
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
        // ⚠️ 会話 id の実値は出さない（2026-09-03 レビュー指摘・G7）。
        // かつてここは「会話 id は Slack のチャンネル/DM 識別子であって caller identity
        // ではないので出力してよい」として両側の実値を出していたが、**DM では成り立たない**:
        // resolveSlackChannel は DM を `DM:<senderId>` に解決するため、その実値は
        // Slack user id そのものになる（実証: `pendingChannelIds=[DM:U09CX1CCBLN]`）。
        // 本 PR で emitPluginLog が console へ必ず二重書きするようになり、上流の
        // ログレベル抑制も効かないので、ここは形と件数だけにする。
        const shapes =
          ch === 0
            ? [...new Set(pend.map(i => shapeOfChannel(i.channelId)))].sort().join(",")
            : "";
        const distinct = ch === 0 ? new Set(pend.map(i => i.channelId)).size : 0;
        mismatch =
          ` matchSessionKey=${sk} matchSenderId=${sd} matchChannelId=${ch} fresh=${fresh}` +
          (ch === 0
            ? ` runChannelShape=${shapeOfChannel(channelId)}` +
              ` pendingChannelShapes=[${shapes}] pendingChannelDistinct=${distinct}`
            : "");
      }
      emitPluginLog(
        logger,
        "warn",
        "bind_agent_run rejected reason=no_unique_binding" +
          ` candidates=${candidates.length} pending=${pendingByMessage.size}` +
          `${bindFailed ? " bindRunRefused=true" : ""}${mismatch}` +
          ` ${idShape({ sender: senderId, channel: channelId, session: sessionKey })}`,
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

  // block は必ずここを通す（2026-09-03）。1 回の呼び出しで
  //   ① 利用者向けの診断行つき blockReason を組み
  //   ② 管理者向けに 1 行ログを出す（コードと id_shape だけ・値は載せない）
  // ことを不可分にして、「拒否したのにログが 1 行も無い」状態を構造的に作れなくする。
  function blockAndLog(reason, code, logger, shape) {
    emitPluginLog(
      logger,
      "warn",
      `before_tool_call blocked diagnostic=${code} ${shape}`,
    );
    return { block: true, blockReason: formatBlockReason(reason, code, now(), adminName) };
  }

  function signToolCall(event, ctx, logger) {
    const observedToolName = nonBlank(event?.toolName, 256);
    const contextToolName = nonBlank(ctx?.toolName, 256);
    // 拒否ログに載せる「形」だけの手掛かり。値（user id / channel id / ts）は出さない。
    const shape = () =>
      idShape({
        sender: ctx?.senderId,
        channel: ctx?.channelId,
        session: ctx?.sessionKey,
        team: ctx?.teamId,
        expectedTeam: expectedTeamId,
      });
    if ([observedToolName, contextToolName].some(
      name => name && NATIVE_CALLER_BYPASS_TOOLS.has(name.toLowerCase()),
    )) {
      return blockAndLog(
        "native message, filesystem, and session tools are denied",
        BLOCK_DIAG.NATIVE_TOOL_DENIED,
        logger,
        shape(),
      );
    }
    const tool = canonicalToolName(observedToolName);
    if (tool === null) return undefined;
    if (ctx?.toolName !== event?.toolName) {
      return blockAndLog(
        "authoritative tool name binding is missing or mismatched",
        BLOCK_DIAG.TOOL_NAME_BINDING,
        logger,
        shape(),
      );
    }
    const eventRunId = canonicalInvocationId(event?.runId);
    const contextRunId = canonicalInvocationId(ctx?.runId);
    if (!eventRunId || !contextRunId || eventRunId !== contextRunId) {
      return blockAndLog(
        "authoritative run binding is missing or mismatched",
        BLOCK_DIAG.RUN_BINDING,
        logger,
        shape(),
      );
    }
    const eventToolCallId = canonicalInvocationId(event?.toolCallId);
    const contextToolCallId = canonicalInvocationId(ctx?.toolCallId);
    if (
      !eventToolCallId ||
      !contextToolCallId ||
      eventToolCallId !== contextToolCallId
    ) {
      return blockAndLog(
        "authoritative tool invocation binding is missing or mismatched",
        BLOCK_DIAG.INVOCATION_BINDING,
        logger,
        shape(),
      );
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
      return blockAndLog(
        "trusted Slack session or channel binding is missing",
        BLOCK_DIAG.SESSION_OR_CHANNEL_BINDING,
        logger,
        shape(),
      );
    }
    const nowMs = now();
    pruneState(nowMs);
    const trusted = ingressByRun.get(eventRunId);
    const trustedTtl =
      trusted?.ingressKind === "action"
        ? ACTION_CONTEXT_TTL_MS
        : INBOUND_CONTEXT_TTL_MS;
    if (!trusted || nowMs - trusted.receivedAtMs > trustedTtl) {
      // 実測 2026-09-03 の 9 件。bindAgentRun 側の拒否（run が束縛できなかった）が
      // ここに落ちてくるので、bindAgentRun の warn と時刻で突き合わせる。
      return blockAndLog(
        "trusted Slack run identity is missing or stale",
        BLOCK_DIAG.RUN_BINDING,
        logger,
        shape(),
      );
    }
    const trustedChannelMatches =
      trusted.channelId === channelId ||
      (Array.isArray(trusted.channelAliases) &&
        trusted.channelAliases.includes(channelId));
    if (!trusted.sessionKey || trusted.sessionKey !== sessionKey || !trustedChannelMatches) {
      return blockAndLog(
        "tool context does not match the bound Slack run",
        BLOCK_DIAG.SESSION_OR_CHANNEL_BINDING,
        logger,
        shape(),
      );
    }
    const exactInvocationKey = invocationKey(eventRunId, eventToolCallId);
    if (consumedInvocations.has(exactInvocationKey)) {
      return blockAndLog(
        "tool invocation replay rejected",
        BLOCK_DIAG.INVOCATION_BINDING,
        logger,
        shape(),
      );
    }
    if (trusted.ingressKind === "action") {
      if (tool !== MAIL_DRAFT_TOOL) {
        return blockAndLog(
          "Slack mail action cannot authorize another tool",
          BLOCK_DIAG.TOOL_NAME_BINDING,
          logger,
          shape(),
        );
      }
      if (trusted.actionToolCallId !== null) {
        return blockAndLog(
          "Slack mail action was already consumed",
          BLOCK_DIAG.INVOCATION_BINDING,
          logger,
          shape(),
        );
      }
    } else if (tool === MAIL_DRAFT_TOOL) {
      return blockAndLog(
        "mail_draft requires an authoritative Slack button action",
        BLOCK_DIAG.TOOL_NAME_BINDING,
        logger,
        shape(),
      );
    }
    let params;
    let declaredContext;
    try {
      // ── 二重包みの決定論 unwrap（引数検査より前）─────────────────────────
      // ここより下（assertPlainObject / validateDeclaredContext）が「引数検査」なので、
      // その手前で 1 度だけ正規化する。剥がせなければ無変更＝従来どおり block。
      // try の**内側**に置くこと（2026-09-03 レビュー指摘）: 外に出すと、万一 unwrap が
      // throw した場合に block へ変換されず上流へ委ねられ、fail-closed が破れる。
      // （JSON 由来の params では throw 不能だが、規律として例外も block に落とす）
      const unwrapped = unwrapToolArguments(event?.params, observedToolName);
      if (unwrapped.depth > 0) {
        // 識別子・本文・URL は載せない（G7）。形と段数だけ。
        emitPluginLog(
          logger,
          "warn",
          `unwrapped tool arguments (shape=${unwrapped.shape}, depth=${unwrapped.depth})`,
        );
      }
      const suppliedParams = assertPlainObject(unwrapped.params, "tool params");
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
      // 実測 2026-09-03 の 72 件（`_user_context must be a plain object`）はここ。
      // unwrap を通してもなお直らなかった場合だけが残る。
      return blockAndLog(
        error instanceof Error ? error.message : "invalid tool params",
        BLOCK_DIAG.USER_CONTEXT_SHAPE,
        logger,
        shape(),
      );
    }
    const declarationError = validateDeclaredContext(declaredContext, trusted);
    if (declarationError) {
      // 実測 2026-09-03 の 2 件（`declared channel_id does not match the bound ingress`）。
      return blockAndLog(
        declarationError,
        BLOCK_DIAG.SESSION_OR_CHANNEL_BINDING,
        logger,
        shape(),
      );
    }

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
      return blockAndLog(
        "secure nonce generation failed",
        BLOCK_DIAG.SIGNING_FAILED,
        logger,
        shape(),
      );
    }
    let signed;
    try {
      signed = mintCallerClaim({
        trusted,
        runId: eventRunId,
        toolCallId: eventToolCallId,
        tool,
        params,
        nowMs,
        nonceBytes,
      });
    } catch (error) {
      return blockAndLog(
        error instanceof Error ? error.message : "request binding failed",
        BLOCK_DIAG.SIGNING_FAILED,
        logger,
        shape(),
      );
    }
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
    return { params: signed.params };
  }

  // 署名 claim の鋳造。signToolCall（before_tool_call 経由）と層1（直接 tools/call）が
  // 同じ関数を使う＝mcp 側の検証契約（caller_claim.py）に対する発行元は 1 箇所のまま。
  // 例外は呼び出し側が block / fallthrough に変換する。
  function mintCallerClaim({ trusted, runId, toolCallId, tool, params, nowMs, nonceBytes }) {
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
    const argumentsSha256 = canonicalRequestSha256(adjustedParams);
    const issuedAt = Math.floor(nowMs / 1000);
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
      run_id: runId,
      tool_call_id: toolCallId,
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
    // (c) 介入条件は OR: 連携 URL を含む（#353）／利用者の最新メッセージが短い連携依頼（層2）。
    //     後者は run に束縛済みの ingress（権威的な受信）から読む。本文の推測はしない。
    const { kinds } = findFabricatedConnectUrlKinds(reply);
    const zeroToolConnect = ingressByRun.get(eventRunId)?.connectRequest === true;
    if (kinds.length === 0 && !zeroToolConnect) return undefined;
    const urlRule = kinds.length > 0;
    const describe = urlRule
      ? `connect_url_fabrication_blocked runId=${eventRunId} tool_calls=0 kinds=${kinds.join("+")}`
      : `connect zero-tool revise runId=${eventRunId} tool_calls=0 reason=short_connect_request`;
    // (d) 自前の予算（両ルール共有＝1 run につき再パスは 1 回）。上流予算に依存せず
    //     ループ不在を担保する。
    const revisions = connectRevisionsByRun.get(eventRunId)?.count ?? 0;
    if (revisions >= MAX_CONNECT_FABRICATION_REVISIONS) {
      logger?.warn?.(
        `${PLUGIN_ID}: ${describe} outcome=budget_exhausted revise_attempt=${revisions}`,
      );
      if (zeroToolConnect) {
        // 層3 を武装する: 再パス後も 0 tool call のまま終わった＝モデルが従わなかった。
        // 送信直前（reply_payload_sending）で本文を定型文へ置換する。
        const trusted = ingressByRun.get(eventRunId);
        connectFallbackByRun.delete(eventRunId);
        connectFallbackByRun.set(eventRunId, {
          senderId: trusted.senderId,
          replaced: false,
          updatedAtMs: nowMs,
        });
        logger?.warn?.(
          `${PLUGIN_ID}: connect zero-tool revise runId=${eventRunId} tool_calls=0 ` +
            `reason=model_did_not_call_tool outcome=fallback_armed diagnostic=${CONNECT_DIAGNOSTIC_CODE}`,
        );
      }
      return undefined;
    }
    // toolCallsByRun と同じ規律で delete->set する。現状 MAX_CONNECT_FABRICATION_REVISIONS
    // が 1 なので 1 run につき 1 度しか set されず既存キーの再 set は起きないが、
    // その値を 2 以上へ上げた瞬間に退避順が壊れる依存を残さない。
    connectRevisionsByRun.delete(eventRunId);
    connectRevisionsByRun.set(eventRunId, { count: revisions + 1, updatedAtMs: nowMs });
    // G7: 本文・URL 実体・Slack 識別子は載せない（捏造 URL には user_id が埋まっていた）。
    logger?.warn?.(`${PLUGIN_ID}: ${describe} outcome=revised revise_attempt=${revisions + 1}`);
    // URL 捏造は指示がより厳格（URL を書くな）なので、両方成立時は URL 側を優先する。
    return urlRule
      ? {
          action: "revise",
          reason: CONNECT_FABRICATION_REASON,
          retry: {
            instruction: CONNECT_FABRICATION_INSTRUCTION,
            idempotencyKey: CONNECT_FABRICATION_RETRY_KEY,
            maxAttempts: MAX_CONNECT_FABRICATION_REVISIONS,
          },
        }
      : {
          action: "revise",
          reason: CONNECT_ZERO_TOOL_REASON,
          retry: {
            instruction: CONNECT_ZERO_TOOL_INSTRUCTION,
            idempotencyKey: CONNECT_ZERO_TOOL_RETRY_KEY,
            maxAttempts: MAX_CONNECT_FABRICATION_REVISIONS,
          },
        };
  }

  // 層3。層2 の再パス後も 0 tool call のまま終わった run の最終応答を、送信直前に
  // 定型文へ置換する。event.runId と ctx.runId は agent run と同じ id
  // （dispatch:2528-2545 が runState.runId を両方に載せる）。食い違えば触らない。
  // 同一 run の 2 通目以降（分割 payload）は、置換済みの定型文と重複するので取り消す。
  function replaceExhaustedConnectReply(event, ctx, logger) {
    const eventRunId = canonicalInvocationId(event?.runId);
    const contextRunId = canonicalInvocationId(ctx?.runId);
    if (!eventRunId || !contextRunId || eventRunId !== contextRunId) return undefined;
    const entry = connectFallbackByRun.get(eventRunId);
    if (!entry) return undefined;
    const payload = event?.payload;
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return undefined;
    const text = typeof payload.text === "string" ? payload.text.trim() : "";
    if (!text) return undefined;
    const nowMs = now();
    if (entry.replaced) {
      return { cancel: true, reason: CONNECT_FALLBACK_CANCEL_REASON };
    }
    entry.replaced = true;
    entry.updatedAtMs = nowMs;
    logger?.warn?.(
      `${PLUGIN_ID}: connect zero-tool fallback runId=${eventRunId} outcome=replaced ` +
        `diagnostic=${CONNECT_DIAGNOSTIC_CODE}`,
    );
    return {
      payload: {
        ...payload,
        text: buildConnectFallbackText({ senderId: entry.senderId, nowMs }),
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
      api.on("before_agent_reply", (event, ctx) =>
        answerShortConnectRequest(event, ctx, api.logger),
      );
      api.on("before_model_resolve", (event, ctx) => {
        bindAgentRun(event, ctx, api.logger);
      });
      // logger を渡していなかったのが「14 日間 warn が 1 行も出ない」原因だった（2026-09-03）。
      api.on("before_tool_call", (event, ctx) => signToolCall(event, ctx, api.logger));
      api.on("before_agent_finalize", (event, ctx) =>
        guardConnectUrlFabrication(event, ctx, api.logger),
      );
      api.on("reply_payload_sending", (event, ctx) =>
        replaceExhaustedConnectReply(event, ctx, api.logger),
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
