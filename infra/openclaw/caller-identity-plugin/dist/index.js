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
const SLACK_INTERACTION_EVENT_PREFIX = "Slack interaction: ";
// 上流が system event（heartbeat の prompt に載る "Slack interaction: {...}"）の文字列値を
// 切り詰める長さ。超えた値は「先頭 159 字＋…」になる（openclaw@v2026.7.1
// extensions/slack/src/monitor/events/interactions.ts:15,36-37 / extensions/slack/src/truncate.ts）。
// interactive handler 側（押下の捕捉）には切られていない値が届く（interactions.block-actions.ts:658）。
const SLACK_INTERACTION_VALUE_MAX_LENGTH = 160;
const SLACK_INTERACTION_VALUE_ELLIPSIS = "…";

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
//        ⚠️ reply_payload_sending は **agent_end の後** に走る（本番実測 2026-09-04 17:11 JST・
//        上流実物 selection-8ixiqbew.js:14591 / dispatch-V82RCNJs.js:1994-1996,1716,2533）。
//        ここで読む台帳（connectFallbackByRun / connectIngressByRun）は agent_end で消さない。
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
// 保証経路が既に同じ内容を配信したターンで、モデル側の最終応答を落とすときの理由。
const CONNECT_GUARANTEE_CANCEL_REASON = "connect guarantee already delivered this inbound";

// ── 動画 URL × 0 tool call の層2（2026-09-25）────────────────────────────────
// 本番実測 2026-09-24〜25: DM「この動画を分析して <YouTube URL>」に Aico がツールを一度も
// 呼ばず「YouTube は取得不可です。TikTok / Instagram の動画か、ファイル添付でお願いします。」と
// 4 回返した。SOUL（#441/#445）とツール説明（#444）は本番に反映済みで、履歴の無いセッション
// （/new 後）では video_analysis が呼ばれて分析結果が返った。断り続けた原因は同じ DM セッションの
// 履歴（初回の誤った断りと「今後は即座にお断りします」という自分の約束）で、SOUL の文言では
// 上書きできなかった＝連携（2026-09-03）と同じ失敗クラス。連携と同じ作りの層2 だけを置く。
//   層1（モデルを通さず呼ぶ）は置かない: 分析か切り出しか、引数（focus / timecodes）をモデルが決める。
//   層3（定型文へ置換）は置かない: 定型文では分析結果を出せない（再パスでも断るならそのまま届く）。
// 判定は受信時に URL の「種類」と依頼語の有無（真偽）だけを ingress に載せる（URL・本文は保持しない＝G7）。
// 誤爆を避けるため、動画 URL があっても「依頼の語がある」か「下書きが断りの形」のときだけ介入する
// （URL を共有しただけの会話に再パスを掛けて、頼んでいない分析＝Gemini 課金・月の利用枠を誘わない）。
const VIDEO_URL_SCAN_LIMIT = 2048;
// 種類ごとの検出規則。Slack は URL を `<https://…|label>` で包んで届ける（本番実測）ので、
// 区切りに `<` `>` `|` を含めない。動画でない URL（YouTube のトップ・TikTok のプロフィールや
// 広告管理画面など）は拾わない。
const VIDEO_URL_RULES = [
  ["youtube", /https?:\/\/(?:www\.|m\.|music\.)?youtube\.com\/(?:watch\?|(?:shorts|live)\/[^\s<>|])/iu],
  ["youtube", /https?:\/\/youtu\.be\/[^\s<>|]/iu],
  ["tiktok", /https?:\/\/(?:www\.|m\.)?tiktok\.com\/(?:@[^\s<>|/]+\/(?:video|photo)\/|[tv]\/)[^\s<>|]/iu],
  ["tiktok", /https?:\/\/(?:vt|vm)\.tiktok\.com\/[^\s<>|]/iu],
  ["instagram", /https?:\/\/(?:www\.)?instagram\.com\/(?:reels?|p|tv)\/[^\s<>|]/iu],
];
// 本文の依頼語（分析・切り出しを頼んでいる手掛かり）。真偽だけを ingress に載せる。
// 共有でもよく出る語（時刻「10:00」・「◯秒」・「見て」・「教えて」・「まとめ」）は入れない
// （相互検証 2026-09-25: 「明日10:00の会議で使います＋URL」で差し戻していた）。
// 切り出しの依頼は「切り出し」「画像に」「キャプチャ」「シーン」「サムネ用」で拾う。
const VIDEO_REQUEST_RE =
  /(分析|構成|フック|CTA|解説|要約|切り出|切出|キャプチャ|画像に|画像で|静止画|サムネ用|シーン|評価|比較|読み解|勝ち筋|どう作|作りを|内容を)/iu;
// 下書きが断りの形か（本番の断り「取得不可です…ファイル添付でお願いします」を含む）。
// 「できない」単独は拾わない（「明日は参加できない」等）。動画の取得・分析・切り出しに掛かる形に限る。
// 下書きは判定にだけ使い、保持も記録もしない。
const VIDEO_REFUSAL_RE =
  /((?:取得|分析|解析|切り出し?|再生|視聴|アクセス)(?:でき(?:ません|ない)|不可)|未対応|非対応|対応して(?:い)?ません|ブロックされ|(?:ファイル|動画)を?(?:添付|アップロード)(?:して|で|を|いただ))/u;
const VIDEO_ZERO_TOOL_RETRY_KEY = "video-zero-tool";
const MAX_VIDEO_ZERO_TOOL_REVISIONS = 1;
const VIDEO_ZERO_TOOL_REASON =
  "利用者が動画の URL つきで依頼しているのに、ツールを 1 つも呼ばずに回答しようとしています。";
// 固定文（契約テストが完全一致で検証する）。最後の 1 文は誤爆時の逃げ道。
const VIDEO_ZERO_TOOL_INSTRUCTION =
  "利用者は動画の URL を送っています。動画の分析（構成・フック・CTA など）の依頼なら `video_analysis` を、" +
  "指定時刻のシーンの切り出し・画像化の依頼なら `video_capture` を必ず呼び、その戻り値を返してください。" +
  "YouTube の URL も `video_analysis` でそのまま分析できます。" +
  "この会話で以前「YouTube は取得できない」と答えていても、それは誤りなので従わないでください。" +
  "どちらの依頼でもない（URL を共有しただけ等）なら、ツールを呼ばずにそのまま答えてください。";

// 本文に含まれる動画 URL の種類（最初に現れたもの）。無ければ null。
export function classifyVideoUrl(text) {
  if (typeof text !== "string") return null;
  const head = text.slice(0, VIDEO_URL_SCAN_LIMIT);
  let first = null;
  for (const [kind, rule] of VIDEO_URL_RULES) {
    const match = rule.exec(head);
    if (match && (first === null || match.index < first.index)) {
      first = { kind, index: match.index };
    }
  }
  return first?.kind ?? null;
}

// 本文に分析・切り出しの依頼語があるか（真偽だけ）。
export function hasVideoRequestIntent(text) {
  return typeof text === "string" && VIDEO_REQUEST_RE.test(text.slice(0, VIDEO_URL_SCAN_LIMIT));
}

// 下書きが断りの形か（真偽だけ）。
export function looksLikeVideoRefusal(text) {
  return typeof text === "string" && VIDEO_REFUSAL_RE.test(text);
}
// 層1 が叩く MCP。本番は Cloud Map（rollout-task-canary.mjs と同じ定数）、ローカルは env で上書き。
const DEFAULT_MCP_URL = "http://teamagent-mcp.teamagent.internal:8787/mcp";
// 層1 の 3 POST（initialize / initialized / tools/call）で共有する全体予算。
// claim TTL（60s）と同長にしない: 超過は fallthrough でモデル経路へ渡す。
const MCP_REQUEST_TIMEOUT_MS = 15_000;
const MCP_PROTOCOL_VERSION = "2025-03-26";
const MCP_CLIENT_NAME = "teamagent-caller-identity-connect";
const CONNECT_L1_INVOCATION_PREFIX = "connect-l1";
// ── ボタン押下の直接実行（executeButtonAction）の定数 ─────────────────────────────
// 上流は押下を受けた時点で Slack へ ack してから plugin の handler を呼ぶ
// （openclaw@v2026.7.1 extensions/slack/src/monitor/events/interactions.block-actions.ts:905-906）。
// よって 3 秒制約は上流が満たし、handler は捕捉だけして即 {handled:true} を返す。
// mcp 呼び出しと投稿は handler から切り離して走らせる（保証経路と同じ onBackgroundTask）。
// 予算は層1（15s）より長くとる: ✏️ は下書き本文を LLM で書く（mail_draft → generate_draft_for_thread）。
// claim の TTL（60s）は mcp が受信時に検証するので、ツールの実行が長くても失効しない。
const BUTTON_MCP_TIMEOUT_MS = 120_000;
// 直接実行した押下の台帳（buttonPressLedger）の保持期間。ボタンの value（mcp の HMAC トークン）の
// 最長の寿命（24h・MAIL_ACTION_TTL_S の上限）より長くとる（2026-09-29 レビュー指摘）。
// 以前は署名経路と同じ seenActions（10 分）に置いていたため、10 分を過ぎて押し直すと plugin は通し、
// mcp の one-use nonce（押下の指紋から決まる固定値・DynamoDB の TTL 削除は数時間〜数日遅れる）が
// 「再生」として拒否し、それを失敗として本人へ伝えて自由文での頼み直し（＝別 id での二重登録）を招いていた。
// 押下の台帳は mcp の nonce より先に切れてはならない。
const BUTTON_PRESS_LEDGER_TTL_MS = 24 * 60 * 60 * 1000 + INBOUND_CONTEXT_TTL_MS;
// 台帳の上限。超えたら古いものから捨てる（署名経路を落とす MAX_TRACKED_CONTEXTS の fail には
// 相乗りさせない）。捨てた押下の押し直しは mcp の nonce が止め、文面は texts.unknown になる。
const MAX_BUTTON_PRESS_LEDGER = 5000;
// 押した本人だけに見える一時表示（上流の ctx.respond.reply＝Slack の response_url）の待ち上限。
const BUTTON_EPHEMERAL_TIMEOUT_MS = 10_000;
const BUTTON_INVOCATION_PREFIX = "slack-action";
const MCP_BUTTON_CLIENT_NAME = "teamagent-caller-identity-button";
// claim の session_sha256 の元（押下 1 件ごと）。heartbeat run のセッションが無い直接実行でも
// mcp の claim 契約（sha256 必須・caller_claim.py）を満たす。mcp は形だけを検証し、認可には使わない。
const BUTTON_SESSION_PREFIX = "teamagent-slack-action-v1";
const SLACK_CANONICAL_CHANNEL_RE = /^[CDG][A-Z0-9]{8,}$/u;
const SLACK_DM_CHANNEL_RE = /^D[A-Z0-9]{8,}$/u;

// ── (D) 保証経路: 「連携」と言われたら必ず何かが届く ────────────────────────
// 目的（2026-09-04 のゴール）: 新規／既存／過去のテストユーザーを問わず、「連携して」と
// 言ったら漏れなく連携リンク（または「次に何をすればよいか分かる案内」）が届くこと。
//
// なぜ message_received に載せるのか（上流実物での比較・§12 に file:line）:
//   - `message_received` は非 conversation hook。非 bundled plugin でも
//     `hooks.allowConversationAccess` の可否に関係なく登録される
//     （registry-D1_pYg_a.js:4224-4235 の門は CONVERSATION_HOOK_NAMES だけに掛かる）。
//     本番で確実に呼ばれていることは、署名済み claim を mcp が受理している事実
//     （= before_tool_call → signToolCall が動く = その前提の ingress 記録が動く）で実証済み。
//   - `before_agent_reply`（層1）は conversation hook。設定が 1 つ欠けるだけで
//     **診断も出ないまま黙って登録が捨てられる**。保証の土台には使えない。
//   - `message_received` は fire-and-forget（dispatch-V82RCNJs.js:1438）で、
//     void hook の既定タイムアウトも無い（hook-runner-global:248-253）ので、
//     ここで MCP と Slack を叩いても上流の応答経路を遅らせない。
//
// 配信手段に Slack Web API（chat.postMessage）を直接使う理由:
//   上流の送信面 `api.runtime.channel.outbound.loadAdapter` / `reply.dispatch*`
//   （types-DaHgOqFX.d.ts:8228-8352）は gateway の request context と account 解決に
//   依存し、hook から単独で正しく駆動する契約が公開されていない。対して bot token は
//   entrypoint の REQUIRED_SECRETS で確実に子プロセスへ渡っており
//   （openclaw-entrypoint.mjs:15-21,184）、この plugin は既に MCP へ生 fetch している。
//   「確実に届く」ことを最優先し、依存の少ない方を選ぶ。
const SLACK_API_BASE = "https://slack.com/api";
const SLACK_API_TIMEOUT_MS = 10_000;
// 一時失敗の再試行（保証の唯一の配信面なので、429/5xx で無音にしない）。
const SLACK_MAX_RETRIES = 2;
const SLACK_RETRY_BACKOFF_MS = 500;
const SLACK_DEFAULT_RETRY_AFTER_MS = 1_000;
// Slack が返す Retry-After を鵜呑みにして何分も待たない（保証の遅延に上限を置く）。
const SLACK_MAX_RETRY_AFTER_SECONDS = 5;
// 保証経路の 1 回性キー。同じ受信に対して 2 回投稿しない。
const CONNECT_GUARANTEE_INVOCATION_PREFIX = "connect-d1";
// 保証経路が MCP にも Slack にも届かなかったときの最終文面。無言終了を作らない。
const CONNECT_GUARANTEE_DIAGNOSTIC_CODE = "CONNECT-Z02";
// 本番でどのフックを登録要求するかの**期待値**（単一正本）。
// register() は実際に api.on した名前でバナーを出し、両者が食い違ったら起動時に fail する
// （下の register 末尾）。定数とコードが黙って乖離しないようにするための二重化。
export const REGISTERED_HOOKS = Object.freeze([
  "inbound_claim",
  "message_received",
  "before_agent_reply",
  "before_model_resolve",
  "before_tool_call",
  "before_agent_finalize",
  "reply_payload_sending",
  "agent_end",
]);

const SLACK_USER_RE = /^U[A-Z0-9]{8,}$/u;
const SLACK_TEAM_RE = /^T[A-Z0-9]{8,}$/u;
const SLACK_CHANNEL_RE = /^[CDG][A-Z0-9]{8,}$/u;
const SLACK_TS_RE = /^[0-9]{10,}\.[0-9]{6}$/u;
// ボタンの value（mcp の HMAC 署名トークン）の形: base64url(payload) "." base64url(16 バイト署名)。
// 名前は歴史的経緯（mail_draft 専用だった）で、4 種のボタンすべてがこの形。
const DRAFT_TOKEN_RE = /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{22}$/u;
// system event で切り詰められた value の本体（159 字）。署名の途中で切れることがある。
const TRUNCATED_ACTION_TOKEN_RE = /^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*)?$/u;
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

// ── ボタン束縛表 ACTION_BINDINGS（2026-09-29・裁定「ボタンは表示したまま改修を急ぐ」）──────
// 朝ダイジェストのボタン（Slack の action_id）ごとに、押下 1 回で呼べるツールを 1 対 1 で固定する。
// 以前は interactive handler を namespace `mail_draft` にだけ登録し、ボタン由来の run で呼べる
// ツールも mail_draft に固定していたため、📅 calendar_event・🗓 schedule_propose は表示されるのに
// 押しても何も起きなかった（本番 30 日でボタン経由の実行 0 件）。
//   tool          … その押下の run で署名してよい唯一のツール（action_id と同名）
//   tokenParam    … 押下時に捕捉した value（mcp の HMAC 署名トークン）で上書きする引数名。
//                   モデルが渡した値は使わない（system event の value は切れていることがある）。
//   maxLength     … value の上限＝各ツールの入力 schema の max_length
//                   （mail_draft だけは従来どおり 160 のまま＝弱めない）
//   tokenTypes    … value の payload（v2）の typ。null は形（DRAFT_TOKEN_RE）だけを見る（mail_draft の従来どおり）。
//                   🗓 は ✏️ と同じ draft トークンを value に使う（run_morning_digest_fargate.py の _reply_buttons）。
//   outsideAction … メッセージ由来の run でこのツールが呼ばれたとき
//                   "deny"        署名しない（ボタン専用ツール・mail_draft の従来どおり）
//                   "blank_token" 署名するが tokenParam を "" に上書きする
//                                 （calendar_event は自由文の登録入口を持つ。自由文は今までどおり通す）
// mcp 側の検証（HMAC・purpose・本人・期限）はそのまま残る＝plugin と mcp の二重の守り。
// digest_ack は本番 OFF（mcp の tools/list に出ない）。押下を束縛しても、その run で呼べるのは
// digest_ack だけなので、ツールが無ければ何も実行されない（他のツールでの代用は止める）。
//
// ── 押下の直接実行（2026-09-29 裁定「AI を通さず直接処理する」）の返し方 ──────────────
// 本番は heartbeat.every="0m" で押下の後に AI の run が起きない（上流の heartbeat-runner が
// interval 0 の agent を載せない）。そこで押下を捕捉した plugin が束縛先の 1 ツールを mcp へ
// 直接呼び、結果を押した本人の DM へ投稿する（下の executeButtonAction）。その文面の決まり:
//   resultLink … 出力のリンク欄と表示名。URL は生のまま出さず <url|表示名> にする
//                （SOUL のボタン節の例「<event_url|カレンダーで開く>」と同じ）。
//   undoToken  … 出力の取り消し用トークン欄（digest_ack だけ）。同じ action_id のボタンにして添える
//                （slack_bot.py の EC2 経路と同じ「押下直後の取り消し導線」）。
//   texts.retry   … mcp へツールを渡す前に失敗（mcp の one-use nonce も未消費）。
//                   同じボタンをもう一度押せるようにする（押下の台帳から外す）。
//   texts.failed  … mcp が利用者向けの文（message）の無い失敗を返した。同じ押下はもう実行されない
//                   （mcp が nonce を消費済みでありうる）ので、別の頼み方を案内する。
//   texts.unknown … ツールを渡した後に応答が途切れた（実行されたか分からない）か、mcp が
//                   CALLER_IDENTITY_REJECTED を返した（one-use nonce の再生＝すでに実行済みの押下と、
//                   本人を確かめられない拒否を mcp の応答からは見分けられない）。確認を促し、
//                   頼み直しは「入っていなければ」に限る（二重登録を招かない）。
//   pendingText … 実行を始めたときに押した本人だけへ一時表示する 1 行（上流の ctx.respond.reply＝
//                 Slack の response_url・ephemeral）。結果まで数秒〜数十秒かかるもの（✏️ は下書きを
//                 LLM で書く・🗓 は空き枠と仮予定を作る）だけに付け、画面が何も変わらないまま
//                 押し直しや自由文の頼み直しに流れないようにする。null は出さない（📅・☑️ はすぐ返る）。
// ツールが利用者向けの文（message）を返したときは、成功・失敗を問わずその文をそのまま出す。
export const ACTION_BINDINGS = Object.freeze({
  mail_draft: Object.freeze({
    tool: "mail_draft",
    tokenParam: "draft_token",
    maxLength: SLACK_INTERACTION_VALUE_MAX_LENGTH,
    tokenTypes: null,
    outsideAction: "deny",
    resultLink: Object.freeze({field: "open_url", label: "Gmailで開く"}),
    undoToken: null,
    pendingText:
      "✏️ 返信下書きを作っています。できたらこの DM でお知らせします（数十秒かかることがあります）。",
    texts: Object.freeze({
      retry: "返信下書きを作れませんでした。もう一度押してください。",
      failed:
        "返信下書きを作れませんでした。お手数ですが『（件名）の返信下書きを作って』と送ってください。",
      unknown: "返信下書きを作れたか確認できませんでした。Gmail の下書きをご確認ください。",
    }),
  }),
  calendar_event: Object.freeze({
    tool: "calendar_event",
    tokenParam: "event_token",
    maxLength: 500,
    tokenTypes: Object.freeze(["event"]),
    outsideAction: "blank_token",
    resultLink: Object.freeze({field: "event_url", label: "カレンダーで開く"}),
    undoToken: null,
    pendingText: null,
    texts: Object.freeze({
      retry: "予定の登録に失敗しました。もう一度押すか、『予定入れといて』と送ってください。",
      failed: "予定の登録に失敗しました。『予定入れといて』と送ってください。",
      unknown:
        "予定を登録できたか確認できませんでした。カレンダーに入っていなければ『予定入れといて』と送ってください。",
    }),
  }),
  schedule_propose: Object.freeze({
    tool: "schedule_propose",
    tokenParam: "schedule_token",
    maxLength: 400,
    tokenTypes: Object.freeze(["draft"]),
    outsideAction: "deny",
    resultLink: Object.freeze({field: "open_url", label: "Gmailで開く"}),
    undoToken: null,
    pendingText: "🗓 日程候補の下書きを作っています。できたらこの DM でお知らせします。",
    texts: Object.freeze({
      retry: "日程候補の下書きを作れませんでした。もう一度押してください。",
      failed: "日程候補の下書きを作れませんでした。お手数ですが Gmail から直接ご返信ください。",
      unknown: "日程候補の下書きを作れたか確認できませんでした。Gmail の下書きをご確認ください。",
    }),
  }),
  digest_ack: Object.freeze({
    tool: "digest_ack",
    tokenParam: "ack_token",
    maxLength: 2000,
    tokenTypes: Object.freeze(["ack", "ackall", "unack"]),
    outsideAction: "deny",
    resultLink: null,
    undoToken: Object.freeze({field: "undo_token", tokenType: "unack", label: "↩︎ 取り消す"}),
    pendingText: null,
    texts: Object.freeze({
      retry: "確認済みにできませんでした。もう一度押してください。",
      failed: "確認済みにできませんでした。次回の朝ダイジェストでもう一度お試しください。",
      unknown: "確認済みにできたか確認できませんでした。次回の朝ダイジェストでご確認ください。",
    }),
  }),
});
// 直接実行の共通の定型文（ツール名・コード・URL・トークンは含めない）。
// ツールが mcp に無い（digest_ack は本番 OFF）ときの 1 行。SOUL のボタン共通プロトコルと同じ文。
export const BUTTON_UNAVAILABLE_TEXT = "このボタンはいま使えません。";
// DM 以外（チャンネル・グループ・本人以外の DM）で押されたとき、押した本人の DM へ送る案内。
export const BUTTON_DM_ONLY_TEXT =
  "このボタンは Aico との DM に届いた朝ダイジェストでだけ使えます。DM のダイジェストから押してください。";
