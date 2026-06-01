#!/usr/bin/env node
// ラッコキーワード 検索量スクレイパ (TeamAgent VSEO 自動化用)
//
// 方式: ログイン済みの Chrome プロファイル (userDataDir) を再利用して、
//       ラッコキーワードの関連KW結果ページから月間検索数/SEO難易度/CPC を取得する。
//       認証情報 (ID/パスワード) は一切扱わない。ユーザーが --login で手動ログインし、
//       そのセッション cookie を userDataDir に永続化 → 以降は自動で使う。
//
// セキュリティ: userDataDir (.userdata/) は cookie=認証情報を含むため .gitignore 済み。
//
// 使い方:
//   # 初回: 画面付きで開く → 手でログイン → ウィンドウを閉じる (セッション保存)
//   node scrape.mjs --login
//
//   # 以降: ログイン済みセッションで検索量を取得
//   node scrape.mjs --query "新宿 ランチ" --out /tmp/rakko.json
//   node scrape.mjs --queries "新宿 ランチ,新宿 グルメ" --limit 30 --out /tmp/rakko.json
//
// 出力 (stdout/--out, JSON):
//   login モード: { ok, mode:"login", message }
//   query モード: { ok, mode:"query", results: { "<KW>": [{kw,vol,seo,cpc}], ... }, error }

import puppeteer from "puppeteer-core";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const USER_DATA_DIR = path.join(__dirname, ".userdata");

function parseArgs(argv) {
  const a = {
    mode: "query", // "login" | "query"
    query: "",
    queries: "",
    limit: 30,
    out: null,
  };
  for (let i = 2; i < argv.length; i++) {
    const k = argv[i];
    if (k === "--login") a.mode = "login";
    else if (k === "--query") a.query = argv[++i];
    else if (k === "--queries") a.queries = argv[++i];
    else if (k === "--limit") a.limit = parseInt(argv[++i], 10) || 30;
    else if (k === "--out") a.out = argv[++i];
  }
  return a;
}

const args = parseArgs(process.argv);
const log = (...m) => console.error("[rakko]", ...m);

function findChrome() {
  if (process.env.CHROMIUM_PATH && fs.existsSync(process.env.CHROMIUM_PATH)) {
    return process.env.CHROMIUM_PATH;
  }
  // 最優先: @puppeteer/browsers でこのディレクトリに入れた専用 Chrome for Testing。
  // 普段使いの Google Chrome と分離でき、2重起動の競合 (起動タイムアウト) を避けられる。
  const cftRoot = path.join(__dirname, "chrome");
  if (fs.existsSync(cftRoot)) {
    try {
      for (const ver of fs.readdirSync(cftRoot).sort().reverse()) {
        const p = path.join(
          cftRoot,
          ver,
          "chrome-mac-arm64",
          "Google Chrome for Testing.app",
          "Contents",
          "MacOS",
          "Google Chrome for Testing",
        );
        if (fs.existsSync(p)) return p;
        // x64 / Linux 命名も一応見る
        const alt = path.join(cftRoot, ver, "chrome-mac-x64", "Google Chrome for Testing.app", "Contents", "MacOS", "Google Chrome for Testing");
        if (fs.existsSync(alt)) return alt;
        const lin = path.join(cftRoot, ver, "chrome-linux64", "chrome");
        if (fs.existsSync(lin)) return lin;
      }
    } catch {
      /* fallthrough */
    }
  }
  const candidates = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ];
  for (const c of candidates) if (fs.existsSync(c)) return c;
  throw new Error("Chrome/Chromium が見つかりません。CHROMIUM_PATH を設定してください");
}

