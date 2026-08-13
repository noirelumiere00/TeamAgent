#!/usr/bin/env node
// @openclaw/slack の App Home 実装をビルド時に NewsTV AI 仕様へ差し替える。
//
// なぜ dist を触るのか（2026-08-13）:
//   - Home タブの view は上流にハードコード（header "OpenClaw"・英語文面）。
//     そのまま有効化するとエンジン名がそのまま全員に露出する。
//   - DM を開いた瞬間（app_home_opened の tab === "messages"）は上流が明示的に
//     return しており、初回ウェルカムを出す口が存在しない。
//   上流は tarball の sha256 で固定済み（Dockerfile ADD --checksum）なので、
//   この差し替えは「レビュー済みの版に対する決定的なパッチ」として成立する。
//   置換はすべて出現回数を検証し、上流が変わって前提が崩れたらビルドを落とす。
//
// 使い方: node patch-slack-home.mjs <slack-plugin-dist-dir>
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const distDir = process.argv[2];
if (!distDir) {
  console.error("usage: patch-slack-home.mjs <slack-plugin-dist-dir>");
  process.exit(1);
}

const candidates = readdirSync(distDir).filter(
  (f) => f.startsWith("provider-") && f.endsWith(".js"),
);
const targets = candidates.filter((f) =>
  readFileSync(join(distDir, f), "utf8").includes("function buildSlackHomeView"),
);
if (targets.length !== 1) {
  console.error(
    `FATAL: buildSlackHomeView を含む provider ファイルが ${targets.length} 件 ` +
      `（1件であるべき。候補=${candidates.join(",")}）`,
  );
  process.exit(1);
}
const path = join(distDir, targets[0]);
let src = readFileSync(path, "utf8");

function replaceOnce(label, from, to) {
  const count = src.split(from).length - 1;
  if (count !== 1) {
    console.error(`FATAL: ${label} の出現が ${count} 回（1回であるべき）。上流が変わった`);
    process.exit(1);
  }
  src = src.replace(from, to);
}

// ── ① Home タブの文面を NewsTV AI 仕様へ ─────────────────────────────
replaceOnce(
  "home ヘッダ",
  `text: "OpenClaw"`,
  `text: "NewsTV AI"`,
);
replaceOnce(
  "home 本文",
  `text: "Send a DM, mention OpenClaw in a channel, or use \`/openclaw\` to start a session."`,
  `text: "*NewsTV AI* は Vector 社の営業を支援する社内アシスタントです。\\n" +
\t\t\t\t\t"*まずはこれをコピーして DM に送ってみてください*（約25秒で返ります）\\n" +
\t\t\t\t\t"\\u0060\\u0060\\u0060「（取引先名）」の社内資料を最大3件。資料名・種別・日付・出典リンクを一覧で。\\u0060\\u0060\\u0060\\n" +
\t\t\t\t\t"*できること*\\n" +
\t\t\t\t\t"📂 社内資料をさがす — 過去の提案書・レポートを出典リンクつきで\\n" +
\t\t\t\t\t"📧 メールと予定 — 未読の仕分け・空き時間の提示・返信の下書き\\n" +
\t\t\t\t\t"📱 SNS を調べる — TikTok の伸び動画・X の生活者の声・上位動画の分析\\n" +
\t\t\t\t\t"🔗 メールと予定を使う方は、DM に「連携」と送ってください\\n" +
\t\t\t\t\t"⚠️ 依頼はチャンネルでもこの DM でも OK ／ 困ったときは小俣に DM"`,
);
replaceOnce(
  "home 注記",
  `text: "This Home tab is safe to show to any workspace member who opens the app."`,
  `text: "外に出る操作（メール送信・予定登録・SNS投稿）は行いません。見えるのはご自身の受信箱とカレンダーだけです。"`,
);

// ── ② DM（messages タブ）初回オープンでウェルカムを1回だけ送る ────────────
// 上流は tab === "messages" を無条件 return している。履歴が空のときだけ
// ようこそカードを送る分岐に差し替える（履歴が1件でもあれば何もしない＝再送しない。
// 2026-08-07 に配布済みのようこそカード受領者にも再送されない）。
replaceOnce(
  "messages タブ分岐",
  `if (!payload.user || payload.tab === "messages") return;`,
  `if (!payload.user) return;
\t\t\tif (payload.tab === "messages") {
\t\t\t\tawait maybeSendNewstvWelcome(ctx, payload);
\t\t\t\treturn;
\t\t\t}`,
);

// registerSlackHomeEvents と同じモジュールへウェルカム送信関数を追記する。
// 関数宣言はモジュールスコープで巻き上がるため、参照より後方への追記でよい。
replaceOnce(
  "ウェルカム関数の追記アンカー",
  `//#endregion
//#region extensions/slack/src/interactive-dispatch.ts`,
  `async function maybeSendNewstvWelcome(ctx, payload) {
\ttry {
\t\tconst channel = payload.channel;
\t\tif (!channel) return;
\t\tconst history = await ctx.app.client.conversations.history({
\t\t\ttoken: ctx.botToken,
\t\t\tchannel,
\t\t\tlimit: 1,
\t\t});
\t\tif ((history.messages ?? []).length > 0) return;
\t\tawait ctx.app.client.chat.postMessage({
\t\t\ttoken: ctx.botToken,
\t\t\tchannel,
\t\t\ttext:
\t\t\t\t":wave: *NewsTV AI へようこそ*\\n" +
\t\t\t\t"Slack で話しかけるだけで、調べもの・整理・下書きを引き受ける社内アシスタントです。専用画面も、覚えるコマンドもありません。\\n" +
\t\t\t\t"*まずはこれをコピーして、この DM に送ってみてください*（約25秒で返ります）\\n" +
\t\t\t\t"\\u0060\\u0060\\u0060「（取引先名）」の社内資料を最大3件。資料名・種別・日付・出典リンクを一覧で。\\u0060\\u0060\\u0060\\n" +
\t\t\t\t"（取引先名）はご自身の担当クライアントに置き換えてください。\\n" +
\t\t\t\t"*できること*\\n" +
\t\t\t\t":open_file_folder: *社内資料をさがす* — 取引先名から過去の提案書・レポートを出典リンクつきで\\n" +
\t\t\t\t":e-mail: *メールと予定* — 未読の仕分け・空き時間の提示・返信の下書き\\n" +
\t\t\t\t":iphone: *SNSを調べる* — TikTokの伸び動画・Xの生活者の声・上位動画の分析\\n" +
\t\t\t\t":link: *メールと予定を使う方は、この DM に「連携」と送ってください。* Google の許可画面をご案内します（資料検索・SNS調査は連携なしで使えます）。\\n" +
\t\t\t\t"*やらないこと* — メールの送信・予定の登録・SNSへの投稿は行いません。外に出る操作は必ずご自身の手で行っていただきます。見えるのはご自身の受信箱とカレンダーだけです。\\n" +
\t\t\t\t"平日の朝9:30に、その日の要点（要返信メール・予定）を自動でお届けします。\\n" +
\t\t\t\t":warning: 依頼はチャンネルでもこの DM でも OK ／ 困ったときは *小俣に DM* してください。",
\t\t});
\t} catch (err) {
\t\tctx.runtime.error?.(danger(\`slack welcome message failed: \${formatErrorMessage(err)}\`));
\t}
}
//#endregion
//#region extensions/slack/src/interactive-dispatch.ts`,
);

writeFileSync(path, src);
console.log(`patched: ${path}`);