// 値の形が合わない押下（古いダイジェストの長すぎるトークン・別種のトークン等）への案内。
export const BUTTON_STALE_TEXT =
  "このボタンは使えなくなっています。最新の朝ダイジェストから押してください。";
// 上流の namespace 規則（openclaw@v2026.7.1 src/plugins/interactive-shared.ts:13-20）と、
// data = `${actionId}:${value}` の最初の ":" で namespace を切る規則（同 :30-35）に合わせる。
const SLACK_ACTION_NAMESPACE_RE = /^[a-z][a-z0-9_]{0,63}$/u;
const TOKEN_PARAM_RE = /^[a-z][a-z0-9_]{0,63}$/u;
const BUTTON_OUTSIDE_ACTION_POLICIES = new Set(["deny", "blank_token"]);
const BUTTON_TEXT_KINDS = ["retry", "failed", "unknown"];

function isButtonText(value) {
  return typeof value === "string" && value.trim() !== "" && value.length <= 200;
}

// 束縛表の自己検査（起動時に落とす）。1 対 1（ツールの重複なし）であること。
const ACTION_BINDING_BY_TOOL = (() => {
  const byTool = new Map();
  for (const [actionId, binding] of Object.entries(ACTION_BINDINGS)) {
    if (
      !SLACK_ACTION_NAMESPACE_RE.test(actionId) ||
      typeof binding?.tool !== "string" ||
      !TOOL_RE.test(binding.tool) ||
      byTool.has(binding.tool) ||
      !TOKEN_PARAM_RE.test(binding.tokenParam) ||
      !Number.isSafeInteger(binding.maxLength) ||
      binding.maxLength < SLACK_INTERACTION_VALUE_MAX_LENGTH ||
      !(
        binding.tokenTypes === null ||
        (Array.isArray(binding.tokenTypes) && binding.tokenTypes.length > 0)
      ) ||
      !BUTTON_OUTSIDE_ACTION_POLICIES.has(binding.outsideAction) ||
      !(
        binding.resultLink === null ||
        (TOKEN_PARAM_RE.test(binding.resultLink?.field) && isButtonText(binding.resultLink?.label))
      ) ||
      !(
        binding.undoToken === null ||
        (TOKEN_PARAM_RE.test(binding.undoToken?.field) &&
          Array.isArray(binding.tokenTypes) &&
          binding.tokenTypes.includes(binding.undoToken?.tokenType) &&
          isButtonText(binding.undoToken?.label))
      ) ||
      !(binding.pendingText === null || isButtonText(binding.pendingText)) ||
      !BUTTON_TEXT_KINDS.every(kind => isButtonText(binding.texts?.[kind]))
    ) {
      fail(`ACTION_BINDINGS entry is invalid or not one-to-one: ${actionId}`);
    }
    byTool.set(binding.tool, Object.freeze({actionId, ...binding}));
  }
  return byTool;
})();

// 押下の action_id に対応する束縛（プロトタイプ由来のキーは拾わない）。
function actionBindingFor(actionId) {
  return typeof actionId === "string" && Object.hasOwn(ACTION_BINDINGS, actionId)
    ? ACTION_BINDINGS[actionId]
    : null;
}

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