function delay(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

const RESULT_URL = (kw) =>
  `https://rakkokeyword.com/result/relatedKeywords?q=${encodeURIComponent(kw)}`;

// --- login モード: 画面付きで開き、ユーザーが手動ログイン ---
async function runLogin() {
  fs.mkdirSync(USER_DATA_DIR, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: findChrome(),
    headless: false, // 画面を出してユーザーがログインできるように
    userDataDir: USER_DATA_DIR,
    args: ["--no-sandbox", "--disable-setuid-sandbox", "--window-size=1280,900", "--lang=ja-JP"],
  });
  // ログイン中のリダイレクトで frame detach の未処理 rejection が出てもクラッシュさせない
  process.on("unhandledRejection", (e) => {
    log("(無視) unhandledRejection:", String((e && e.message) || e));
  });

  const page = await browser.newPage();
  // goto はリダイレクトで detach し得るので失敗しても続行 (ログインは手動で進める)
  try {
    await page.goto("https://rakkokeyword.com/login", {
      waitUntil: "domcontentloaded",
      timeout: 30000,
    });
  } catch (e) {
    log("(続行) 初期遷移エラー:", String((e && e.message) || e));
  }
  log("==================================================");
  log(" ブラウザが開きました。ラッコにログインしてください。");
  log(" ログインできたら、このブラウザ画面を手で閉じてください。");
  log(" → 閉じるとセッション(cookie)が .userdata/ に保存されます。");
  log("==================================================");

  // ブラウザが閉じられるまで待つ (disconnected イベント)
  await new Promise((resolve) => {
    browser.on("disconnected", resolve);
  });
  // userDataDir に cookie が永続化される (browser は既に閉じているので IO のみ)
  process.stdout.write(
    JSON.stringify({
      ok: true,
      mode: "login",
      message: "ログインセッションを保存しました (.userdata/)",
    }),
  );
  process.exit(0);
}

// --- query モード: ログイン済みセッションで検索量を取得 ---
async function scrapeOne(page, kw, limit) {
  log(`navigate: ${kw}`);
  await page.goto(RESULT_URL(kw), { waitUntil: "domcontentloaded", timeout: 30000 });
  // データ表示までの遅延ロードを待つ (rakko_scrape.md: 5秒以上)
  await delay(6000);
  try {
    await page.waitForSelector("tbody tr", { timeout: 15000 });
  } catch {
    log(`${kw}: テーブル未検出 (ログイン切れ or 0件の可能性)`);
  }

  // rakko_scrape.md の DOM 取得ロジックを移植
  const rows = await page.evaluate((maxN) => {
    const out = [];
    const trs = document.querySelectorAll("tbody tr");
    for (const tr of trs) {
      const cells = tr.querySelectorAll("td");
      if (cells.length >= 5) {
        const kwText = (cells[1].textContent || "").trim();
        if (!kwText) continue;
        out.push({
          kw: kwText,
          seo: (cells[2].textContent || "").trim(),
          vol: (cells[3].textContent || "").trim(),
          cpc: (cells[4].textContent || "").trim(),
        });
      }
      if (out.length >= maxN) break;
    }
    return out;
  }, limit);

  return rows;
}

async function runQuery() {
  // ナビゲート中の frame detach 等でクラッシュさせない (結果は best-effort)
  process.on("unhandledRejection", (e) => {
    log("(無視) unhandledRejection:", String((e && e.message) || e));
  });

  const result = { ok: false, mode: "query", results: {}, error: null };

  if (!fs.existsSync(USER_DATA_DIR)) {
    result.error = "未ログイン: 先に `node scrape.mjs --login` でログインしてください";
    process.stdout.write(JSON.stringify(result));
    process.exit(3);
  }

  const queries = [];
  if (args.query) queries.push(args.query);
  if (args.queries) queries.push(...args.queries.split(",").map((s) => s.trim()).filter(Boolean));
  if (queries.length === 0) {
    result.error = "--query または --queries で検索KWを指定してください";
    process.stdout.write(JSON.stringify(result));
    process.exit(1);
  }

  let browser;
  try {
    browser = await puppeteer.launch({
      executablePath: findChrome(),
      headless: true,
      userDataDir: USER_DATA_DIR,
      args: ["--no-sandbox", "--disable-setuid-sandbox", "--window-size=1280,900", "--lang=ja-JP"],
    });
    const page = await browser.newPage();
    await page.setViewport({ width: 1280, height: 900 });

    let anyData = false;
    for (const kw of queries) {
      const rows = await scrapeOne(page, kw, args.limit);
      result.results[kw] = rows;
      if (rows.length > 0) anyData = true;
      await delay(1500 + Math.random() * 1500); // 人間的な間隔
    }

    result.ok = anyData;
    if (!anyData) {
      result.error =
        "データを取得できませんでした (ログインセッション切れの可能性 → 再度 --login)";
    }
  } catch (e) {
    result.error = String((e && e.message) || e);
    log("ERROR:", result.error);
  } finally {
    if (browser) await browser.close().catch(() => {});
  }

  const out = JSON.stringify(result);
  if (args.out) fs.writeFileSync(args.out, out);
  process.stdout.write(out);
  process.exit(result.ok ? 0 : 2);
}

if (args.mode === "login") {
  await runLogin();
} else {
  await runQuery();
}