// モデルが `_user_context.slack_user_id` に書いてくる**申告値**の正規化。
// 本人の ID を指しているのに形だけ違う書き方（メンション表記 `<@U…>` /
// `<@U…|name>`）を、素の ID へ寄せてから `normalizeSlackId` に渡す。
// 解釈できなければ null（＝呼び出し側で「破棄して続行」）。
const SLACK_MENTION_RE = /^<@([^>|]+)(?:\|[^>]*)?>$/u;
function normalizeDeclaredSlackUserId(value) {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  const mention = SLACK_MENTION_RE.exec(trimmed);
  return normalizeSlackId(mention ? mention[1] : trimmed, SLACK_USER_RE);
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

// ── 利用者向けの正しい 1 行（2026-09-11 実測の誤誘導対策）───────────────────
// 本番実測 2026-09-11 10:27: P06 の block 文の 1 行目が英語の技術理由
// （`_user_context must be a plain object`）だったため、モデルがそれを読み解けず
// 「Google 連携をリセットすることで解決する可能性があります」「『連携』と返して
// いただければリセットリンクをお出しします」と**自分で原因を作文**し、
// 利用者を無意味な操作へ誘導した（連携は成立しており、リセットは無関係）。
//
// 直し方は 2 つ同時に:
//   (1) ここ: block 文の 1 行目を **日本語の正しい 1 行**にし、その直後に
//       「推測して別の操作を勧めるな」というモデル宛の 1 行を必ず差し込む。
//       （python 側 connect_diagnostics.format_user_message の user_action と同じ流儀）
//   (2) SOUL: 「診断: 行つきのブロック文」全般へ規則の適用範囲を広げる
//       （従来は oauth_connect / CALLER_IDENTITY_REJECTED だけを名指ししていたため、
//        plugin の before_tool_call block はどの規則にも当たらなかった）。
// コードごとの文面は「利用者が取れる行動」だけを書く。原因の説明はしない
// （原因は診断コードで管理者が runbook を引く）。
const USER_ACTION_BY_CODE = Object.freeze({
  [BLOCK_DIAG.NATIVE_TOOL_DENIED]:
    "このツールはこの環境では使えません。依頼の内容を変えてもう一度お試しください。",
  [BLOCK_DIAG.TOOL_NAME_BINDING]:
    "この操作は受け付けられませんでした。もう一度同じ依頼を送ってください。",
  [BLOCK_DIAG.RUN_BINDING]:
    "受付の有効時間が切れました。お手数ですが、もう一度同じ依頼を送ってください。",
  [BLOCK_DIAG.INVOCATION_BINDING]:
    "この操作は受け付けられませんでした。もう一度同じ依頼を送ってください。",
  [BLOCK_DIAG.SESSION_OR_CHANNEL_BINDING]:
    "この操作は受け付けられませんでした。もう一度同じ依頼を送ってください。",
  [BLOCK_DIAG.USER_CONTEXT_SHAPE]:
    "この操作は受け付けられませんでした。もう一度同じ依頼を送ってください。",
  [BLOCK_DIAG.SIGNING_FAILED]:
    "利用者側の操作では直りません。管理者のサポートが必要です。",
});

// モデル宛の 1 行。利用者へはこの行自体を見せない。
// ── 行そのものを識別可能にする（2026-09-11 レビュー指摘）────────────────────
// 従来この行は接頭辞が無く、SOUL の「提示せよ」（1 行目・転送定型文・診断行）にも
// 「出すな」（`teamagent-caller-identity:` 付きの行）にも当たらなかった。拒否理由を
// 一字も変えず出すと、利用者の画面に「連携のリセット・再ログイン・ブラウザ変更」の
// 語がそのまま並ぶ＝この PR が潰した誤誘導が別の入口で再現していた。
// SOUL の記述に頼らず**行自体で判別できる**よう、他の管理者向け行と同じ接頭辞を付け、
// 既存の「接頭辞付きの行は利用者に出さない」規則で自動的に除外されるようにする。
// 接頭辞付きでも指示には従うことは SOUL 側（「診断:」行の節）で明記している。
const BLOCK_MODEL_INSTRUCTION =
  "この案内と診断行をそのまま利用者へ提示してください。原因を推測して" +
  "連携のリセット・再ログイン・ブラウザ変更などの別の操作を勧めてはいけません。";

export function userActionForBlockCode(code) {
  return (
    USER_ACTION_BY_CODE[code] ??
    "この操作は受け付けられませんでした。もう一度同じ依頼を送ってください。"
  );
}

// 利用者に届く block 文。
//   1 行目 利用者向けの正しい案内（日本語・行動だけ）
//   2 行目 モデル宛の禁止事項（推測して別の操作を勧めない）※接頭辞つき＝利用者に出さない
//   3 行目 転送の定型文
//   4 行目 診断行
//   5 行目 技術理由（管理者・モデルの切り分け用。利用者向けではない）※接頭辞つき
// user id・本文・URL は載せない（G7）。管理者は runId ではなくコード＋時刻で突合する。
export function formatBlockReason(reason, code, nowMs, adminName = DEFAULT_ADMIN_NAME) {
  // `fail()` 由来の理由は既に `teamagent-caller-identity: ` が付いている。
  // 本番実測 2026-09-11 の block 文は接頭辞が 2 回並んでいた（利用者に見えていた）。
  const detail = String(reason).startsWith(`${PLUGIN_ID}: `)
    ? String(reason).slice(`${PLUGIN_ID}: `.length)
    : String(reason);
  return (
    `${userActionForBlockCode(code)}\n` +
    `${PLUGIN_ID}: ${BLOCK_MODEL_INSTRUCTION}\n` +
    `${adminForwardHint(adminName)}\n` +
    `診断: ${code} ${formatJstMinute(nowMs)}\n` +
    `${PLUGIN_ID}: ${detail}`
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
  // console 側は **常に stderr**（level に関わらず console.warn）。
  //
  // 理由（2026-09-04 実証）: このプラグインが読み込まれる node プロセスでは、
  // stdout が「データ面」であることがある。tests/test_mcp_gateway_caller_claim.py の
  // ハーネスは `node -e` で plugin を読み込み、`process.stdout.write(JSON.stringify(...))`
  // の結果を `json.loads` する。診断行を stdout に混ぜると **その JSON が壊れる**。
  // 実際、register バナーを console.info（= stdout）で出した瞬間に 3 本が赤くなった。
  //
  // 診断は stderr、データは stdout ——この分離は破らない。
  // CloudWatch は stdout/stderr を同じロググループへ入れるので、到達性は変わらない
  // （上流 gateway 実機で確認済み: §12-1 の表）。level の意味は logger 側で保つ。
  console.warn(line);
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
  //
  // `stillWrapped`（2026-09-11 追加）: 「包みでなかった」と「上限まで剥がしてもまだ包み」を
  // 呼び出し側が区別できるようにする。従来は両方 depth:0 で返しており、
  // 「まだ包み」は `_user_context` が見つからないことを経由して結果的に block されていた。
  // 本 PR で `_user_context` の欠落を block しなくなるため、その間接的な fail-closed が
  // 消える。3 段以上は**明示的に**block する（＝入力面を任意の深さへ広げない）。
  if (kinds.length === 0) {
    return { params, depth: 0, shape: null, stillWrapped: false };
  }
  if (wrapperOf(current) !== null) {
    return { params, depth: 0, shape: null, stillWrapped: true };
  }
  return {
    params: current,
    depth: kinds.length,
    shape: [...new Set(kinds)].join("+"),
    stillWrapped: false,
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

// ── Slack が機械的に付ける定型注記の除去（2026-09-04 本番実測）─────────────
// 本番 OC TD:45 の実測ログ: 利用者が「連携」（2 文字）と送ったのに、プラグインには
// `content_len=16 / normalized_len=16` で届き、12 文字上限に当たって
// `not_connect_request` で落ちていた。Slack 側が本文へ定型の注記を混ぜるため。
//
// ⚠️ normalized_len == content_len == 16（正規化で 1 文字も減っていない）という事実から、
// その 16 文字には Slack マークアップ（`<@U…>` 等）も絵文字も端の約物も**無い**ことが判る。
// つまり素のテキストが混ざっている。中身はログに出せない（G7）ので、
// 下の connectRequestShape() で「形」だけを出し、次の実機で内訳を確定する。
//
// 除去は保守的に行う: **送信通知の語彙**を含み、かつ**連携語を含まない**部分だけを落とす。
// 連携語を含む行・装飾は絶対に落とさない（本文を消してしまわないため）。
const CONNECT_WORD_RE = /(?:連携|接続|connect)/iu;
const CONNECT_NOTICE_RE =
  /(?:使用して送信|送信されました|経由で送信|より送信|Sent via|sent via|Sent from|sent using|posted via|Sent with)/iu;
// Slack の装飾（`_…_` / `*…*`）。注記はこの中に入って届くことが多い。
const CONNECT_DECORATION_RE = /(_{1,2}|\*{1,2})([^\n]{0,200}?)\1/gu;

export function stripConnectBoilerplate(text) {
  if (typeof text !== "string") return { text: "", kinds: [] };
  const kinds = new Set();
  // ① 装飾で囲まれた注記を落とす（連携語を含むものは触らない）。
  let value = text.replace(CONNECT_DECORATION_RE, (match, _marker, inner) => {
    if (!CONNECT_NOTICE_RE.test(inner) || CONNECT_WORD_RE.test(inner)) return match;
    kinds.add("decorated_notice");
    return " ";
  });
  // ② 装飾が無い素の注記行を落とす（連携語を含む行は絶対に落とさない）。
  const lines = value.split(/\r?\n/u);
  const kept = lines.filter(line => {
    if (CONNECT_NOTICE_RE.test(line) && !CONNECT_WORD_RE.test(line)) {
      kinds.add("notice_line");
      return false;
    }
    return true;
  });
  if (kept.length !== lines.length) value = kept.join("\n");
  return { text: value, kinds: [...kinds].sort() };
}

function matchesConnectCore(normalized) {
  if (normalized === null || normalized.length === 0) return false;
  if ([...normalized].length > CONNECT_REQUEST_MAX_LENGTH) return false;
  return CONNECT_REQUEST_CORE_RE.test(normalized);
}

// 「短い連携依頼」判定。次のいずれかを満たすときだけ真（誤爆は従来どおり避ける）:
//   (a) 正規化後の**本文全体**が連携語＋助詞・敬語末尾だけ（従来の判定・維持）
//   (b) Slack の定型注記を除去したあとの**全体**が (a) を満たす（同一行に注記が付く形）
//   (c) **最初の中身のある行**が (a) を満たし、かつ**後続の行に連携語が無い**
//       （(b) の語彙に無い未知の定型が付いた場合の受け皿。語彙に依存しないのが要点）
//
// (c) を「行のどれかが一致」にしないのは誤爆を避けるため。利用者の本文が先に来て
// クライアントの定型が後ろに付く、という実際の並びだけを救う。
// 「今日の予定を教えて\n連携」は先頭行が一致しないので通さないし、
// 「〇〇社との連携について提案書を\n連携」は先頭行が一致せず、かつ後続に連携語があるので通さない。
// ── どの規則で一致したかを返す（2026-09-04 レビュー指摘 重大1・重大2）───────────
// 「一致したか」だけでなく **どの規則で一致したか** を返すのが要点。
// 規則ごとに確度が違い、確度によって後続の扱い（モデル応答を消してよいか）を変えるため。
//
//   "whole"          … 正規化後の本文全体が連携語だけ。最も確実。
//   "stripped"       … Slack の送信通知を除いた全体が連携語だけ。ほぼ確実。
//   "leading_line"   … 先頭行だけが連携依頼で、後続行に連携語が無い。**曖昧**。
//   "leading_phrase" … 1 行の**先頭の句**だけが連携依頼で、同じ行の残りが別の依頼
//                      （「連携 今日の予定を教えて」「連携して、あと明日の予定も」）。**曖昧**。
//                      利用者の送信経路によっては改行が落ちて 1 行になるため、
//                      `leading_line` の 1 行版として同じ扱いにする（2026-09-07）。
//
// ⚠️ `leading_line` / `leading_phrase` は「後続に連携語が無い」しか見ていないので、
// 「連携\n今日の予定を教えて」「連携 今日の予定を教えて」のように
// **後続が別の依頼** でも真になる。
// トリガーとしては妥当（利用者は確かに連携を求めている）だが、
// この確度でモデルの最終応答を消すと **別の依頼への回答が消える**。
// 実測でその回帰を出した（本番相当の end-to-end で「予定の回答」が消滅）。
// よって抑止は曖昧な 2 規則では効かせない（replaceExhaustedConnectReply を参照）。
export const CONNECT_RULE_WHOLE = "whole";
export const CONNECT_RULE_STRIPPED = "stripped";
export const CONNECT_RULE_LEADING_LINE = "leading_line";
export const CONNECT_RULE_LEADING_PHRASE = "leading_phrase";

export function classifyConnectRequest(text) {
  // (a) 最も保守的な全体一致。
  if (matchesConnectCore(normalizeConnectRequest(text))) return CONNECT_RULE_WHOLE;
  const stripped = stripConnectBoilerplate(text);
  // (b) 既知の送信通知を除去したうえでの全体一致。
  if (
    stripped.kinds.length > 0 &&
    matchesConnectCore(normalizeConnectRequest(stripped.text))
  ) {
    return CONNECT_RULE_STRIPPED;
  }
  // (c) 未知の定型が後ろに付いた形の受け皿。確度は低い。
  if (matchesLeadingConnectLine(stripped.text)) return CONNECT_RULE_LEADING_LINE;
  // (d) 改行が落ちて 1 行になった「連携＋別依頼」。確度は (c) と同じく低い。
  if (matchesLeadingConnectPhrase(stripped.text)) return CONNECT_RULE_LEADING_PHRASE;
  return null;
}

// 抑止（モデルの最終応答を消す）を許してよい確度か。
// `leading_line` / `leading_phrase` は後続が「別の依頼」でありうるので許さない。
export function connectRuleAllowsSuppression(rule) {
  return rule === CONNECT_RULE_WHOLE || rule === CONNECT_RULE_STRIPPED;
}

export function isShortConnectRequest(text) {
  return classifyConnectRequest(text) !== null;
}

// (c) 未知の定型が後ろに付いた形の受け皿。
// 先頭の中身のある行だけが連携依頼で、後続行のどこにも連携語が無いときに限り真。
// 後続に連携語があると「どれが依頼か」を権威的に決められないので通さない。
function matchesLeadingConnectLine(text) {
  if (typeof text !== "string") return false;
  let leading = null;
  const trailing = [];
  for (const line of text.split(/\r?\n/u)) {
    const normalized = normalizeConnectRequest(line);
    // 走査上限を超える行が混じったら判定しない（従来どおり保守的に落とす）。
    if (normalized === null) return false;
    if (normalized.length === 0) continue;
    if (leading === null) leading = normalized;
    else trailing.push(line);
  }
  if (leading === null || !matchesConnectCore(leading)) return false;
  return !trailing.some(line => CONNECT_WORD_RE.test(line));
}

// (d) 1 行の中で「連携語＋別依頼」が続く形（2026-09-07）。
// 「連携 今日の予定を教えて」「連携して、あと明日の予定も」のように、利用者の送信経路で
// 改行が落ちると (c) の 2 行が 1 行に潰れる。先頭の句（空白・句読点までの部分）だけが
// (a) を満たし、残りが空でなく・連携語を含まず・否定/解除/助詞で始まらないときに限り真。
// 「連携解除」「連携できない」「〇〇社との連携について」は先頭の句が (a) を満たさないか、
// そもそも区切りが無いので通らない。「連携 解除」「連携 できない」は残りの先頭語で落とす。
const CONNECT_PHRASE_SPLIT_RE = /[\s　、。，．,.!！?？・:：;；…]+/u;
// 残りが助詞で始まる（「連携 の設計を説明して」「連携 が切れた」）＝連携語がその文の主語/目的語で、
// 「連携＋別依頼」ではない。保守的に落とす（落ちても従来どおりモデル経路が答える）。
const CONNECT_PHRASE_TRAILING_DENY_RE =
  /^(?:解除|やめ|止め|停止|取り?消|不要|できない|出来ない|しない|切れ|済み|失敗|エラー|について|とは|[のがはをにへとも])/u;
function matchesLeadingConnectPhrase(text) {
  if (typeof text !== "string") return false;
  let leadingLine = null;
  const trailing = [];
  for (const line of text.split(/\r?\n/u)) {
    const normalized = normalizeConnectRequest(line);
    if (normalized === null) return false;
    if (normalized.length === 0) continue;
    if (leadingLine === null) leadingLine = line;
    else trailing.push(line);
  }
  if (leadingLine === null) return false;
  const line = leadingLine.normalize("NFKC").replace(CONNECT_REQUEST_EDGE_RE, "");
  const separator = CONNECT_PHRASE_SPLIT_RE.exec(line);
  if (separator === null || separator.index === 0) return false;
  const head = line.slice(0, separator.index);
  const rest = line.slice(separator.index + separator[0].length).replace(CONNECT_REQUEST_EDGE_RE, "");
  if (rest.length === 0) return false;
  if (!matchesConnectCore(normalizeConnectRequest(head))) return false;
  if (CONNECT_WORD_RE.test(rest) || CONNECT_PHRASE_TRAILING_DENY_RE.test(rest)) return false;
  return !trailing.some(other => CONNECT_WORD_RE.test(other));
}

// ── 受信本文の「形」だけを出す診断（G7: 本文は 1 文字も出さない）───────────────
// 本番で `content_len=16` の内訳が判らず「連携」が落ちた原因を特定できなかったため、
// **本文を出さずに内訳が判る指標**を足す（2026-09-04 レビュー指摘 1）。
// ここで判るのは「何行か」「注記を落とせたか」「どの規則なら通るか」「連携語を含むか」だけ。
export function connectRequestShape(text) {
  if (typeof text !== "string") return "connect_shape=absent";
  const rawLines = text.split(/\r?\n/u);
  const stripped = stripConnectBoilerplate(text);
  const strippedNormalized = normalizeConnectRequest(stripped.text);
  const lineNormalized = [];
  for (const line of stripped.text.split(/\r?\n/u)) {
    const normalized = normalizeConnectRequest(line);
    if (normalized !== null && normalized.length > 0) lineNormalized.push(normalized);
  }
  const leadingLineMatches = matchesLeadingConnectLine(stripped.text);
  const leadingPhraseMatches = matchesLeadingConnectPhrase(stripped.text);
  const yn = value => (value ? "yes" : "no");
  const lengthOf = value => (value === null ? "na" : [...value].length);
  return (
    "connect_shape=" +
    [
      // 何行で届いたか（注記が別行で付いているかの一次判定）。
      `lines:${rawLines.length}`,
      // 注記を落としたあと「中身のある行」が何本残るか。
      `content_lines:${lineNormalized.length}`,
      // どの規則で通る（通らない）のか。
      `whole:${yn(matchesConnectCore(normalizeConnectRequest(text)))}`,
      `stripped:${yn(matchesConnectCore(strippedNormalized))}`,
      `leading_line:${yn(leadingLineMatches)}`,
      `leading_phrase:${yn(leadingPhraseMatches)}`,
      // どの規則で通ったか（抑止の可否はこれで決まる）。
      `rule:${classifyConnectRequest(text) ?? "none"}`,
      // 連携語がそもそも含まれているか／先頭にあるか。
      `word:${yn(CONNECT_WORD_RE.test(text))}`,
      `head_word:${yn(CONNECT_WORD_RE.test((normalizeConnectRequest(text) ?? "").slice(0, 8)))}`,
      // 落とせた注記の種類（語彙は固定・本文は出さない）。
      `boiler:[${stripped.kinds.join("+")}]`,
      // 正規化後の長さ（注記除去前 / 除去後）。
      `stripped_len:${lengthOf(strippedNormalized)}`,
    ].join(",")
  );
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
// progress を渡すと、tools/call を送り出す直前に progress.toolsCallSent = true を立てる
// （ボタンの直接実行が「mcp にツールを渡す前の失敗＝nonce 未消費」かを見分けるため）。
async function callMcpTool({
  fetchFn,
  mcpUrl,
  bearer,
  name,
  toolArguments,
  timeoutMs,
  clientName = MCP_CLIENT_NAME,
  progress = null,
}) {
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
    // streamable-http の SSE は先に 200 と header を返し、結果の event を後から流す。
    // 予算切れは fetch ではなく本文の読み取りで起きるので、ここでも timeout として扱う。
    let text;
    try {
      text = await response.text();
    } catch (error) {
      throw error?.name === "TimeoutError" || error?.name === "AbortError"
        ? new ConnectPathError("timeout")
        : new ConnectPathError("mcp_invalid_json");
    }
    let payload;
    try {
      payload = parseJsonRpcPayload(
        text,
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
      clientInfo: { name: clientName, version: "1" },
    },
  });
  const sessionId = initialized.headers?.get?.("mcp-session-id") || null;
  await readResult(initialized, 1);
  await post({ jsonrpc: "2.0", method: "notifications/initialized", params: {} }, sessionId);
  // ここから先の失敗は「mcp がツールを受け取った（実行した）かもしれない」。
  if (progress) progress.toolsCallSent = true;
  const called = await post(
    { jsonrpc: "2.0", id: 2, method: "tools/call", params: { name, arguments: toolArguments } },
    sessionId,
  );
  return readResult(called, 2);
}

// ── (E) 利用者の状態差を「必ず届く 1 通」に畳む ────────────────────────────
// mcp は失敗も **成功と同じ TextContent の JSON** で返す（server.py:442-445,819）。
// 形は `{"error": "<利用者向けの文面>", "request_id": "…"}` で、`isError` も
// JSON-RPC error も使わない。しかもその `error` 文面は既に利用者向けに整形済みで、
// 「何をすればよいか」＋`診断: CONNECT-Ixx <時刻> <識別子>` の行まで含んでいる
// （connect_diagnostics.py:260-277）。代表例が新規ユーザーの
//   CONNECT-I02「Slack プロフィールのメールアドレスが会社メールになっているか確認し…」
// （skill.py:228-236）。
//
// 従来の extractConnectMessage はこれを一律 `mcp_tool_error` に潰して捨てていた。
// その結果、新規ユーザーには「何も届かない」か、モデルの自作回答だけが届いていた。
// 保証経路では **潰さずそのまま利用者へ渡す**。ここが「新規ユーザーにも必ず
// 次の一手が届く」ことの本体になる。
//
// 成功時（既に連携済み／片方だけ）は `message` にその旨が入って返る
// （skill.py:460-492）ので、追加の分岐は要らない＝状態差は mcp 側の単一正本に委ねる。
export function extractConnectOutcome(result) {
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
  // 利用者向けに整形済みの失敗文面。捨てずに届ける。
  if (typeof data.error === "string") {
    const text = data.error.trim();
    if (!text) throw new ConnectPathError("mcp_invalid_result");
    return { kind: "user_error", text };
  }
  const message = typeof data.message === "string" ? data.message.trim() : "";
  if (!message) throw new ConnectPathError("mcp_invalid_result");
  return { kind: "message", text: message };
}

// ── ボタン結果の文面（直接実行）──────────────────────────────────────────────
// Slack の text は & < > を実体参照にする（Slack の書式規約）。ツールの文はそのまま出すが、
// 文中の < > & が Slack の記法（<url|…> や <@U…>）として解釈されないようにする。
function escapeSlackText(text) {
  return text.replace(/&/gu, "&amp;").replace(/</gu, "&lt;").replace(/>/gu, "&gt;");
}

// リンクにしてよい URL か。mcp のツールが返す本人向けのリンク（Google カレンダー・Gmail）だけを通す。
// 記法を壊す文字（空白・< > |）や https 以外・google.com 以外は、リンクにせず捨てる（文は出す）。
//
// **検査した値と出力する値を一致させる**（2026-09-29 レビュー指摘）。WHATWG の URL パーサは `\` を `/` と
// 読むので、`https://calendar.google.com\@evil.example/x` は hostname=calendar.google.com として通るが、
// RFC 3986 系のパーサ（Slack のクライアント等）は userinfo@evil.example と読みうる。そこで
//   - `\` を拒否文字に入れる
//   - userinfo（user:pass@）を持つ URL を拒否する
//   - 正規化後の href が元の値と 1 字も違わないものだけを通す（検査した形＝出力する形）
// Google が返す htmlLink と mcp が組む Gmail の URL は元から正規形なので、これで落ちるものは無い。
function safeResultLink(value) {
  if (typeof value !== "string" || value === "" || value.length > 2000) return null;
  if (/[\s<>|\\]/u.test(value)) return null;
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return null;
  }
  if (parsed.protocol !== "https:") return null;
  if (parsed.username !== "" || parsed.password !== "") return null;
  if (parsed.href !== value) return null;
  const host = parsed.hostname.toLowerCase();
  return host === "google.com" || host.endsWith(".google.com") ? parsed.href : null;
}

// ツールの出力（mcp の TextContent の JSON）から、押した本人へ送る 1 通を組む。
// 返り値 { reply: {text, blocks?}, result }。result はログ用の種別（値は含めない＝G7）。
//   - message があれば成功・失敗を問わずその文をそのまま出す（ツールの利用者向けの文）。
//     リンク欄は <url|表示名> にする（文中に生の URL があればそこを置き換え、無ければ末尾に添える）。
//   - mcp が「unknown tool: <束縛先>」を返した＝そのツールは mcp に無い（digest_ack は本番 OFF）。
//   - それ以外（mcp の門の拒否・入力検証・例外・壊れた応答）は定型文。mcp の error 文は出さない
//     （英語の例外名・診断コード・内部語を含むため）。
// 例外は投げない（投げると押した人に何も届かない）。
export function renderButtonResult(binding, actionId, result) {
  const failed = kind => ({ reply: { text: binding.texts.failed }, result: kind });
  if (!result || typeof result !== "object" || result.isError === true) {
    return failed("mcp_tool_error");
  }
  const first = Array.isArray(result.content)
    ? result.content.find(item => item?.type === "text" && typeof item.text === "string")
    : null;
  if (!first) return failed("mcp_invalid_result");
  let data;
  try {
    data = JSON.parse(first.text);
  } catch {
    return failed("mcp_invalid_result");
  }
  if (!isPlainObject(data)) return failed("mcp_invalid_result");
  const message = typeof data.message === "string" ? data.message.trim() : "";
  if (message) {
    // ツールの失敗種別（expired / not_connected 等の固定語彙）はログにだけ残す。
    const toolError =
      typeof data.error === "string" && /^[a-z_]{1,32}$/u.test(data.error) ? data.error : "";
    return {
      reply: buildButtonReply(binding, actionId, data, message),
      result: toolError ? `tool_message_error_${toolError}` : "tool_message",
    };
  }
  if (data.error === `unknown tool: ${binding.tool}`) {
    return { reply: { text: BUTTON_UNAVAILABLE_TEXT }, result: "tool_unavailable" };
  }
  const code =
    typeof data.code === "string" && /^[A-Z_]{1,64}$/u.test(data.code)
      ? data.code.toLowerCase()
      : "error";
  // mcp は caller claim の検証失敗をすべて CALLER_IDENTITY_REJECTED で返す（server.py の
  // _verify_caller_claim → _identity_rejected）。その中には one-use nonce の再生（＝同じ押下が
  // すでに実行済み。plugin の再起動・OC タスク 2 つ・台帳の上限落ちで plugin が覚えていないとき）が入り、
  // 本人を確かめられない拒否とは応答から見分けられない。実行済みでありうるので texts.failed
  // （自由文での頼み直しを勧める）にはせず、確認を促す texts.unknown にする（二重登録を招かない）。
  if (code === "caller_identity_rejected") {
    return { reply: { text: binding.texts.unknown }, result: `gateway_${code}` };
  }
  return failed(`gateway_${code}`);
}

function buildButtonReply(binding, actionId, data, message) {
  let text = escapeSlackText(message);
  const rawLink = binding.resultLink ? data[binding.resultLink.field] : null;
  const link = binding.resultLink ? safeResultLink(rawLink) : null;
  if (link !== null) {
    const escapedUrl = escapeSlackText(link);
    const markup = `<${escapedUrl}|${binding.resultLink.label}>`;
    text = text.includes(escapedUrl)
      ? text.split(escapedUrl).join(markup)
      : `${text}\n🔗 ${markup}`;
  } else if (typeof rawLink === "string" && /^https?:\/\/\S/iu.test(rawLink)) {
    // リンクにできなかった URL は文からも消す。📅 の message は URL を生で含む（calendar_event の
    // 「…\n🔗 <htmlLink>」）ので、門で落としても文に残ると Slack が自動でリンクにしてしまう。
    // 消すのは http(s) の URL の形の値だけ（短い別の値で文を削らない）。
    const escapedRaw = escapeSlackText(rawLink);
    if (text.includes(escapedRaw)) {
      text = text
        .split(escapedRaw)
        .join("")
        .split("\n")
        .filter(line => line.trim() !== "🔗")
        .join("\n")
        .trim();
      // 文が URL だけだった（想定外）ときも空の投稿にしない。
      if (text === "") text = escapeSlackText(binding.texts.unknown);
    }
  }
  const undoToken = binding.undoToken
    ? canonicalActionToken(data[binding.undoToken.field], {
        ...binding,
        tokenTypes: [binding.undoToken.tokenType],
      })
    : null;
  // section の mrkdwn は 3000 字まで。超える文にはボタンを付けない（文だけは届ける）。
  if (undoToken === null || text.length > 2900) return { text };
  return {
    text,
    blocks: [
      {
        type: "section",
        text: { type: "mrkdwn", text },
        accessory: {
          type: "button",
          text: { type: "plain_text", text: binding.undoToken.label, emoji: true },
          action_id: actionId,
          value: undoToken,
        },
      },
    ],
  };
}

// ── Slack Web API の最小クライアント（保証経路の配信面） ──────────────────
// 使うのは 2 つだけ:
//   conversations.open … DM の正準 conversation id（`D…`）を得る。mcp の claim 検証が
//                        `^[CDG][A-Z0-9]{8,}$` を要求する（caller_claim.py:39,385）ため、
//                        受信側で `DM:U…` にしか解決できない DM ではこれが必須。
//   chat.postMessage   … 本文の投稿。
// Slack は HTTP 200 + `{"ok": false, "error": "…"}` で失敗を返すので、必ず ok を見る。
async function callSlackApiOnce({ fetchFn, botToken, method, body, timeoutMs }) {
  let response;
  try {
    response = await fetchFn(`${SLACK_API_BASE}/${method}`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${botToken}`,
        "Content-Type": "application/json; charset=utf-8",
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (error) {
    throw error?.name === "TimeoutError" || error?.name === "AbortError"
      ? new ConnectPathError("slack_timeout")
      : new ConnectPathError("slack_fetch_failed");
  }
  // 429 は Retry-After（秒）で待てば通ることが多い。ここだけ待ち時間を持ち帰る。
  if (response.status === 429) {
    const header = response.headers?.get?.("retry-after");
    const seconds = Number.parseInt(typeof header === "string" ? header : "", 10);
    const error = new ConnectPathError("slack_rate_limited");
    error.retryAfterMs =
      Number.isFinite(seconds) && seconds >= 0
        ? Math.min(seconds, SLACK_MAX_RETRY_AFTER_SECONDS) * 1000
        : SLACK_DEFAULT_RETRY_AFTER_MS;
    throw error;
  }
  if (!response?.ok) {
    const error = new ConnectPathError(`slack_http_${response?.status ?? "unknown"}`);
    // 5xx は一時障害として 1 回だけ再試行してよい。4xx は再試行しても同じ。
    error.retryable = typeof response?.status === "number" && response.status >= 500;
    throw error;
  }
  let payload;
  try {
    payload = JSON.parse(await response.text());
  } catch {
    throw new ConnectPathError("slack_invalid_json");
  }
  if (!payload || payload.ok !== true) {
    // Slack の error コードは識別子ではないので、切り分けのために形だけ残す。
    const code = typeof payload?.error === "string" ? payload.error : "unknown";
    const error = new ConnectPathError(`slack_api_${code.slice(0, 64)}`);
    // HTTP 200 + {"ok":false,"error":"ratelimited"} で返ってくる面もある。
    if (code === "ratelimited") error.retryAfterMs = SLACK_DEFAULT_RETRY_AFTER_MS;
    throw error;
  }
  return payload;
}

// 保証経路の **唯一の配信面** なので、一時失敗（429 / 5xx / ネットワーク）で
// 黙って無音にしない。短い固定回数だけ再試行する（2026-09-04 レビュー指摘 小）。
// 全体予算は呼び出し側の timeoutMs × 試行回数を超えないよう、待ちも上限で刈る。
//
// retryOnTimeout=false … 時間切れ（slack_timeout）は再試行しない。Slack が受け付けた後に
// こちらの待ちだけが切れた場合、再送すると同じ投稿が 2 通になる（ボタンの結果の投稿で使う・
// 2026-09-29 レビュー指摘）。429・5xx・接続失敗は従来どおり再試行する。
async function callSlackApi({
  fetchFn,
  botToken,
  method,
  body,
  timeoutMs,
  sleepFn,
  retryOnTimeout = true,
}) {
  let lastError = null;
  for (let attempt = 0; attempt <= SLACK_MAX_RETRIES; attempt += 1) {
    try {
      return await callSlackApiOnce({ fetchFn, botToken, method, body, timeoutMs });
    } catch (error) {
      lastError = error;
      const waitMs =
        typeof error?.retryAfterMs === "number"
          ? error.retryAfterMs
          : error?.retryable === true ||
              (retryOnTimeout && error?.code === "slack_timeout") ||
              error?.code === "slack_fetch_failed"
            ? SLACK_RETRY_BACKOFF_MS
            : null;
      if (waitMs === null || attempt === SLACK_MAX_RETRIES) throw error;
      await sleepFn(waitMs);
    }
  }
  throw lastError;
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

// 捏造 URL ルールが立っているか（層2 の抑止判断で「連携依頼ルールだけ」を切り分ける）。
function urlRuleApplies(kinds) {
  return kinds.length > 0;
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

// 同一受信かどうかは識別子だけで決める。本文由来の判定結果（connectRequest / videoUrlKind）は
// 比較に含めない: content の有無が違う同じ受信の再通知を「別の受信」と誤判定しないため。
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

// 押下の value（mcp の HMAC 署名トークン）の形の検査。束縛ごとに上限と payload の typ を見る。
// 署名そのものは検証しない（鍵は mcp にしか無い）。ここは「そのボタンに載るはずの種類か」の門で、
// 本物かどうかは mcp が HMAC・purpose・本人・期限で判定する。
// mail_draft は tokenTypes=null・上限 160 で、従来の canonicalDraftToken と同じ判定になる。
function canonicalActionToken(value, binding) {
  if (
    !binding ||
    typeof value !== "string" ||
    value !== value.trim() ||
    value.length > binding.maxLength ||
    !DRAFT_TOKEN_RE.test(value)
  ) {
    return null;
  }
  if (binding.tokenTypes === null) return value;
  let payload;
  try {
    payload = JSON.parse(
      Buffer.from(value.slice(0, value.indexOf(".")), "base64url").toString("utf8"),
    );
  } catch {
    return null;
  }
  if (
    !isPlainObject(payload) ||
    payload.v !== 2 ||
    typeof payload.typ !== "string" ||
    !binding.tokenTypes.includes(payload.typ)
  ) {
    return null;
  }
  return value;
}

// ボタンが載っている block の id（Slack の block_id・最大 255 字）。ダイジェストは行ごとに
// actions block を分けていて（block_id は Slack が行ごとに振る）、同じメッセージに同じ件名の
// 📅 が 2 行並んで value の先頭 159 字が一致しても、どの行の押下かを決める手掛かりになる。
// 無い（undefined / null / ""）は null。前後に空白がある・長すぎる・文字列でない値は不正。
function optionalSlackBlockId(value) {
  if (value === undefined || value === null || value === "") {
    return {valid: true, value: null};
  }
  if (typeof value !== "string" || value !== value.trim() || value.length > 255) {
    return {valid: false, value: null};
  }
  return {valid: true, value};
}

// 押下時に捕捉した完全な value が、上流の system event ではどう見えるか（切り詰めの再現）。
function systemEventValueOf(actionValue) {
  return actionValue.length <= SLACK_INTERACTION_VALUE_MAX_LENGTH
    ? actionValue
    : `${actionValue.slice(0, SLACK_INTERACTION_VALUE_MAX_LENGTH - 1)}${SLACK_INTERACTION_VALUE_ELLIPSIS}`;
}

// system event に載っていた value の形。完全なトークン（160 字以内）か、
// 上流が切り詰めた形（先頭 159 字＋…）だけを受け付ける。切り詰めた形は、上限が 160 を超える
// 束縛（📅・☑️ 一括など）でしか起こりえないので、それ以外（mail_draft）では従来どおり拒否する。
function canonicalSystemEventValue(value, binding) {
  if (!binding || typeof value !== "string" || value !== value.trim()) return null;
  if (value.length <= SLACK_INTERACTION_VALUE_MAX_LENGTH && DRAFT_TOKEN_RE.test(value)) {
    return value;
  }
  if (
    binding.maxLength > SLACK_INTERACTION_VALUE_MAX_LENGTH &&
    value.length === SLACK_INTERACTION_VALUE_MAX_LENGTH &&
    value.endsWith(SLACK_INTERACTION_VALUE_ELLIPSIS) &&
    TRUNCATED_ACTION_TOKEN_RE.test(value.slice(0, -SLACK_INTERACTION_VALUE_ELLIPSIS.length))
  ) {
    return value;
  }
  return null;
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

// heartbeat の prompt に載った「Slack interaction:」の system event を 1 件だけ読む。
// value は上流で切り詰められていることがある（eventValue はその見え方のまま返す）。
// 完全な value は押下の捕捉（rememberSlackButtonAction）が持っていて、ここは照合の鍵にだけ使う。
function parseSlackActionSystemEvent(prompt) {
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
  const binding = actionBindingFor(payload.actionId);
  const eventValue = canonicalSystemEventValue(payload.value, binding);
  const blockId = optionalSlackBlockId(payload.blockId);
  if (
    payload.interactionType !== "block_action" ||
    !binding ||
    payload.actionType !== "button" ||
    !senderId ||
    !teamId ||
    !channelId ||
    !messageTs ||
    !thread.valid ||
    !blockId.valid ||
    !eventValue
  ) {
    return null;
  }
  return {
    senderId,
    teamId,
    channelId,
    messageTs,
    threadTs: thread.value,
    actionId: payload.actionId,
    blockId: blockId.value,
    eventValue,
  };
}

// 押下の捕捉（または束縛済みの ingress）が、この system event と同じ押下を指しているか。
// value は上流と同じ切り詰めを掛けた上で比べる（160 字以内なら完全一致と同じ）。
function actionEventMatches(ingress, actionEvent) {
  return (
    ingress?.ingressKind === "action" &&
    ingress.actionId === actionEvent.actionId &&
    ingress.senderId === actionEvent.senderId &&
    ingress.teamId === actionEvent.teamId &&
    ingress.channelId === actionEvent.channelId &&
    ingress.messageId === actionEvent.messageTs &&
    ingress.threadTs === actionEvent.threadTs &&
    (ingress.actionBlockId === null
      ? actionEvent.blockId === null
      : typeof ingress.actionBlockId === "string" &&
        systemEventValueOf(ingress.actionBlockId) === actionEvent.blockId) &&
    typeof ingress.actionValue === "string" &&
    systemEventValueOf(ingress.actionValue) === actionEvent.eventValue
  );
}

// heartbeat run の会話名が、押下の会話と同じか。
// DM では run 側が `DM:<押した本人>` を名乗る（session 鍵 …:direct:<user> は会話 id を持たず、
// hook ctx の channelId は messageTo の user:U… 由来になる: openclaw@v2026.7.1
// src/plugins/hook-agent-context.ts:67-98 / src/sessions/session-key-utils.ts:436-438）。
// 押下の D… と同じ 1:1 会話なので、**押した本人の DM に限って**同一視する
// （通常メッセージの束縛 matchesConversation と同じ規律。他人の DM は `DM:<他人>` なので一致しない）。
function actionRunChannelMatches(actionEvent, runChannelId) {
  return (
    actionEvent.channelId === runChannelId ||
    (SLACK_DM_CHANNEL_RE.test(actionEvent.channelId) &&
      runChannelId === `DM:${actionEvent.senderId}`)
  );
}

export function createCallerIdentityPlugin({
  env = process.env,
  now = () => Date.now(),
  randomBytesFn = randomBytes,
  fetchFn = globalThis.fetch,
  // 保証経路は hook から切り離して走らせる（下の startConnectGuarantee を参照）。
  // その「切り離した仕事」をテストから待てるようにするための注入口。既定は捨てるだけ。
  onBackgroundTask = () => {},
  // Slack 再試行の待ち。テストでは即時に潰す。
  sleepFn = ms => new Promise(resolve => setTimeout(resolve, ms)),
  // ボタン直接実行の mcp 予算（initialize〜tools/call の全体）。テストでタイムアウトを再現するための注入口。
  buttonTimeoutMs = BUTTON_MCP_TIMEOUT_MS,
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
  // (D) 保証経路の配信面。entrypoint の REQUIRED_SECRETS に入っているので本番では必ず届く
  // （openclaw-entrypoint.mjs:15-21・buildChildEnvironment:184）。無い環境では保証経路だけを
  // 畳み、層1/2/3 と署名経路はそのまま生かす（bearer と同じ規律）。
  const rawSlackBotToken = env.SLACK_BOT_TOKEN;
  const slackBotToken =
    typeof rawSlackBotToken === "string" &&
    rawSlackBotToken.trim() &&
    !rawSlackBotToken.includes("${")
      ? rawSlackBotToken.trim()
      : null;
  const rawMcpUrl = env.TEAMAGENT_MCP_URL;
  const mcpUrl =
    typeof rawMcpUrl === "string" && /^https?:\/\//u.test(rawMcpUrl.trim())
      ? rawMcpUrl.trim()
      : DEFAULT_MCP_URL;
  // ボタン押下の直接実行（2026-09-29 裁定）。mcp へ呼ぶ bearer と、結果を届ける bot token の
  // 両方があるときだけ有効（本番は entrypoint の REQUIRED_SECRETS で両方が必ず届く）。
  // どちらかが無い環境（ローカル・テスト）は従来の経路（handled:false → system event →
  // heartbeat run を bindSlackActionRun で束縛）のまま。register のバナーに button_direct を出す。
  const buttonDirect =
    mcpBearer !== null && slackBotToken !== null && typeof fetchFn === "function";

  // 送信者 → DM の正準 conversation id（`D…`）。conversations.open は冪等だが、
  // 「連携」1 通ごとに Slack を叩かないための素朴なキャッシュ。値は不変。
  const dmChannelBySender = new Map();
  // ── 保証経路と層1 が共有する「この受信にはもう答えた」台帳（2026-09-04 レビュー指摘 重大1）──
  // key は pendingKey（= [sessionKey, messageId]）、値は記録時刻。
  //
  // 当初は ingress オブジェクトの `connectDeterministicAttempted` フィールドに持たせていたが、
  // **これは壊れていた**。bindRun 成功時に removePending で pending から ingress が消え、
  // 次の通知は新しい ingress オブジェクトを作るため、旗が毎回リセットされていた。
  // 実測（実物 dist を駆動）: message_received ×2（runId 付き）で投稿 2・tools/call 2、
  // message_received → before_model_resolve → message_received でも投稿 2。
  // 「同じ受信に 1 通」を保つには、旗を **オブジェクトの寿命から切り離す**必要がある。
  // pendingKey は受信そのものの同一性なので、pending に残っていようが run へ束縛済みだろうが
  // 同じ値になる。TTL 掃除は pruneState に相乗りする。
  const connectAnsweredByMessage = new Map();
  // ── 保証経路が「実際に配信できた」受信（2026-09-04 本番実測 TD:45）─────────────
  // connectAnsweredByMessage は「誰かが答えた（or 答えている）」で、投稿失敗時は解放する。
  // こちらは **配信に成功した**ときだけ立てる別の台帳で、モデル側の最終応答を落として
  // よいかの判定に使う。両者を分けるのが要点:
  //   answered  … 一回性（同じ受信に 2 回投稿しない）
  //   delivered … 抑止（利用者に既に届いたので、モデルの重複返信を落としてよい）
  // 失敗時に answered を解放しても delivered は立たないため、抑止が誤って効くことはない。
  const connectDeliveredByMessage = new Map();
  // ── 抑止・層2 判定用の run→ingress 台帳（2026-09-07 本番実測 TD:46）────────────
  // 本番実測（2026-09-04 17:11 JST）: 保証経路が delivered なのに `reply_payload_sending` が
  // cancel を返さず 2 通届いた。原因は **`agent_end` が `reply_payload_sending` より先に
  // 発火する**こと（上流実物: runEmbeddedAttempt が finalize 後に agent_end を起動し
  // (selection-8ixiqbew.js:14591)、最終応答の配信＝reply_payload_sending は run が返った
  // **後**に dispatch 側で走る (dispatch-V82RCNJs.js:1994-1996 → :1716 → :2533)）。
  // 従来は `agent_end`（releaseAgentRun）が `ingressByRun` を掃除しており、
  // `reply_payload_sending` で `ingressByRun.get(runId)` が引けなくなっていた。
  //
  // `ingressByRun` は署名の門（signToolCall）の権威台帳なので、その寿命は延ばさない
  // （延ばすと agent_end 後の tool call も署名できてしまい fail-closed が緩む）。
  // 代わりに **抑止と層2 の判定にだけ使う別台帳**を持つ。中身は bindRun が束縛したのと
  // 同じ ingress オブジェクト（分類結果の複写もそのまま見える）。設定は bindRun だけ
  // （＝新しい信頼境界は作らない。他人の run を掴めないのは ingressByRun と同じ理由）。
  // agent_end では消さず、TTL と上限（pruneConnectGuardState）に任せる。
  // 署名には一切使わない（signToolCall は従来どおり ingressByRun を見る）。
  const connectIngressByRun = new Map();
  // 抑止判定のログを「hook × run × 理由」ごとに 1 回だけ出すための記録
  // （分割 payload ごとに出すと騒音になる。理由が変われば別の行として出す）。
  const connectDecisionLogged = new Map();
  // 「受信に content が無い」警告をフックごとに 1 回だけ出すための記録（騒音防止）。
  const contentAbsentWarned = new Set();
  const pendingByMessage = new Map();
  const pendingActions = new Map();
  const seenActions = new Map();
  // 直接実行（buttonDirect）の押下の台帳。key は押下の指紋（actionFingerprint）、値は記録時刻。
  // 値の形が合わない押下への案内の 1 回性にも使う（key は "notice:" ＋生の value での指紋）。
  // 署名経路の seenActions（10 分）とは寿命を分ける: mcp の one-use nonce より先に切れないよう、
  // ボタンの value の最長の寿命（24h）より長く持つ（BUTTON_PRESS_LEDGER_TTL_MS）。
  // 上限（MAX_BUTTON_PRESS_LEDGER）で古いものから捨て、署名経路の capacity の fail には相乗りさせない。
  const buttonPressLedger = new Map();
  const ingressByRun = new Map();
  const rejectedRuns = new Map();
  const consumedInvocations = new Map();
  const toolCallsByRun = new Map();
  const connectRevisionsByRun = new Map();
  // 動画 URL × 0 tool call の層2 の予算（1 run につき revise は 1 回）。連携とは別の台帳。
  const videoRevisionsByRun = new Map();
  // 層3: revise 予算を使い切っても 0 tool call のままだった run。reply_payload_sending で
  // 本文を定型文に置換する。agent_end より後に配信が走りうるので releaseAgentRun では消さず、
  // 他の台帳と同じ TTL/上限掃除に任せる。
  const connectFallbackByRun = new Map();

  function pruneConnectGuardState(nowMs) {
    for (const ledger of [
      toolCallsByRun,
      connectRevisionsByRun,
      connectFallbackByRun,
      videoRevisionsByRun,
    ]) {
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
    // run→ingress 台帳は受信時刻で TTL を切る（ingressByRun と同じ寿命規律・agent_end 非依存）。
    for (const [runId, ingress] of connectIngressByRun) {
      if (nowMs - ingress.receivedAtMs > INBOUND_CONTEXT_TTL_MS) {
        connectIngressByRun.delete(runId);
      }
    }
    while (connectIngressByRun.size > MAX_CONNECT_GUARD_RUNS) {
      const oldest = connectIngressByRun.keys().next();
      if (oldest.done) break;
      connectIngressByRun.delete(oldest.value);
    }
    for (const [key, atMs] of connectDecisionLogged) {
      if (nowMs - atMs > INBOUND_CONTEXT_TTL_MS) connectDecisionLogged.delete(key);
    }
    while (connectDecisionLogged.size > MAX_CONNECT_GUARD_RUNS) {
      const oldest = connectDecisionLogged.keys().next();
      if (oldest.done) break;
      connectDecisionLogged.delete(oldest.value);
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
    for (const [key, pressedAtMs] of buttonPressLedger) {
      if (nowMs - pressedAtMs > BUTTON_PRESS_LEDGER_TTL_MS) {
        buttonPressLedger.delete(key);
      }
    }
    while (buttonPressLedger.size > MAX_BUTTON_PRESS_LEDGER) {
      const oldest = buttonPressLedger.keys().next();
      if (oldest.done) break;
      buttonPressLedger.delete(oldest.value);
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
    for (const [key, answeredAtMs] of connectAnsweredByMessage) {
      if (nowMs - answeredAtMs > INBOUND_CONTEXT_TTL_MS) {
        connectAnsweredByMessage.delete(key);
      }
    }
    // 署名経路を落とす MAX_TRACKED_CONTEXTS の fail には相乗りさせない（第3層の台帳と同じ規律）。
    // ここでの脱落は「同じ受信にもう 1 通出しうる」だけで、無言にはならない。
    while (connectAnsweredByMessage.size > MAX_CONNECT_GUARD_RUNS) {
      const oldest = connectAnsweredByMessage.keys().next();
      if (oldest.done) break;
      connectAnsweredByMessage.delete(oldest.value);
    }
    for (const [key, atMs] of connectDeliveredByMessage) {
      if (nowMs - atMs > INBOUND_CONTEXT_TTL_MS) connectDeliveredByMessage.delete(key);
    }
    while (connectDeliveredByMessage.size > MAX_CONNECT_GUARD_RUNS) {
      const oldest = connectDeliveredByMessage.keys().next();
      if (oldest.done) break;
      connectDeliveredByMessage.delete(oldest.value);
    }
    // DM の正準 id は不変なので TTL は要らないが、無制限には育てない。
    while (dmChannelBySender.size > MAX_TRACKED_CONTEXTS) {
      const oldest = dmChannelBySender.keys().next();
      if (oldest.done) break;
      dmChannelBySender.delete(oldest.value);
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
    // 拒否した run の束縛は抑止にも使わない（拒否＝この run の受信を権威的に決められない）。
    connectIngressByRun.delete(runId);
    rejectedRuns.set(runId, rejectedAtMs);
  }

  // 抑止・層2 用の run→ingress 記録。bindRun が束縛を確定した直後にだけ呼ぶ。
  // delete→set で挿入順を更新順に保つ（上限退避が「最も古い記録から」になるように）。
  function rememberConnectIngress(runId, ingress) {
    if (ingress.ingressKind !== "message") return;
    connectIngressByRun.delete(runId);
    connectIngressByRun.set(runId, ingress);
  }

  function bindRun(runId, ingress) {
    if (rejectedRuns.has(runId)) return false;
    const existing = ingressByRun.get(runId);
    if (existing) {
      const matches = sameIngress(existing, ingress);
      if (matches) {
        // 同一 ingress の再通知（content を伴う側が後から来る経路）でも判定を失わない。
        //
        // ⚠️ 分類結果は **1 組で意味を持つ**（2026-09-04 レビュー指摘）。
        // 以前は connectRequest だけを複写していたため、
        // 「content 無しの通知が先に来て run へ束縛 → content つきの再通知」の順序だと
        // 束縛側の connectRequestRule が null のまま残り、
        // connectRuleAllowsSuppression(null) === false で抑止が効かなかった
        // （実測 {posts:1, modelCancelled:false, userVisible:2}）。
        // 無音にはならない安全側の重複だが、本番で「`連携` 単独なのに 2 通返る」を見たときに
        // 「run 束縛の問題」か「規則の問題」かを判別できなくなる＝原因を取り違える。
        // 判定・抑止・診断が同じ受信について食い違わないよう、まとめて複写する。
        if (ingress.connectRequest === true) {
          existing.connectRequest = true;
          existing.connectRequestRule = ingress.connectRequestRule;
          existing.connectShape = ingress.connectShape;
          existing.connectNormalizedLength = ingress.connectNormalizedLength;
          existing.connectContentLength = ingress.connectContentLength;
        }
        // 動画 URL の種類も同じ理由で引き継ぐ（content 無しの通知が先に束縛された順序でも層2 が効くように）。
        if (ingress.videoUrlKind && !existing.videoUrlKind) {
          existing.videoUrlKind = ingress.videoUrlKind;
          existing.videoRequestIntent = ingress.videoRequestIntent === true;
        }
        // 再通知でも抑止用台帳を確実に持つ（agent_end 後に ingressByRun 側が消えた後、
        // 同じ受信の再通知が来る順序でも判定が失われないように）。
        rememberConnectIngress(runId, existing);
        removePending(ingress);
      } else rejectRun(runId, now(), ingress);
      return matches;
    }
    for (const bound of ingressByRun.values()) {
      if (sameIngress(bound, ingress)) return false;
    }
    ingressByRun.set(runId, ingress);
    rememberConnectIngress(runId, ingress);
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
      return null;
    }
    const nowMs = now();
    pruneState(nowMs);
    const pendingKey = JSON.stringify([sessionKey, messageId]);
    // event.content は上流が BodyForCommands ?? RawBody ?? Body から作る利用者の生本文
    // （message-hook-mappers:23 / Slack は commandBody ?? rawBody = 封筒無しの本文）。
    // 本文そのものは保持せず、判定結果の真偽だけを ingress に載せる（G7）。
    const connectRequestRule =
      typeof event?.content === "string" ? classifyConnectRequest(event.content) : null;
    const connectRequest = connectRequestRule !== null;
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
    // 本文は保持しない。「形」だけを 1 本の文字列にして持つ（G7）。
    // 本番で `content_len=16` の内訳が判らず原因を特定できなかったため（2026-09-04）。
    const connectShape = connectRequestShape(event?.content);
    // 動画 URL × 0 tool call の層2 用。種類と依頼語の有無だけを持つ（URL・本文は保持しない＝G7）。
    const videoUrlKind = classifyVideoUrl(event?.content);
    const videoRequestIntent = videoUrlKind !== null && hasVideoRequestIntent(event?.content);
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
      // どの規則で一致したか。抑止（モデル応答の cancel）の可否をこれで決める。
      connectRequestRule,
      connectNormalizedLength,
      connectContentLength,
      connectShape,
      videoUrlKind,
      videoRequestIntent,
    };
    const existing = pendingByMessage.get(pendingKey);
    if (existing && !sameIngress(existing, ingress)) {
      pendingByMessage.delete(pendingKey);
      emitPluginLog(logger, "warn", "inbound rejected reason=conflicting_message_identity");
      return null;
    }
    if (existing && Array.isArray(existing.channelAliases)) {
      // 同じ受信が 2 度通知される経路がある（inbound_claim と message_received の両方、
      // および上流の再通知）。解決済みの会話 id 別名だけは引き継ぐ。
      // 一回性は ingress オブジェクトではなく connectAnsweredByMessage が持つ
      // （pending から消えても失効しないようにするため。上の定義を参照）。
      ingress.channelAliases = existing.channelAliases;
    }
    // 動画 URL の判定は本文から決まる。本文を伴わない再通知が後から来ても、先に分かった種類を落とさない。
    if (existing?.videoUrlKind && !ingress.videoUrlKind) {
      ingress.videoUrlKind = existing.videoUrlKind;
      ingress.videoRequestIntent = existing.videoRequestIntent === true;
    }
    pendingByMessage.set(pendingKey, ingress);
    if (runId && !bindRun(runId, ingress)) {
      pendingByMessage.delete(pendingKey);
      emitPluginLog(logger, "warn", "inbound rejected reason=conflicting_run_binding");
      return null;
    }
    // 受理側も観測できないと、層1 の no_candidate_ingress が
    // 「受信を記録できていない」のか「照合が外れた」のか区別できない（2026-09-03）。
    emitTrace(
      logger,
      `inbound recorded connect_request=${connectRequest}` +
        ` normalized_len=${connectNormalizedLength === null ? "na" : connectNormalizedLength}` +
        ` content_len=${connectContentLength === null ? "na" : connectContentLength}` +
        ` ${connectShape}` +
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
    // (D) 保証経路が「今記録した受信」をそのまま使えるように返す。
    // 既存の呼び出し元（inbound_claim）は戻り値を無視するので挙動は変わらない。
    return ingress;
  }

  // ── (D) 保証経路の本体 ───────────────────────────────────────────────────
  // 「連携」と言われたら、モデル・層1/2/3・run 束縛・channel 一致の成否と無関係に、
  // 必ず 1 通（リンク or 診断つき案内）を Slack へ直接届ける。
  //
  // 一回性: 同じ受信に対して 2 回投稿しない。台帳 `connectAnsweredByMessage` を
  // **await より前に同期で**押さえることで層1 と 1 つの旗を共有する。message_received は
  // before_agent_reply より先に走る（dispatch → getReply）ので、通常はこちらが旗を取り、
  // 層1 は `already_attempted` で降りる＝二重投稿にならない。
  // ただし「抑制のために保証を犠牲にしない」ため、モデル経路そのものは止めない。
  // モデルが別途返事をして 2 通に見えることは許容する（無言よりはるかに良い）。
  async function deliverConnectGuarantee(event, ctx, logger, source = "unknown") {
    const ingress = rememberInbound(event, ctx, logger);
    if (!ingress || ingress.ingressKind !== "message") return;
    if (ingress.connectRequest !== true) {
      // 語彙不一致は通常の会話なので黙る。ただし `content` が **文字列ですらない**のは
      // 上流の形が変わった疑いがあり、そのままだと (A) と同型の「設定したのに無音」に
      // なる（2026-09-04 レビュー指摘 中3）。TRACE と無関係に、**フックごとに 1 回だけ**
      // 残す。毎回出すと content を持たない受信（inbound_claim 等）で会話ごとの騒音になる。
      if (ingress.connectContentLength === null && !contentAbsentWarned.has(source)) {
        contentAbsentWarned.add(source);
        emitPluginLog(
          logger,
          "warn",
          `connect guarantee cannot evaluate reason=inbound_content_absent source=${source}` +
            " content_len=na (upstream event.content shape may have changed)",
        );
      }
      return;
    }
    if (connectAnsweredByMessage.has(ingress.pendingKey)) return;
    const invocationId =
      `${CONNECT_GUARANTEE_INVOCATION_PREFIX}-${randomBytesFn(16).toString("hex")}`;
    const nowMs = now();
    const done = (outcome, extra = "") =>
      emitPluginLog(
        logger,
        outcome === "delivered" ? "info" : "warn",
        `connect guarantee invocation=${invocationId} outcome=${outcome}${extra}`,
      );
    // ⚠️ 1 回性の旗は「実際に配信を試みる」と決めた後にだけ立てる。
    // 手前で立てると、保証経路が使えない環境（bot token 無し＝ローカル/テスト）で
    // 層1 まで `already_attempted` で降りてしまい、**誰も答えない**状態を作る。
    // 旗は「この受信にはもう誰かが答えた（or 答えている）」の意味に限定する。
    if (slackBotToken === null) {
      // 配信面が無い環境。層1/2/3 に委ねる（旗は立てない）。
      done("skipped", " reason=no_slack_bot_token");
      return;
    }
    if (typeof fetchFn !== "function") {
      done("skipped", " reason=no_fetch");
      return;
    }
    connectAnsweredByMessage.set(ingress.pendingKey, nowMs);
    let text;
    let kind;
    try {
      // kind は "message"（リンク or 連携済みの案内）か "user_error"
      // （mcp が返した利用者向けの失敗文面。新規ユーザーの CONNECT-I02 等）。
      // 運用側で「リンクを出した」と「何が足りないかを伝えた」を区別できるようにする。
      const outcome = await requestConnectMessage({ ingress, invocationId, nowMs });
      text = outcome.text;
      kind = outcome.kind;
    } catch (error) {
      // mcp まで届かなかった／壊れた戻り値だった。無言では終わらせず、
      // 「もう一度言えばよい」＋管理者へ転送する 1 行を必ず届ける。
      kind = connectPathReason(error);
      text = buildConnectGuaranteeFallbackText({
        senderId: ingress.senderId,
        nowMs,
        reason: kind,
        adminName,
      });
    }
    try {
      await postConnectMessage({ ingress, text });
    } catch (error) {
      // 投稿できなかったので、**この受信は誰にも答えられていない**。台帳を解放して
      // 層1／層2 に救済させる（2026-09-04 レビュー指摘）。
      //
      // 台帳は投稿の前に押さえている（同時に走る再通知で二重投稿しないため）。
      // その状態で失敗のまま抜けると、層1 まで `already_attempted` で降りてしまい
      // **利用者に何も届かない**。実測: slackMode=post_fails で
      // posts 0 / 層1 stand down / fallthrough 0 ＝ 完全な無音だった。
      //
      // 層1 はハーネスの reply 経路で返すので、bot token も Slack Web API も使わない
      // ＝**別の故障ドメイン**である。ここで降りるのは救済機会の放棄になる。
      // 解放しても二重投稿にはならない: 投稿は 0 通で終わっている。
      connectAnsweredByMessage.delete(ingress.pendingKey);
      // 管理者が気付けるよう理由を残す（G7: 値は載せない）。
      done("post_failed", ` result=${kind} reason=${connectPathReason(error)}`);
      return;
    }
    // 配信できた受信だけを記録する。これがモデル側の最終応答を落としてよい唯一の根拠。
    connectDeliveredByMessage.set(ingress.pendingKey, nowMs);
    done("delivered", ` result=${kind}`);
  }

  // ── 保証経路を hook の await から切り離す（2026-09-04 レビュー指摘 重大2）─────
  // `inbound_claim` は **claiming hook** で、上流は各ハンドラを
  // **逐次 await** する（hook-runner-global-Cucx8m-W.js の runClaimingHooksList）。
  // しかも `inbound_claim` は modifyingHookTimeoutMsByHook に無いので **タイムアウトが無い**。
  // ここで保証経路（最大で MCP 15s + Slack 10s×2）を await すると、
  // **受信パイプライン全体をその間止めてしまう**。
  //
  // `message_received` 側は fire-and-forget（dispatch-V82RCNJs.js:1438）かつ
  // void hook のタイムアウトも無い（hook-runner-global:248-253・実物で確認）ので
  // await しても実害は無いが、上流が将来タイムアウトを足したら配信が切られる。
  // どちらのフックから来ても同じ形にしておくほうが安全なので、**両方とも切り離す**。
  //
  // 受信の記録（rememberInbound）と一回性の確保は deliverConnectGuarantee の
  // **最初の await より前**に同期で終わるので、切り離しても取りこぼさない。
  function startConnectGuarantee(event, ctx, logger, source) {
    const task = deliverConnectGuarantee(event, ctx, logger, source).catch(error => {
      // ここに来るのは想定外（内部で握っている）。無言にはしない。
      emitPluginLog(
        logger,
        "warn",
        `connect guarantee crashed reason=${connectPathReason(error)}`,
      );
    });
    onBackgroundTask(task);
  }

  // 保証経路が MCP へ辿り着けなかったときの最終文面。URL も秘匿値も含めない。
  function buildConnectGuaranteeFallbackText({ senderId, nowMs, reason, adminName }) {
    return (
      "連携リンクをお出しできませんでした。恐れ入りますが、もう一度『連携』と送ってください。\n" +
      `${adminForwardHint(adminName)}\n` +
      `診断: ${CONNECT_GUARANTEE_DIAGNOSTIC_CODE} ${formatJstMinute(nowMs)} ${senderId} ${reason}`
    );
  }

  // 受信から oauth_connect を直接呼ぶ。層1 と同じ claim・同じ mcp 手順を使い、
  // 新しい信頼境界は作らない。DM は `DM:U…` にしか解決できないため、mcp の
  // `^[CDG][A-Z0-9]{8,}$`（caller_claim.py:39,385）を満たす正準 id を Slack へ問い合わせる。
  async function requestConnectMessage({ ingress, invocationId, nowMs }) {
    if (mcpBearer === null) throw new ConnectPathError("no_mcp_bearer");
    const claimChannel = await resolveCanonicalChannel(ingress);
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
    return extractConnectOutcome(result);
  }

  // 正準 conversation id。チャンネルはそのまま、DM は conversations.open で `D…` を得る。
  // 解決した `D…` は ingress.channelId を **書き換えず** alias にだけ足す。
  // 書き換えると matchesConversation の DM 別名照合（`DM:<sender>` 側）が崩れ、
  // 後続の bindAgentRun が候補を見失う（＝ C1 の再発）ため。
  async function resolveCanonicalChannel(ingress) {
    if (SLACK_CANONICAL_CHANNEL_RE.test(ingress.channelId)) return ingress.channelId;
    if (ingress.channelId !== `DM:${ingress.senderId}`) {
      throw new ConnectPathError("no_canonical_channel");
    }
    const cached = dmChannelBySender.get(ingress.senderId);
    if (cached) return cached;
    const payload = await callSlackApi({
      fetchFn,
      botToken: slackBotToken,
      method: "conversations.open",
      body: { users: ingress.senderId },
      timeoutMs: SLACK_API_TIMEOUT_MS,
      sleepFn,
    });
    const opened = normalizeSlackId(payload?.channel?.id, SLACK_DM_CHANNEL_RE);
    if (!opened) throw new ConnectPathError("slack_no_dm_channel");
    dmChannelBySender.set(ingress.senderId, opened);
    const aliases = new Set(ingress.channelAliases ?? [ingress.channelId]);
    aliases.add(opened);
    ingress.channelAliases = [...aliases];
    return opened;
  }

  async function postConnectMessage({ ingress, text }) {
    const channel = await resolveCanonicalChannel(ingress);
    const post = body =>
      callSlackApi({
        fetchFn,
        botToken: slackBotToken,
        method: "chat.postMessage",
        body: { channel, text, ...body },
        timeoutMs: SLACK_API_TIMEOUT_MS,
        sleepFn,
      });
    // チャンネルのスレッドで訊かれたらスレッドへ返す（会話面を移さない）。
    if (ingress.threadTs === null) return post({});
    try {
      return await post({ thread_ts: ingress.threadTs });
    } catch (error) {
      // スレッドが消えている・thread_ts が無効といった理由で弾かれることがある
      // （2026-09-04 レビュー指摘 小）。**届かないより会話面がずれるほうがまし**なので、
      // スレッド無しで 1 回だけ投げ直す。それも失敗したら呼び出し側が post_failed を残す。
      if (error?.code === "slack_timeout") throw error;
      return post({});
    }
  }

  // ボタン押下の捕捉（interactive handler）。ACTION_BINDINGS の action_id ごとに 1 つずつ登録し、
  // expectedActionId はその登録の action_id に固定する（handler は自分の namespace の押下しか受けない）。
  // ここで捕捉した**切り詰められていない** value が、その押下で呼べる唯一のツールの
  // トークン引数になる（モデルが system event から写した値は使わない）。
  //
  // 検査は 2 段に分ける（どちらも従来と同じ条件・弱めていない）:
  //   ① 押下そのものの同一性: 本人（Slack が署名した senderId）・会話・メッセージ・trigger・
  //      interactionId の完全一致・data/payload と value の一致・認可済みの送信者。
  //      ここで外れたら何もしない（誰の押下か確かでないので、案内も送らない）。
  //   ② value の形（束縛ごとの上限・payload の typ）。①が通って②だけ外れたのは
  //      「本人が古い／別種のボタンを押した」なので、直接実行が有効なら本人の DM へ案内だけ送る。
  // 直接実行が有効なら（buttonDirect）、捕捉した押下は handler から切り離して実行し、
  // {handled:true} を返す＝上流は system event も heartbeat も積まない（本番は heartbeat 0m）。
  async function rememberSlackButtonAction(ctx, expectedActionId, logger) {
    try {
      const binding = actionBindingFor(expectedActionId);
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
      const rawValue = typeof interaction.value === "string" ? interaction.value : null;
      const actionValue = canonicalActionToken(interaction.value, binding);
      const blockId = optionalSlackBlockId(interaction.blockId);
      const triggerId = nonBlank(interaction.triggerId, 512);
      const interactionId = nonBlank(ctx?.interactionId, 2048);
      // 上流の interactionId（[user, channel, messageTs, triggerId, actionId, value].join(":")）。
      // value は形の検査の前の生の値で組む（形が正しい値では従来の組み方と同一）。
      const expectedInteractionId =
        senderId &&
        channelId &&
        messageTs &&
        triggerId &&
        rawValue !== null
          ? [
              senderId,
              channelId,
              messageTs,
              triggerId,
              expectedActionId,
              rawValue,
            ].join(":")
          : null;
      if (
        !binding ||
        ctx?.channel !== "slack" ||
        ctx?.auth?.isAuthorizedSender !== true ||
        interaction.kind !== "button" ||
        interaction.actionId !== expectedActionId ||
        interaction.namespace !== expectedActionId ||
        rawValue === null ||
        !blockId.valid ||
        interaction.payload !== rawValue ||
        interaction.data !== `${expectedActionId}:${rawValue}` ||
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
          `${PLUGIN_ID}: rejected incomplete or unauthorized Slack button action` +
            ` action=${binding ? expectedActionId : "unbound"}`,
        );
        return {handled: true};
      }
      const threadTs = interactionThread.value ?? contextThread.value;
      const nowMs = now();
      pruneState(nowMs);
      if (!actionValue) {
        emitPluginLog(
          logger,
          "warn",
          `button action rejected reason=value_shape action=${expectedActionId}` +
            ` value_len=${rawValue.length} direct=${buttonDirect ? "yes" : "no"}`,
        );
        if (buttonDirect) {
          // 案内は同じボタン（生の value での指紋）につき 1 回だけ（押した回数だけ届けない）。
          // 投稿に失敗したら印を外し、押し直しで案内をもう一度試せるようにする。
          const noticeKey = `notice:${actionFingerprint({
            senderId,
            teamId: expectedTeamId,
            channelId,
            messageTs,
            threadTs,
            actionId: expectedActionId,
            actionValue: rawValue,
          })}`;
          if (buttonPressLedger.has(noticeKey)) return {handled: true};
          buttonPressLedger.set(noticeKey, nowMs);
          startButtonNotice(
            {actionId: expectedActionId, senderId, channelId, threadTs},
            BUTTON_STALE_TEXT,
            "value_shape",
            logger,
            () => buttonPressLedger.delete(noticeKey),
          );
        }
        return {handled: true};
      }
      const fingerprint = actionFingerprint({
        senderId,
        teamId: expectedTeamId,
        channelId,
        messageTs,
        threadTs,
        actionId: expectedActionId,
        actionValue,
      });
      if (
        seenActions.has(fingerprint) ||
        pendingActions.has(fingerprint) ||
        buttonPressLedger.has(fingerprint)
      ) {
        logger?.warn?.(
          `${PLUGIN_ID}: rejected replayed Slack button action action=${expectedActionId}`,
        );
        return {handled: true};
      }
      if (buttonDirect) {
        // 1 押下 1 回: 台帳は await より前に同期で押さえる（同じ押下の再送・連打は上で止まる）。
        // 実行が mcp へツールを渡す前に失敗したときだけ、executeButtonAction が外す。
        // 台帳は 24h 持つ（BUTTON_PRESS_LEDGER_TTL_MS）＝ボタンが押せる間の押し直しはここで止まる。
        buttonPressLedger.set(fingerprint, nowMs);
        startButtonAction(
          {
            binding,
            actionId: expectedActionId,
            senderId,
            teamId: expectedTeamId,
            channelId,
            threadTs,
            messageTs,
            fingerprint,
            actionValue,
            replyEphemeral: ephemeralReplier(ctx),
          },
          logger,
        );
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
        actionId: expectedActionId,
        actionBlockId: blockId.value,
        actionValue,
        actionToolCallId: null,
        sessionSha256: null,
        receivedAtMs: nowMs,
      };
      seenActions.set(fingerprint, nowMs);
      pendingActions.set(fingerprint, ingress);
      // 直接実行が無効な環境（bearer か bot token が無い）だけの従来経路。
      // handled:false deliberately preserves OpenClaw's fixed-runtime
      // system-event + immediate-heartbeat path after authoritative capture.
      return {handled: false};
    } catch {
      logger?.warn?.(`${PLUGIN_ID}: rejected malformed Slack button action`);
      return {handled: true};
    }
  }

  // ── ボタン押下の直接実行（2026-09-29 裁定「AI を通さず直接処理する」）──────────────────
  // 押した本人の DM で押された押下だけを実行し、結果をその DM へ 1 通だけ投稿する。
  //
  // DM だけに限る理由（チャンネル・グループ・他人の DM での押下は実行しない）:
  //   - 朝ダイジェストは本人の DM にだけ届く（run_morning_digest_fargate.py が conversations.open で
  //     本人の IM を開いて投稿する）。ボタンが DM 以外にある時点で正規の経路ではない。
  //   - 結果は本人にだけ見せる前提の文（カレンダー・Gmail のリンク、候補日時＝G3）。
  //     チャンネルのスレッドへ返すと、そのチャンネルの全員に見える。
  //   - 通常メッセージの束縛（matchesConversation）も DM は「押した本人の DM:<本人>」に限って
  //     同一視している。同じ規律で、押された会話が本人の DM であることを Slack に確かめてから
  //     実行する（conversations.open(users=本人) の IM id と一致すること。保証経路と同じ API・同じ cache）。
  //   DM 以外の押下には、押した本人の DM へ案内（BUTTON_DM_ONLY_TEXT）だけを送る（無言にしない）。
  //
  // 押した人・投稿先の会話は Slack の押下イベントの値（handler の ctx）だけから決める。
  // トークンやツールの出力・モデルの値からは取らない。
  function startButtonAction(press, logger) {
    const task = executeButtonAction(press, logger).catch(error => {
      // ここに来るのは想定外（内部で握っている）。OpenClaw へは例外を返さない。
      emitPluginLog(
        logger,
        "warn",
        `button action crashed action=${press.actionId} reason=${connectPathReason(error)}`,
      );
    });
    onBackgroundTask(task);
  }

  function startButtonNotice(press, text, reason, logger, onFailure = () => {}) {
    const task = deliverButtonNotice(press, text, reason, logger)
      .then(delivered => {
        if (!delivered) onFailure();
      })
      .catch(error => {
        onFailure();
        emitPluginLog(
          logger,
          "warn",
          `button notice crashed action=${press.actionId} reason=${connectPathReason(error)}`,
        );
      });
    onBackgroundTask(task);
  }

  // 押した本人だけに見える一時表示（上流の ctx.respond.reply → Slack の response_url・ephemeral）。
  // 上流の handler ctx にある respond を押下ごとに包んで返す（無い環境では null）。
  // 押下の会話の中で押した本人にだけ見えるので、本人の DM を確かめられないとき（conversations.open の
  // 失敗）にも、投稿先の規律（本人以外に見せない）を崩さずに返事ができる。上流の reply は例外を
  // 握らない（Bolt の respond の失敗がそのまま来る）ので、呼び出し側が必ず catch する。待ちは上限で切る。
  function ephemeralReplier(ctx) {
    const respond = ctx?.respond;
    const reply = respond?.reply;
    if (typeof reply !== "function") return null;
    return async text => {
      let timer = null;
      try {
        await Promise.race([
          Promise.resolve().then(() => reply.call(respond, {text, responseType: "ephemeral"})),
          new Promise((_, reject) => {
            timer = setTimeout(
              () => reject(new ConnectPathError("ephemeral_timeout")),
              BUTTON_EPHEMERAL_TIMEOUT_MS,
            );
            timer.unref?.();
          }),
        ]);
      } finally {
        if (timer !== null) clearTimeout(timer);
      }
    };
  }

  // 一時表示を 1 行出す。失敗しても例外を上げず、ログ用の種別だけ返す。
  async function sendButtonEphemeral(press, text) {
    if (typeof press.replyEphemeral !== "function") return "none";
    try {
      await press.replyEphemeral(text);
      return "ephemeral";
    } catch (error) {
      return `ephemeral_failed_${connectPathReason(error)}`;
    }
  }

  // 押した本人の DM（conversations.open の IM id）。押下の会話がこれと一致したときだけ実行する。
  function openPresserDm(senderId) {
    return resolveCanonicalChannel({channelId: `DM:${senderId}`, senderId});
  }

  async function deliverButtonNotice(press, text, reason, logger) {
    try {
      const channel = await openPresserDm(press.senderId);
      await postButtonMessage({channel, threadTs: channel === press.channelId ? press.threadTs : null}, {text});
    } catch (error) {
      emitPluginLog(
        logger,
        "warn",
        `button notice action=${press.actionId} outcome=post_failed notice=${reason}` +
          ` reason=${connectPathReason(error)}`,
      );
      return false;
    }
    emitPluginLog(
      logger,
      "info",
      `button notice action=${press.actionId} outcome=delivered notice=${reason}`,
    );
    return true;
  }

  async function executeButtonAction(press, logger) {
    const invocationId =
      `${BUTTON_INVOCATION_PREFIX}-${randomBytesFn(16).toString("hex")}`;
    const done = (outcome, extra = "") =>
      emitPluginLog(
        logger,
        outcome === "delivered" ? "info" : "warn",
        `button action invocation=${invocationId} action=${press.actionId}` +
          ` outcome=${outcome}${extra}`,
      );
    // 押された会話が押した本人の DM か（Slack に確かめる）。
    let ownDm;
    try {
      ownDm = await openPresserDm(press.senderId);
    } catch (error) {
      // 本人の DM を確かめられない＝投稿先も決められない。何も実行していないので台帳から外す。
      // 無言にはしない: 押下の会話で押した本人にだけ見える一時表示で「もう一度押して」を返す
      // （本人以外には見えないので、DM を確かめられなくても投稿先の規律は崩れない）。
      buttonPressLedger.delete(press.fingerprint);
      const notice = await sendButtonEphemeral(press, press.binding.texts.retry);
      done("dm_unresolved", ` reason=${connectPathReason(error)} notice=${notice}`);
      return;
    }
    if (ownDm !== press.channelId) {
      // 実行しない（台帳は外さない＝同じ押下で案内を繰り返さない）。案内は本人の DM へ。
      try {
        await postButtonMessage({channel: ownDm, threadTs: null}, {text: BUTTON_DM_ONLY_TEXT});
      } catch (error) {
        done("post_failed", ` result=not_own_dm reason=${connectPathReason(error)}`);
        return;
      }
      done("delivered", " result=not_own_dm");
      return;
    }
    // 結果まで時間のかかるボタン（✏️・🗓）は、押した直後に本人にだけ見える 1 行を出す。
    // mcp の呼び出しは待たせない（並行して走らせ、失敗しても実行には影響させない）。
    if (press.binding.pendingText !== null) {
      onBackgroundTask(
        sendButtonEphemeral(press, press.binding.pendingText).then(notice => {
          if (notice.startsWith("ephemeral_failed")) {
            emitPluginLog(
              logger,
              "warn",
              `button pending invocation=${invocationId} action=${press.actionId} outcome=${notice}`,
            );
          }
        }),
      );
    }
    const progress = {toolsCallSent: false};
    let reply;
    let result;
    try {
      const mcpResult = await callButtonTool({press, invocationId, progress});
      ({reply, result} = renderButtonResult(press.binding, press.actionId, mcpResult));
    } catch (error) {
      const reason = connectPathReason(error);
      if (progress.toolsCallSent) {
        // mcp がツールを受け取った後に途切れた＝実行されたか分からない。台帳は外さない
        // （もう一度押しても、mcp の one-use nonce と plugin の台帳の両方で止まる）。
        reply = {text: press.binding.texts.unknown};
        result = `unknown_${reason}`;
      } else {
        // mcp はまだツールを受け取っていない（nonce も未消費）＝何も実行されていない。
        // 同じボタンをもう一度押せるよう台帳から外す（二重実行は mcp の nonce でも止まる）。
        buttonPressLedger.delete(press.fingerprint);
        reply = {text: press.binding.texts.retry};
        result = `retry_${reason}`;
      }
    }
    try {
      await postButtonMessage({channel: press.channelId, threadTs: press.threadTs}, reply);
    } catch (error) {
      done("post_failed", ` result=${result} reason=${connectPathReason(error)}`);
      return;
    }
    done("delivered", ` result=${result}`);
  }

  // 束縛先の 1 ツールを mcp へ直接呼ぶ（層1 と同じ手順・同じ claim の鋳造）。
  // 引数は束縛のトークン引数 1 つだけ（捕捉した完全な value）。_user_context は mintCallerClaim が
  // 押下の値（本人・team・会話・thread）で丸ごと作る。nonce は押下の指紋から HMAC で決める
  // （heartbeat 経路の signToolCall と同じ）＝同じ押下は mcp の one-use nonce でも 2 回通らない。
  async function callButtonTool({press, invocationId, progress}) {
    const nowMs = now();
    const sessionKey = `${BUTTON_SESSION_PREFIX}:${press.fingerprint}`;
    let signed;
    try {
      signed = mintCallerClaim({
        trusted: {
          senderId: press.senderId,
          teamId: press.teamId,
          channelId: press.channelId,
          threadTs: press.threadTs,
          messageId: press.messageTs,
          sessionSha256: createHash("sha256").update(sessionKey, "utf8").digest("hex"),
        },
        runId: invocationId,
        toolCallId: invocationId,
        tool: press.binding.tool,
        params: {[press.binding.tokenParam]: press.actionValue, [USER_CONTEXT_KEY]: {}},
        nowMs,
        nonceBytes: actionNonceBytes(press.fingerprint),
      });
    } catch {
      throw new ConnectPathError("claim_failed");
    }
    return callMcpTool({
      fetchFn,
      mcpUrl,
      bearer: mcpBearer,
      name: press.binding.tool,
      toolArguments: signed.params,
      timeoutMs: buttonTimeoutMs,
      clientName: MCP_BUTTON_CLIENT_NAME,
      progress,
    });
  }

  // 投稿（保証経路と同じ chat.postMessage）。スレッドで押されたらそのスレッドへ返し、
  // スレッドが弾かれたら（timeout 以外）スレッド無しで 1 回だけ投げ直す（postConnectMessage と同じ）。
  // 時間切れは再送しない（retryOnTimeout=false）: Slack が受け付けた後に待ちだけが切れた場合、
  // 再送すると同じ結果が 2 通届く。429・5xx・接続失敗は保証経路と同じく再試行する。
  // 本人向けのリンク（Google）を展開表示しない。
  async function postButtonMessage({channel, threadTs}, reply) {
    const post = extra =>
      callSlackApi({
        fetchFn,
        botToken: slackBotToken,
        method: "chat.postMessage",
        body: {
          channel,
          text: reply.text,
          ...(Array.isArray(reply.blocks) ? {blocks: reply.blocks} : {}),
          unfurl_links: false,
          unfurl_media: false,
          ...extra,
        },
        timeoutMs: SLACK_API_TIMEOUT_MS,
        sleepFn,
        retryOnTimeout: false,
      });
    if (threadTs === null || threadTs === undefined) return post({});
    try {
      return await post({thread_ts: threadTs});
    } catch (error) {
      if (error?.code === "slack_timeout") throw error;
      return post({});
    }
  }

  // heartbeat run（押下の system event を受けて上流が起こす run）を、捕捉済みの押下 1 件へ束縛する。
  // 照合は system event の見え方（value・blockId は 160 字で切れる）と、捕捉した押下に同じ切り詰めを
  // 掛けたものとの一致で行う。候補がちょうど 1 件のときだけ束縛し、0 件・2 件以上は run ごと拒否する。
  function bindSlackActionRun(event, ctx, logger) {
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
    const actionEvent = parseSlackActionSystemEvent(event?.prompt);
    if (
      !actionEvent ||
      actionEvent.teamId !== expectedTeamId ||
      !actionRunChannelMatches(actionEvent, channelId)
    ) {
      rejectRun(runId, nowMs);
      logger?.warn?.(
        `${PLUGIN_ID}: heartbeat has no exact authoritative Slack button action`,
      );
      return;
    }
    const existing = ingressByRun.get(runId);
    if (existing) {
      if (
        !actionEventMatches(existing, actionEvent) ||
        existing.sessionKey !== sessionKey ||
        !(
          existing.channelId === channelId ||
          (Array.isArray(existing.channelAliases) &&
            existing.channelAliases.includes(channelId))
        )
      ) {
        rejectRun(runId, nowMs);
        logger?.warn?.(
          `${PLUGIN_ID}: rejected mismatched repeated Slack action run`,
        );
      }
      return;
    }
    const candidates = [...pendingActions.values()].filter(
      pending =>
        nowMs - pending.receivedAtMs <= ACTION_CONTEXT_TTL_MS &&
        actionEventMatches(pending, actionEvent),
    );
    if (candidates.length !== 1) {
      rejectRun(runId, nowMs);
      logger?.warn?.(
        `${PLUGIN_ID}: Slack button action is missing, replayed, stale, or ambiguous` +
          ` action=${actionEvent.actionId} candidates=${candidates.length}`,
      );
      return;
    }
    const pending = candidates[0];
    const ingress = {
      ...pending,
      sessionKey,
      sessionSha256: createHash("sha256")
        .update(sessionKey, "utf8")
        .digest("hex"),
    };
    if (channelId !== pending.channelId) {
      // DM の heartbeat run は `DM:<押した本人>` を名乗る。署名の門（signToolCall）が run 側の
      // 名前でも照合できるよう別名に持つ。claim には押下の正準 id（D…）を載せる（mcp が要求する形）。
      ingress.channelAliases = [pending.channelId, channelId];
    }
    if (!bindRun(runId, ingress)) {
      rejectRun(runId, nowMs, ingress);
      logger?.warn?.(
        `${PLUGIN_ID}: Slack button action could not bind one unique run`,
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
          ` content_len=${lengthOf(ingress.connectContentLength)}` +
          ` ${ingress.connectShape ?? "connect_shape=absent"}`,
      );
    }
    // 同じ受信に対して 2 度は鋳造しない（重複発行・往復の防止）。
    // 台帳は pendingKey 基準なので、保証経路が既に答えていれば ingress オブジェクトが
    // 差し替わっていても確実に降りる（2026-09-04 レビュー指摘 重大1）。
    if (connectAnsweredByMessage.has(ingress.pendingKey)) {
      return skipped("already_attempted");
    }
    connectAnsweredByMessage.set(ingress.pendingKey, nowMs);
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
      // (E) 失敗も mcp が利用者向けに整形した文面で返る（新規ユーザーの CONNECT-I02 等）。
      // 捨ててモデル経路へ落とすと「無言」か「自作回答」になるので、そのまま返す。
      const outcome = extractConnectOutcome(result);
      // handled で返すとモデルは起動せず before_model_resolve も走らないため、この受信を
      // ここで消費する。残すと同じ DM の次の受信で bindAgentRun が candidates=2 で run を拒否し、
      // 以後 10 分間すべてのツールが「trusted Slack run identity is missing or stale」で
      // ブロックされる（レビュー実証 2026-09-03）。fallthrough 分岐では残す（モデル経路が束縛に使う）。
      removePending(ingress);
      for (const [boundRunId, bound] of ingressByRun) {
        if (bound === ingress) ingressByRun.delete(boundRunId);
      }
      for (const [boundRunId, bound] of connectIngressByRun) {
        if (bound === ingress) connectIngressByRun.delete(boundRunId);
      }
      emitPluginLog(
        logger,
        "info",
        `connect deterministic path invocation=${invocationId} ` +
          `outcome=answered tool_calls=1 result=${outcome.kind}`,
      );
      return { handled: true, reply: { text: outcome.text } };
    } catch (error) {
      return fallthrough(connectPathReason(error));
    }
  }

  function bindAgentRun(_event, ctx, logger) {
    if (String(ctx?.messageProvider ?? "").toLowerCase() !== "slack") return;
    if (ctx?.trigger === "heartbeat") {
      bindSlackActionRun(_event, ctx, logger);
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
    const matching = [...pendingByMessage.values()].filter(ingress =>
      matchesConversation(ingress, { sessionKey, senderId, channelId, nowMs }),
    );
    // ── C1: 候補が複数でも run を落とさない（2026-09-04） ──────────────────
    // 本番実測 9 件の `trusted Slack run identity is missing or stale` の源はここだった。
    // 従来は `candidates.length !== 1` で run を拒否し、rejectedRuns に 10 分間登録して
    // いたため、その run の **すべてのツール** が block された（signToolCall の
    // `!trusted` 分岐）。連続してメッセージを送る／並行 run が走るだけで再現する。
    //
    // 安全側は崩れない: matchesConversation は sessionKey・senderId・channel
    // （DM の `DM:<sender>` 別名込み）・TTL を **すべて** 満たしたものだけを残す。
    // つまり候補は全員「同じ人の同じ会話の受信」であり、曖昧なのは「どのメッセージか」
    // だけで「誰か」ではない。よって最新の受信を選んでも、他人の受信を掴むことは
    // 原理的に起こらない（他人・別会話は候補になる前に落ちている）。
    // 最新を選ぶのは、run を起こした本人の直近の発話が最も蓋然性が高いため。
    const candidates =
      matching.length > 1
        ? [
            matching.reduce((newest, ingress) =>
              ingress.receivedAtMs >= newest.receivedAtMs ? ingress : newest,
            ),
          ]
        : matching;
    if (matching.length > 1) {
      emitPluginLog(
        logger,
        "info",
        `bind_agent_run disambiguated candidates=${matching.length} rule=newest_in_conversation` +
          ` ${idShape({ sender: senderId, channel: channelId, session: sessionKey })}`,
      );
    }
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

  // ── C2: 会話面の申告不一致は「破棄」であって「拒否」ではない（2026-09-04） ──
  // 本番実測 2 件の `declared channel_id does not match the bound ingress` はここ。
  // モデルが `_user_context.channel_id` に別の値を書いてくると、その run のツールが
  // block され、利用者には「連携できない」としか見えなかった。
  //
  // しかしこの拒否は **セキュリティ上 1 ビットも稼いでいない**。mintCallerClaim は
  // `_user_context` を authoritativeContext で丸ごと置き換えてから署名し、mcp へは
  // その値しか渡らない（この直下の mintCallerClaim を参照）。つまり申告値は
  // 元々 100% 捨てられている。拒否は「捨てる前に落とす」だけの純粋な失敗モードだった。
  // よって会話面 3 フィールドは **黙って捨てず・落とさず・観測して続行** に変える。
  //
  // 一方 `caller_claim` と `slack_user_id` は引き続き block する。前者は署名の持ち込み
  // （replay）で、後者は「自分は別人だ」という唯一の明示的ななりすまし申告であり、
  // 捨てて続行してよい類のものではない（fail-closed を維持する）。
  function validateDeclaredContext(declaredContext, trusted) {
    if (declaredContext[CLAIM_FIELD] !== undefined) {
      return { error: "model-supplied or replayed caller claim is forbidden", discarded: [] };
    }
    // ── 申告が「無い」ことは「別人だと申告した」ことではない（2026-09-11）──────
    // 従来は `declaredContext.slack_user_id !== trusted.senderId` だったため、
    // モデルが `_user_context` を空 `{}` で送った／`slack_user_id` を省いただけで
    // P05 block になっていた（本番実測の P05 経路）。しかし送信者は ingress 側の
    // authoritative 値で確定しており、申告値は mintCallerClaim が丸ごと捨てる。
    // 「申告しなかった」は矛盾ではないので通す。
    // **明示的に別人を名乗った場合（キーがあって値が違う）だけ**は従来どおり block。
    // ここが唯一の明示的ななりすまし申告なので fail-closed を維持する。
    //
    // ── 比較は正規化してから行う（2026-09-11 レビュー指摘）──────────────────
    // 従来はここだけが**生値の厳密一致**だった。ingress 側の senderId は
    // `normalizeSlackId`（trim + 大文字化）を通った値なので、モデルが本人の ID を
    // `u09cx1ccbln`（小文字）・`<@U09CX1CCBLN>`（メンション表記）・前後空白つきで
    // 書いただけで P05 block になっていた。これはなりすましではなく**表記ゆれ**。
    // 正規化して一致すれば通し、そもそも Slack ID として解釈できない値は
    // team / channel と同じ「破棄して続行」に倒す（申告値は mintCallerClaim が
    // authoritative 値で丸ごと置き換えるので、緩めてもなりすましは成立しない）。
    // 解釈できて**別人**なら従来どおり block＝信頼境界は 1 ビットも動かない。
    const discarded = [];
    if (Object.hasOwn(declaredContext, "slack_user_id")) {
      const declaredUserId = normalizeDeclaredSlackUserId(
        declaredContext.slack_user_id,
      );
      if (declaredUserId === null) {
        discarded.push("slack_user_id");
      } else if (declaredUserId !== trusted.senderId) {
        return {
          error: "declared Slack caller does not match the bound ingress",
          discarded: [],
        };
      }
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
        discarded.push(field);
      }
    }
    return { error: null, discarded };
  }

  // block は必ずここを通す（2026-09-03）。1 回の呼び出しで
  //   ① 利用者向けの診断行つき blockReason を組み
  //   ② 管理者向けに 1 行ログを出す（コードと id_shape だけ・値は載せない）
  // ことを不可分にして、「拒否したのにログが 1 行も無い」状態を構造的に作れなくする。
  // ── 上流は hook の返り値を「置換」ではなく「浅いマージ」する（2026-09-11 確定）──
  // 一次検証（openclaw@2026.7.1 の実物）:
  //   dist/agent-tools.before-tool-call-84fX7TrL.js:1735
  //     `if (hookResult?.params) finalParams = mergeParamsWithApprovalOverrides(finalParams, hookResult.params);`
  //   同 :938-947  `mergeParamsWithApprovalOverrides = (o, a) => ({ ...o, ...a })`
  // つまり **元の params のトップレベルキーは消えない**。
  //
  // 事故（本番実測）: 2026-09-04 に入れた unwrap は、`{"arguments":{…}}` を剥がして
  // 中身に署名して返していた。上流のマージで元の `arguments` キーが残るため、
  // 実際に mcp へ届く引数は `{arguments:{…}, …剥がした中身}` になり、署名した
  // `arguments_sha256`（剥がした中身だけ）と一致しない。結果 mcp が
  // `caller claim request binding does not match` で拒否し、利用者には
  // `診断: CONNECT-I01a` が出ていた。
  // 相関は 1:1 で確定: 09-04 以降の unwrap 成功 6 件（09-09 15:12:36 / 15:12:51、
  // 09-10 12:40:32 / :34 / :39 / :41）と caller_claim_rejected 6 件が**同一秒**で一致。
  // つまり unwrap 救済は本番で一度も成立していなかった。
  //
  // 直し方: 署名した集合に無い**元のトップレベルキー**を `undefined` で返し、
  // マージ後の実行引数を署名対象と一致させる。JSON 化（JSON-RPC の tools/call）で
  // `undefined` のキーは落ちるため、mcp が受け取るのは署名した集合そのものになる。
  // 包みが無かった場合（depth 0）は**返り値を一切変えない**（バイト同一を保つ）。
  function reconcileReturnedParams(originalParams, signedParams, unwrapDepth, logger) {
    if (unwrapDepth === 0 || !isPlainObject(originalParams)) return signedParams;
    const removed = Object.keys(originalParams).filter(
      key => !Object.hasOwn(signedParams, key),
    );
    if (removed.length === 0) return signedParams;
    const reconciled = { ...signedParams };
    for (const key of removed) reconciled[key] = undefined;
    // キー名だけ（値は載せない・G7）。本番では `arguments` / `name` のはず。
    emitPluginLog(
      logger,
      "warn",
      `reconciled unwrapped tool arguments removed=[${removed.join(",")}]` +
        " (upstream merges hook params instead of replacing them)",
    );
    return reconciled;
  }

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
    // ── ボタン束縛（ACTION_BINDINGS）─────────────────────────────────────────
    // ボタン由来の run: 押下の action_id に束縛された 1 ツールだけを、1 回だけ署名する。
    // メッセージ由来の run: 束縛表に載るツールは outsideAction に従う
    //   deny        … 署名しない（mail_draft / schedule_propose / digest_ack）
    //   blank_token … 署名するがトークン引数を "" に上書きする（calendar_event の自由文入口）
    const actionBinding =
      trusted.ingressKind === "action" ? actionBindingFor(trusted.actionId) : null;
    const toolBinding =
      trusted.ingressKind === "action" ? null : (ACTION_BINDING_BY_TOOL.get(tool) ?? null);
    if (trusted.ingressKind === "action") {
      if (!actionBinding || tool !== actionBinding.tool) {
        return blockAndLog(
          "Slack button action cannot authorize another tool",
          BLOCK_DIAG.TOOL_NAME_BINDING,
          logger,
          shape(),
        );
      }
      if (trusted.actionToolCallId !== null) {
        return blockAndLog(
          "Slack button action was already consumed",
          BLOCK_DIAG.INVOCATION_BINDING,
          logger,
          shape(),
        );
      }
    } else if (toolBinding && toolBinding.outsideAction !== "blank_token") {
      return blockAndLog(
        `${tool} requires an authoritative Slack button action`,
        BLOCK_DIAG.TOOL_NAME_BINDING,
        logger,
        shape(),
      );
    }
    let params;
    let declaredContext;
    let unwrapDepth = 0;
    try {
      // ── 二重包みの決定論 unwrap（引数検査より前）─────────────────────────
      // ここより下（assertPlainObject / validateDeclaredContext）が「引数検査」なので、
      // その手前で 1 度だけ正規化する。剥がせなければ無変更＝従来どおり block。
      // try の**内側**に置くこと（2026-09-03 レビュー指摘）: 外に出すと、万一 unwrap が
      // throw した場合に block へ変換されず上流へ委ねられ、fail-closed が破れる。
      // （JSON 由来の params では throw 不能だが、規律として例外も block に落とす）
      const unwrapped = unwrapToolArguments(event?.params, observedToolName);
      if (unwrapped.stillWrapped) {
        // 3 段以上。従来は `_user_context` が見つからないことを経由して block に
        // なっていたが、本 PR で欠落を通すようにしたので**明示的に**落とす。
        fail("tool arguments are nested deeper than the unwrap limit");
      }
      unwrapDepth = unwrapped.depth;
      if (unwrapped.depth > 0) {
        // 識別子・本文・URL は載せない（G7）。形と段数だけ。
        emitPluginLog(
          logger,
          "warn",
          `unwrapped tool arguments (shape=${unwrapped.shape}, depth=${unwrapped.depth})`,
        );
      }
      const suppliedParams = assertPlainObject(unwrapped.params, "tool params");
      if (actionBinding) {
        // 押下時に捕捉した完全なトークンで上書きする（system event の値は 160 字で切れている）。
        params = {
          ...suppliedParams,
          [actionBinding.tokenParam]: trusted.actionValue,
        };
      } else if (toolBinding && Object.hasOwn(suppliedParams, toolBinding.tokenParam)) {
        // メッセージ由来ではボタンのトークンを使わせない（自由文の入口としてだけ通す）。
        params = {...suppliedParams, [toolBinding.tokenParam]: ""};
      } else {
        params = suppliedParams;
      }
      // ── `_user_context` はモデルに要求しない（2026-09-11 の根治）─────────────
      // 本番実測 2026-09-10 / 09-11 の P06 全 5 件は、モデルが `_user_context` を
      // **そもそも付けてこなかった**ケースだった（EFS の tool call 実物で確認。
      // 09-11 は knowledge_deliver / search が `{query, top_k, filter_doc_type}` のみ、
      // 09-10 は `{"arguments":{}}`）。二重包みではない。
      //
      // 申告値は mintCallerClaim が authoritativeContext で丸ごと置き換えるため、
      // モデルが何を書いても（書かなくても）mcp へ渡る値は変わらない。
      // つまり「モデルが正しい形で `_user_context` を渡すこと」への依存は
      // **セキュリティを 1 ビットも稼いでいない純粋な失敗モード**だった。
      // 依存を切る: 欠落は `{}` とみなし、プレーンオブジェクトでない申告は
      // 捨てて（観測して）続行する。落とすのは「明示的ななりすまし申告」だけ
      // （validateDeclaredContext の caller_claim / slack_user_id）。
      const rawDeclared = params[USER_CONTEXT_KEY];
      if (rawDeclared === undefined) {
        declaredContext = {};
      } else if (isPlainObject(rawDeclared)) {
        declaredContext = rawDeclared;
      } else {
        // 値・型名だけ出す（G7: 中身は載せない）。
        emitPluginLog(
          logger,
          "warn",
          "discarded non-object declared user_context" +
            ` shape=${Array.isArray(rawDeclared) ? "array" : rawDeclared === null ? "null" : typeof rawDeclared}` +
            " (overwritten with authoritative values)",
        );
        declaredContext = {};
      }
    } catch (error) {
      // 実測 2026-09-03 の 72 件（`_user_context must be a plain object`）はここだった。
      // いまここに残るのは「params 自体がオブジェクトでない」「3 段以上の包み」だけ。
      return blockAndLog(
        error instanceof Error ? error.message : "invalid tool params",
        BLOCK_DIAG.USER_CONTEXT_SHAPE,
        logger,
        shape(),
      );
    }
    const declaration = validateDeclaredContext(declaredContext, trusted);
    if (declaration.error) {
      return blockAndLog(
        declaration.error,
        BLOCK_DIAG.SESSION_OR_CHANNEL_BINDING,
        logger,
        shape(),
      );
    }
    if (declaration.discarded.length > 0) {
      // 実測 2026-09-03 の 2 件（`declared channel_id …`）はここで落ちていた。
      // 今は落とさず、authoritative 値で上書きして続行する。フィールド名だけ残す（G7）。
      emitPluginLog(
        logger,
        "warn",
        "discarded declared user_context fields" +
          ` fields=[${declaration.discarded.join(",")}] (overwritten with authoritative values)`,
      );
    }

    const nonceBytes =
      trusted.ingressKind === "action"
        ? actionNonceBytes(trusted.actionFingerprint)
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
    return { params: reconcileReturnedParams(event?.params, signed.params, unwrapDepth, logger) };
  }

  // 押下 1 件の claim nonce（押下の指紋から HMAC で決める）。heartbeat 経路（signToolCall）と
  // 直接実行（callButtonTool）で同じ値になる＝同じ押下は mcp の one-use nonce で 1 回しか通らない。
  function actionNonceBytes(fingerprint) {
    return createHmac("sha256", secret)
      .update(`teamagent-slack-action-v1:${fingerprint}`, "ascii")
      .digest()
      .subarray(0, 16);
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
  // ── 抑止・層2 の判定（唯一の入口・2026-09-07）──────────────────────────────
  // 署名の門とは独立に、抑止用の run→ingress 台帳（connectIngressByRun）だけを見る。
  // 判定は「抑止してよい」か「なぜ抑止しないか」を **必ず理由つきで** 返す。
  //   no_run_binding      … この run に束縛された受信が無い（束縛失敗・TTL 超過・掃除済み）
  //   not_connect_request … 受信は連携依頼ではない（通常の会話）
  //   not_delivered       … 保証経路が（まだ／結局）配信していない。配信中・失敗・未発火
  //   rule_disallows      … 配信済みだが一致規則が曖昧（leading_line / leading_phrase）
  function resolveConnectSuppression(runId) {
    const ingress = connectIngressByRun.get(runId) ?? null;
    if (!ingress) return { suppress: false, reason: "no_run_binding", ingress };
    if (ingress.connectRequest !== true) {
      return { suppress: false, reason: "not_connect_request", ingress };
    }
    if (!connectDeliveredByMessage.has(ingress.pendingKey)) {
      return { suppress: false, reason: "not_delivered", ingress };
    }
    if (!connectRuleAllowsSuppression(ingress.connectRequestRule)) {
      return { suppress: false, reason: "rule_disallows", ingress };
    }
    return { suppress: true, reason: "already_delivered_by_guarantee", ingress };
  }

  // 判定行の共通末尾。G7: 真偽・規則名・形だけ。本文・URL・Slack 識別子・claim は載せない。
  function describeConnectDecision(decision, ctx) {
    const ingress = decision.ingress;
    return (
      ` connect_request=${ingress ? String(ingress.connectRequest === true) : "na"}` +
      ` rule=${ingress?.connectRequestRule ?? "none"}` +
      ` delivered=${ingress ? String(connectDeliveredByMessage.has(ingress.pendingKey)) : "na"}` +
      ` ${idShape({
        sender: ingress?.senderId ?? ctx?.senderId,
        channel: ingress?.channelId ?? ctx?.channelId,
        message: ingress?.messageId,
        session: ingress?.sessionKey ?? ctx?.sessionKey,
      })}`
    );
  }

  // 「hook × run × 理由」ごとに 1 回だけ出す。TRACE とは無関係に必ず出す
  // （2026-09-07 の教訓: 否定経路の行が無いと「行が無い」しか証拠にできない）。
  function logConnectDecisionOnce(logger, level, hook, runId, reason, line) {
    const key = `${hook}|${runId}|${reason}`;
    if (connectDecisionLogged.has(key)) return false;
    connectDecisionLogged.set(key, now());
    emitPluginLog(logger, level, line);
    return true;
  }

  // event.runId / ctx.runId の権威 run 束縛。食い違い・欠落は理由つきで観測してから触らない。
  // run id が両方欠けるのは run に紐づかない配信（層1 handled の応答・コマンド応答等）で、
  // run 単位の去重ができないため TRACE のときだけ出す。不一致は常時 1 回出す。
  function authoritativeRunId(event, ctx, logger, hook) {
    const eventRunId = canonicalInvocationId(event?.runId);
    const contextRunId = canonicalInvocationId(ctx?.runId);
    if (eventRunId && contextRunId && eventRunId === contextRunId) return eventRunId;
    if (eventRunId && contextRunId) {
      logConnectDecisionOnce(
        logger,
        "warn",
        hook,
        eventRunId,
        "run_mismatch",
        `connect suppression hook=${hook} runId=${eventRunId} outcome=skipped reason=run_mismatch`,
      );
    } else {
      emitTrace(logger, `connect suppression hook=${hook} outcome=skipped reason=no_run_id`);
    }
    return null;
  }

  function guardConnectUrlFabrication(event, ctx, logger) {
    const nowMs = Date.now();
    // finalize だけが走る経路でも台帳が育たないよう、ここでも掃除する。
    // pruneState 全体は呼ばない（容量超過の fail を握り潰して fail-open
    // させないため。掃除は第3層の台帳に限定する）。
    pruneConnectGuardState(nowMs);
    const eventRunId = authoritativeRunId(event, ctx, logger, "before_agent_finalize");
    if (!eventRunId) return undefined;
    const toolCalls = toolCallsByRun.get(eventRunId)?.count ?? 0;
    // 介入しない理由を必ず 1 行残す（run × 理由ごとに 1 回）。
    // 本番実測 2026-09-04 17:11: モデルが oauth_connect を自ら呼んだ run では (a) で黙って
    // 抜けており、「層2 が skip した行が無い」ことしか判らなかった。
    const skip = (reason, decision) => {
      logConnectDecisionOnce(
        logger,
        "info",
        "before_agent_finalize",
        eventRunId,
        reason,
        `connect zero-tool revise runId=${eventRunId} outcome=skipped reason=${reason}` +
          ` tool_calls=${toolCalls}` +
          describeConnectDecision(decision ?? resolveConnectSuppression(eventRunId), ctx),
      );
      return undefined;
    };
    // (a) teamagent tool call が 1 件でもあれば、URL はツール発行でありうる。触らない。
    if (toolCalls > 0) return skip("model_called_tool");
    // (b) 本文が無ければ利用者にも何も届かない。
    if (typeof event?.lastAssistantMessage !== "string") return skip("no_assistant_message");
    const reply = event.lastAssistantMessage.trim();
    if (!reply) return skip("empty_assistant_message");
    // (c) 介入条件は OR: 連携 URL を含む（#353）／利用者の最新メッセージが短い連携依頼（層2）。
    //     後者は run に束縛済みの ingress（権威的な受信）から読む。本文の推測はしない。
    const { kinds } = findFabricatedConnectUrlKinds(reply);
    const decision = resolveConnectSuppression(eventRunId);
    const trustedIngress = decision.ingress;
    const zeroToolConnect = trustedIngress?.connectRequest === true;
    if (kinds.length === 0 && !zeroToolConnect) {
      return skip(trustedIngress ? "not_connect_request" : "no_run_binding", decision);
    }
    // 保証経路が既に配信済みなら、モデルへ「oauth_connect を呼べ」と要求しない。
    // 要求すると **state token がもう 1 個発行される**（本番実測 TD:45 の層2 revise がこれ）。
    // 捏造 URL ルール（kinds）は別問題なので、そちらが立っているときは従来どおり介入する。
    // 規則の確度は問わない（曖昧な一致でも「再要求しない」のは安全側: 別依頼への回答は
    // 抑止側が消さないので届く）。
    if (
      !urlRuleApplies(kinds) &&
      trustedIngress &&
      connectDeliveredByMessage.has(trustedIngress.pendingKey)
    ) {
      return skip("already_delivered_by_guarantee", decision);
    }
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
        // 武装の台帳（connectFallbackByRun）は agent_end で消さない（層3 は agent_end の後に走る）。
        connectFallbackByRun.delete(eventRunId);
        connectFallbackByRun.set(eventRunId, {
          senderId: trustedIngress.senderId,
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

  // 動画 URL × 0 tool call の層2（定数の説明は冒頭の VIDEO_ZERO_TOOL_* を参照）。
  // 連携側（guardConnectUrlFabrication）が何もしなかったときだけ呼ばれる。連携側の挙動は変えない。
  function guardVideoZeroTool(event, ctx, logger) {
    const nowMs = Date.now();
    pruneConnectGuardState(nowMs);
    const eventRunId = authoritativeRunId(event, ctx, logger, "before_agent_finalize");
    if (!eventRunId) return undefined;
    // run に束縛された権威 ingress（連携の抑止・層2 と同じ台帳）。本文の推測はしない。
    // 動画 URL の無い受信（大半の会話）は行を出さずに抜ける（騒音にしない）。
    const ingress = connectIngressByRun.get(eventRunId) ?? null;
    if (!ingress?.videoUrlKind) return undefined;
    const toolCalls = toolCallsByRun.get(eventRunId)?.count ?? 0;
    // 判定行の末尾は連携の判定行と同じく id_shape（識別子の「形」だけ）で揃える。
    // G7: URL・本文・下書き・Slack 識別子は載せない（url_kind は種類名だけ）。
    const describe =
      `video zero-tool revise runId=${eventRunId} tool_calls=${toolCalls} url_kind=${ingress.videoUrlKind}` +
      ` ${idShape({
        sender: ingress.senderId ?? ctx?.senderId,
        channel: ingress.channelId ?? ctx?.channelId,
        message: ingress.messageId,
        session: ingress.sessionKey ?? ctx?.sessionKey,
      })}`;
    // 介入しない理由を run × 理由ごとに 1 回だけ残す（連携の判定行とキーが衝突しないよう接頭辞を付ける）。
    const skip = (reason) => {
      logConnectDecisionOnce(
        logger,
        "info",
        "before_agent_finalize",
        eventRunId,
        `video:${reason}`,
        `${describe} outcome=skipped reason=${reason}`,
      );
      return undefined;
    };
    if (toolCalls > 0) return skip("model_called_tool");
    const draft = event?.lastAssistantMessage;
    if (typeof draft !== "string" || !draft.trim()) return skip("empty_assistant_message");
    // 連携依頼が優先（連携側が予算切れで何も返さなかった run にも、動画の指示は重ねない）。
    if (ingress.connectRequest === true) return skip("connect_request");
    // 連携側が既に revise した run には重ねない。逆順（動画が revise した後の再パスで連携 URL を
    // 捏造）は、連携の捏造ガード（安全側）を抑えずにもう 1 回 revise させる＝1 run で最大 2 回
    // （上流の上限 3 回の内側・どちらも自前予算 1 回なのでループしない。相互検証 2026-09-25）。
    if (connectRevisionsByRun.has(eventRunId)) return skip("connect_revised");
    // 誤爆を避ける: 依頼の語がある、または下書きが断りの形のときだけ介入する。
    const refusal = looksLikeVideoRefusal(draft);
    if (ingress.videoRequestIntent !== true && !refusal) return skip("not_a_request");
    // 自前の予算（1 run につき再パスは 1 回）。上流予算に依存せずループ不在を担保する。
    const revisions = videoRevisionsByRun.get(eventRunId)?.count ?? 0;
    if (revisions >= MAX_VIDEO_ZERO_TOOL_REVISIONS) {
      logger?.warn?.(
        `${PLUGIN_ID}: ${describe} outcome=budget_exhausted reason=model_did_not_call_tool` +
          ` revise_attempt=${revisions}`,
      );
      return undefined;
    }
    videoRevisionsByRun.delete(eventRunId);
    videoRevisionsByRun.set(eventRunId, { count: revisions + 1, updatedAtMs: nowMs });
    const trigger = ingress.videoRequestIntent === true ? "request_intent" : "refusal_draft";
    logger?.warn?.(
      `${PLUGIN_ID}: ${describe} outcome=revised reason=video_url_zero_tool trigger=${trigger}` +
        ` revise_attempt=${revisions + 1}`,
    );
    return {
      action: "revise",
      reason: VIDEO_ZERO_TOOL_REASON,
      retry: {
        instruction: VIDEO_ZERO_TOOL_INSTRUCTION,
        idempotencyKey: VIDEO_ZERO_TOOL_RETRY_KEY,
        maxAttempts: MAX_VIDEO_ZERO_TOOL_REVISIONS,
      },
    };
  }

  // 層3。層2 の再パス後も 0 tool call のまま終わった run の最終応答を、送信直前に
  // 定型文へ置換する。event.runId と ctx.runId は agent run と同じ id
  // （dispatch:2528-2545 が runState.runId を両方に載せる）。食い違えば触らない。
  // 同一 run の 2 通目以降（分割 payload）は、置換済みの定型文と重複するので取り消す。
  // ── AI 生成感の除去・送信直前の安全網（2026-09-14）──────────────────────────────
  // mcp 側（skills/_shared/deai_text.py strip_ai_decoration）は tool 結果の em ダッシュ「—」と
  // 「--」を読点にして返すが、最終応答はモデルが文面を組み直すため再び入る
  // （2026-09-14 本番実測: 検索回答の見出し 3 か所。mcp の投稿は進捗 1 行だけで、最終回答の
  // 投稿主は OpenClaw）。ここは配信直前（reply_payload_sending）で本文だけを正規化する。
  // Python 側と同じ規則: URL・Slack リンク <url|label>・インラインコード・コードフェンス内・
  // 表の区切り行・範囲表記（80—100）は触らない。抑止（cancel）と層3 の定型文置換が先。
  // ログは件数だけ（本文は載せない・G7）。
  const DEAI_PROTECTED_RE = /`[^`\n]*`|<[^<>\s|]+(?:\|[^<>\n]*)?>|\]\([^()\s]*\)|https?:\/\/[^\s<>*]+/g;
  const DEAI_EM_DASH_RE = /([ \t　]*)(—+)([ \t　]*)/g;
  const DEAI_SPACED_HYPHENS_RE =
    /(?<=[^\x00-\x7F])[ \t　]+-{2,}[ \t　]+|[ \t　]+-{2,}[ \t　]+(?=[^\x00-\x7F])/g;
  const DEAI_CJK_HYPHENS_RE = /(?<=[^\x00-\x7F])-{2,}(?=[^\x00-\x7F])/g;
  const DEAI_FENCE_RE = /^[ \t]*(?:```|~~~)/;
  const DEAI_TABLE_DELIM_RE =
    /^[ \t　]*[|｜][ \t　]*:?-{2,}:?[ \t　]*(?:[|｜][ \t　]*:?-{2,}:?[ \t　]*)*[|｜]?[ \t　]*$/;
  const DEAI_DASH_COUNT_RE = /—|-{2,}/g;
  // 退避の番兵は ASCII（Python 側の \x00/\x01 と同じく「和文ではない」扱いになる）。
  const DEAI_SENTINEL_RE = /@@DEAI(\d+)@@/g;
  const isNonAscii = (ch) => typeof ch === "string" && ch.length > 0 && ch.charCodeAt(0) > 0x7f;
  function deaiNormalizeLine(line) {
    const kept = [];
    const masked = line.replace(DEAI_PROTECTED_RE, (m) => {
      kept.push(m);
      return `@@DEAI${kept.length - 1}@@`;
    });
    let out = masked.replace(DEAI_EM_DASH_RE, (m, lead, _dash, trail, offset, whole) => {
      if (offset === 0) return "";
      if (offset + m.length === whole.length) return "";
      if (lead || trail) return "、";
      if (isNonAscii(whole[offset - 1]) && isNonAscii(whole[offset + m.length])) return "、";
      return m;
    });
    out = out.replace(DEAI_SPACED_HYPHENS_RE, "、").replace(DEAI_CJK_HYPHENS_RE, "、");
    if (out !== masked) {
      out = out
        .replace(/、{2,}/g, "、")
        .replace(/([。、！？，：・])、/g, "$1")
        .replace(/、(?=[。！？）」』])/g, "");
    }
    return out.replace(DEAI_SENTINEL_RE, (_, i) => kept[Number(i)]);
  }
  function deaiNormalizeText(text) {
    let inFence = false;
    return text
      .split("\n")
      .map((line) => {
        if (DEAI_FENCE_RE.test(line)) {
          inFence = !inFence;
          return line;
        }
        if (inFence || DEAI_TABLE_DELIM_RE.test(line)) return line;
        return deaiNormalizeLine(line);
      })
      .join("\n");
  }
  function normalizeOutgoingText(event, logger, runId) {
    const payload = event?.payload;
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return undefined;
    if (typeof payload.text !== "string" || !payload.text) return undefined;
    const text = deaiNormalizeText(payload.text);
    if (text === payload.text) return undefined;
    const before = (payload.text.match(DEAI_DASH_COUNT_RE) ?? []).length;
    const after = (text.match(DEAI_DASH_COUNT_RE) ?? []).length;
    emitPluginLog(
      logger,
      "info",
      `deai normalized outgoing text runId=${runId ?? "none"} dashes_removed=${before - after}`,
    );
    return { payload: { ...payload, text } };
  }

  function replaceExhaustedConnectReply(event, ctx, logger) {
    const eventRunId = authoritativeRunId(event, ctx, logger, "reply_payload_sending");
    if (!eventRunId) return normalizeOutgoingText(event, logger, null);
    // ── 二重返信の抑止（2026-09-04 本番実測 TD:45）───────────────────────────
    // 実測ログ: 保証経路が `outcome=delivered` で 1 通配信したあと、層2 の revise を経て
    // モデル経路も同じ内容を 1 通返し、**利用者に同じ内容が 2 通**届いていた。
    //
    // 保証経路が **配信に成功した**受信のターンでは、モデル側の最終応答を送らない。
    // 上流は `reply_payload_sending` の結果が `{cancel:true}` なら
    // その payload の配信を丸ごと飛ばす（実物で確認:
    // deliver-DGDN_7sT.js:36 で null 化 → :852 で cancelled → :1329-1334 で
    // `suppressedPayloadOutcome(reason:"cancelled_by_reply_payload_sending_hook")` → continue）。
    //
    // ⚠️ 抑止の根拠は connectDeliveredByMessage（**配信成功**）だけに置く。
    // connectAnsweredByMessage（一回性）は投稿失敗時に解放されるので、
    // それを根拠にすると「届いていないのにモデルも黙る」＝完全な無音を作りうる。
    // 保証は絶対に落とさない、という原則をここでも守る。
    // ⚠️ 抑止は **確度の高い規則で一致した受信だけ**に効かせる（2026-09-04 レビュー指摘 重大1）。
    // `leading_line` / `leading_phrase` は「先頭が連携依頼・後続に連携語が無い」しか見ないので、
    // 「連携\n今日の予定を教えて」のように **後続が別の依頼** でも真になる。
    // その確度でモデルの最終応答を消すと、**別の依頼への回答が消える**
    // （実測: guaranteePosted:1 / modelAnswerCancelled:true / 予定の回答が消滅）。
    // 曖昧な形では従来どおり「保証 1 通 + モデル 1 通」に留める＝
    // 「無言より 2 通」の原則をここでも守る。トリガーと抑止は別の判断である。
    //
    // ⚠️ 受信は `ingressByRun` ではなく `connectIngressByRun` から引く（2026-09-07 本番実測 TD:46）。
    // このフックは **agent_end の後** に走り、agent_end（releaseAgentRun）は ingressByRun を
    // 掃除する。従来はここで受信が引けず、保証が delivered でも抑止が 1 度も効いていなかった
    // （本番: guarantee delivered → agent_end 08:11:08.488 → reply_payload_sending 08:11:09.171 → 2 通）。
    const decision = resolveConnectSuppression(eventRunId);
    if (decision.suppress) {
      logConnectDecisionOnce(
        logger,
        "info",
        "reply_payload_sending",
        eventRunId,
        decision.reason,
        `connect guarantee suppressed model reply runId=${eventRunId} ` +
          `reason=${decision.reason}` +
          describeConnectDecision(decision, ctx),
      );
      return { cancel: true, reason: CONNECT_GUARANTEE_CANCEL_REASON };
    }
    // 抑止しなかった理由を必ず 1 行残す（run × 理由ごとに 1 回・TRACE 非依存）。
    // 「行が無い」を証拠にせざるを得ない状態を作らない（2026-09-07）。
    logConnectDecisionOnce(
      logger,
      "info",
      "reply_payload_sending",
      eventRunId,
      decision.reason,
      `connect suppression skipped runId=${eventRunId} reason=${decision.reason}` +
        describeConnectDecision(decision, ctx),
    );
    const entry = connectFallbackByRun.get(eventRunId);
    if (!entry) return normalizeOutgoingText(event, logger, eventRunId);
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

  // agent_end。署名の門が使う台帳（ingressByRun / consumedInvocations）と層2 の run 記録を
  // 掃除する。ここで **消してはいけない**もの（2026-09-07 本番実測 TD:46）:
  //   - connectIngressByRun … 抑止（reply_payload_sending）が agent_end の **後** に読む。
  //   - connectFallbackByRun … 層3 の置換も同じく agent_end の後に読む（従来どおり）。
  // 上流実物: runEmbeddedAttempt は finalize の後で agent_end を起動し
  // (selection-8ixiqbew.js:14591)、最終応答の配信（reply_payload_sending）は run が返った
  // 後に dispatch が行う (dispatch-V82RCNJs.js:1994-1996 → :1716 → :2533)。
  // つまり最終応答については **agent_end → reply_payload_sending** の順が常に成り立つ。
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
      // 束縛表の action_id ごとに 1 つずつ登録する（上流は data の最初の ":" より前を namespace とし、
      // 登録の無い namespace の押下は plugin を通らない＝捕捉も束縛もされない）。
      for (const actionId of Object.keys(ACTION_BINDINGS)) {
        api.registerInteractiveHandler({
          channel: "slack",
          namespace: actionId,
          handler: ctx => rememberSlackButtonAction(ctx, actionId, api.logger),
        });
      }
      // ── 「本番でどのフックが実際に呼ばれるか」を必ず観測できるようにする（2026-09-04） ──
      // 事故の教訓: 層1（before_agent_reply）が発火しているのかどうかを 2 便かけて判別
      // できなかった。理由は「発火した事実」を出す行が TRACE 依存で、その TRACE が
      // entrypoint の env allowlist に落とされていたため（openclaw-entrypoint.mjs:43-67）。
      // 以後、次の 2 つは **TRACE と無関係に必ず 1 行ずつ**出す:
      //   ① register 時に「登録を要求したフック名の一覧」
      //   ② 各フックが **最初に呼ばれたとき**に 1 行だけ `hook first_fired name=<hook>`
      // ②を「毎回」ではなく「初回だけ」にするのは、通常の会話 1 通ごとに数行増えるのを
      // 避けるため。知りたいのは「呼ばれるか否か」であって回数ではない。
      // ①と②の差分がそのまま「登録はしたが本番では呼ばれないフック」の一覧になる。
      // 上流は conversation hook（before_model_resolve / before_agent_reply /
      // before_agent_finalize / agent_end 等）を非 bundled plugin に対して
      // `hooks.allowConversationAccess=true` が無ければ **診断だけ積んで黙って捨てる**
      // （registry-D1_pYg_a.js:4224-4235・診断はログに出ない registry.diagnostics 行き）。
      // ①だけでは登録成功を意味しないので、②が唯一の一次証拠になる。
      const firedHooks = new Set();
      // バナーは **実際に api.on した名前**から組む（2026-09-04 レビュー指摘 中2）。
      // 定数を直接出すと、observe() を 1 つ消してもバナーは出続けテストも緑になり、
      // 「バナー vs first_fired の差分＝唯一の一次証拠」という前提そのものが壊れる。
      const registeredHooks = [];
      const observe = (name, handler) => {
        registeredHooks.push(name);
        api.on(name, (event, ctx) => {
          if (!firedHooks.has(name)) {
            firedHooks.add(name);
            // G7: フック名だけ。識別子・本文・URL は載せない。
            emitPluginLog(api.logger, "info", `hook first_fired name=${name}`);
          }
          return handler(event, ctx);
        });
      };
      // (D) 保証経路は **両方の受信フック**に掛ける（2026-09-04 レビュー指摘 重大2）。
      // 「mcp が署名 claim を受理している ⇒ rememberInbound が動いている」という実績は
      // `message_received` **または** `inbound_claim` のどちらかを示すだけで、
      // `message_received` 単独の実証にはならない（origin/dev では両方が
      // rememberInbound に繋がっていたため、実績から区別できない）。
      // 本番で `inbound_claim` だけが発火していた場合、片方だけに掛けると保証は
      // 一度も動かず層1 の二の舞になる。両方に掛け、一回性は
      // connectAnsweredByMessage（pendingKey 基準）が保証する＝二重投稿しない。
      observe("inbound_claim", (event, ctx) => {
        startConnectGuarantee(event, ctx, api.logger, "inbound_claim");
      });
      observe("message_received", (event, ctx) => {
        startConnectGuarantee(event, ctx, api.logger, "message_received");
      });
      observe("before_agent_reply", (event, ctx) =>
        answerShortConnectRequest(event, ctx, api.logger),
      );
      observe("before_model_resolve", (event, ctx) => {
        bindAgentRun(event, ctx, api.logger);
      });
      // logger を渡していなかったのが「14 日間 warn が 1 行も出ない」原因だった（2026-09-03）。
      observe("before_tool_call", (event, ctx) => signToolCall(event, ctx, api.logger));
      // 連携側を先に評価し、何もしなかったときだけ動画 URL × 0 tool call の層2 を評価する。
      observe(
        "before_agent_finalize",
        (event, ctx) =>
          guardConnectUrlFabrication(event, ctx, api.logger) ??
          guardVideoZeroTool(event, ctx, api.logger),
      );
      observe("reply_payload_sending", (event, ctx) =>
        replaceExhaustedConnectReply(event, ctx, api.logger),
      );
      observe("agent_end", (event, ctx) => {
        releaseAgentRun(event, ctx);
      });
      // 実登録と期待値の乖離は起動時に落とす（テストが緑のまま前提が壊れるのを防ぐ）。
      if (registeredHooks.join(",") !== REGISTERED_HOOKS.join(",")) {
        fail(
          `registered hooks drifted from REGISTERED_HOOKS: [${registeredHooks.join(",")}]`,
        );
      }
      emitPluginLog(
        api.logger,
        "info",
        `registered hooks=[${registeredHooks.join(",")}]` +
          ` trace=${traceEnabled ? "on" : "off"}` +
          ` mcp_bearer=${mcpBearer === null ? "no" : "yes"}` +
          ` slack_bot_token=${slackBotToken === null ? "no" : "yes"}` +
          ` button_direct=${buttonDirect ? "yes" : "no"}`,
      );
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
