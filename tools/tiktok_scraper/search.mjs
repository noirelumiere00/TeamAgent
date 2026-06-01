#!/usr/bin/env node
// TikTok 検索スクレイパ CLI (TeamAgent tiktok_search Skill 用)
//
// 方式: Puppeteer で実ブラウザ Chrome を起動 → TikTok 検索/タグページに遷移 →
//       人間的にスクロール → 内部 API (/api/search/general/, /api/challenge/item_list/)
//       のレスポンスをネットワーク傍受でキャプチャ → 動画メタを JSON で stdout に出す。
//       X-Bogus 等の署名はブラウザ自身が生成するため外部リクエスト不要。
//
// 出典: vseo-analytics-web/server/tiktokScraper.ts の searchInIncognitoContext を
//       Mac/CLI 向けに移植・単純化 (3 重検索→単一セッション、結果は1回分)。
//
// 使い方:
//   node search.mjs --query "新宿 ランチ" --type keyword --max 10 [--out /tmp/x.json]
//   node search.mjs --query "新宿"        --type hashtag  --max 10
//
// 出力 (stdout, JSON): { ok, query, type, count, videos: [...], error }
// ブラウザのログは stderr に出す (stdout は JSON のみ = Python が parse しやすい)。

import puppeteer from "puppeteer-core";

// ---- 引数パース ----
function parseArgs(argv) {
  const a = { query: "", type: "keyword", max: 10, out: null, headful: false };
  for (let i = 2; i < argv.length; i++) {
    const k = argv[i];
    if (k === "--query") a.query = argv[++i];
    else if (k === "--type") a.type = argv[++i];
    else if (k === "--max") a.max = parseInt(argv[++i], 10) || 10;
    else if (k === "--out") a.out = argv[++i];
    else if (k === "--headful") a.headful = true;
  }
  return a;
}

const args = parseArgs(process.argv);
const log = (...m) => console.error("[tiktok]", ...m); // stderr

// ---- Chrome 実行パス自動検出 (Mac/Linux 両対応) ----
import fs from "fs";
function findChrome() {
  if (process.env.CHROMIUM_PATH && fs.existsSync(process.env.CHROMIUM_PATH)) {
    return process.env.CHROMIUM_PATH;
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

const USER_AGENTS = [
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
];

function humanDelay(minMs, maxMs) {
  const ms = minMs + Math.random() * (maxMs - minMs);
  return new Promise((r) => setTimeout(r, ms));
}

// API レスポンス item → 動画オブジェクト (search/general 形式: {type:1, item:{...}})
function parseSearchItem(item) {
  if (!item || item.type !== 1 || !item.item) return null;
  return normalizeVideo(item.item);
}

// challenge / SSR 形式: 動画オブジェクト直
function normalizeVideo(v) {
  if (!v || !v.id) return null;
  const stats = v.stats || {};
  const author = v.author || {};
  const authorStats = v.authorStats || v.author_stats || {};
  const hashtags = [];
  if (Array.isArray(v.textExtra)) {
    for (const te of v.textExtra) if (te.hashtagName) hashtags.push(te.hashtagName);
  }
  const m = (v.desc || "").match(/#[\w　-鿿]+/g);
  if (m) for (const t of m) { const c = t.replace("#", ""); if (!hashtags.includes(c)) hashtags.push(c); }

  return {
    id: String(v.id),
    url: `https://www.tiktok.com/@${author.uniqueId || "_"}/video/${v.id}`,
    desc: v.desc || "",
    createTime: v.createTime || 0,
    duration: v.video?.duration || 0,
    coverUrl: v.video?.cover || v.video?.originCover || "",
    author: {
      uniqueId: author.uniqueId || "",
      nickname: author.nickname || "",
      followerCount: author.followerCount || authorStats.followerCount || 0,
    },
    stats: {
      playCount: stats.playCount || 0,
      diggCount: stats.diggCount || 0,
      commentCount: stats.commentCount || 0,
      shareCount: stats.shareCount || 0,
      collectCount: Number(stats.collectCount) || 0,
    },
    hashtags,
    music: v.music
      ? { title: v.music.title || "", authorName: v.music.authorName || "", original: !!v.music.original }
      : null,
  };
}

async function searchOnce(browser, query, type, maxVideos) {
  const isTag = type === "hashtag";
  const context = await browser.createBrowserContext();
  const page = await context.newPage();
  const allVideos = [];
  let pagesFetched = 0;
  let latestHasMore = true;

  const MAX_PAGES = 18;
  const MAX_SCROLL = 20;

  try {
    await page.setViewport({ width: 1280, height: 900 });
    await page.setUserAgent(USER_AGENTS[Math.floor(Math.random() * USER_AGENTS.length)]);
    await page.setExtraHTTPHeaders({ "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7" });

    // プロキシ認証 (任意。PROXY_USERNAME/PASSWORD があれば)
    if (process.env.PROXY_USERNAME && process.env.PROXY_PASSWORD) {
      await page.authenticate({
        username: `${process.env.PROXY_USERNAME}-session-${Date.now()}`,
        password: process.env.PROXY_PASSWORD,
      });
    }

    // 不要リソース遮断 (media/font) — 画像は残す (DOM高さ→IntersectionObserver発火に必要)
    await page.setRequestInterception(true);
    page.on("request", (req) => {
      const rt = req.resourceType();
      const u = req.url();
      if (rt === "media" || rt === "font") return req.abort();
      if (u.includes("google-analytics.com") || u.includes("googletagmanager.com") || u.includes("doubleclick.net"))
        return req.abort();
      return req.continue();
    });

    // ネットワーク傍受: 検索 API / challenge API
    page.on("response", async (resp) => {
      const url = resp.url();
      const isSearch = url.includes("/api/search/general/");
      const isChallenge = isTag && url.includes("/api/challenge/item_list/");
      if (!isSearch && !isChallenge) return;
      try {
        const text = await resp.text();
        if (!text || text.includes("<html") || text.includes("<!DOCTYPE")) return;
        const json = JSON.parse(text);
        const items = json?.data || json?.itemList || json?.item_list || [];
        let added = 0;
        for (const it of items) {
          const v = isSearch ? parseSearchItem(it) : normalizeVideo(it);
          if (v && !allVideos.find((e) => e.id === v.id)) { allVideos.push(v); added++; }
        }
        pagesFetched++;
        latestHasMore = json.has_more === true || json.has_more === 1 || json.hasMore === true;
        log(`API page ${pagesFetched}: +${added} (total ${allVideos.length}) has_more=${latestHasMore}`);
      } catch { /* 非JSONは無視 */ }
    });

    // 初回 Cookie 取得
    await page.goto("https://www.tiktok.com/", { waitUntil: "domcontentloaded", timeout: 30000 });
    await humanDelay(2000, 3500);

    // 検索/タグページへ
    const navUrl = isTag
      ? `https://www.tiktok.com/tag/${encodeURIComponent(query)}`
      : `https://www.tiktok.com/search?q=${encodeURIComponent(query)}`;
    log(`navigate: ${navUrl}`);
    await page.goto(navUrl, { waitUntil: "domcontentloaded", timeout: 30000 });

    const waitSel = isTag
      ? '[data-e2e="challenge-item"], [class*="DivItemContainerV2"]'
      : '[data-e2e="search_top-item-list"], [class*="DivItemContainerV2"]';
    try {
      await page.waitForSelector(waitSel, { timeout: 15000 });
    } catch { log("grid selector timeout, continue with SSR/scroll"); }
    await humanDelay(3000, 4500);

    // SSR フォールバック (傍受で 0 件のとき埋め込み JSON から)
    if (allVideos.length === 0) {
      try {
        const ssr = await page.evaluate((tag) => {
          const el = document.getElementById("__UNIVERSAL_DATA_FOR_REHYDRATION__");
          if (!el?.textContent) return null;
          try {
            const p = JSON.parse(el.textContent);
            const scope = p?.["__DEFAULT_SCOPE__"] || {};
            if (tag) {
              const cd = scope["webapp.challenge-detail"];
              return cd?.itemList || null;
            }
            return scope["webapp.search-detail"]?.data || null;
          } catch { return null; }
        }, isTag);
        if (Array.isArray(ssr)) {
          for (const it of ssr) {
            const v = isTag ? normalizeVideo(it) : parseSearchItem(it);
            if (v && !allVideos.find((e) => e.id === v.id)) allVideos.push(v);
          }
          log(`SSR extraction: ${allVideos.length} videos`);
        }
      } catch (e) { log("SSR extraction failed:", e.message); }
    }

    // ページネーション (スクロールで内部 API を誘発)
    let noNew = 0;
    let hasMoreFalseRetries = 0;
    for (let s = 0; s < MAX_SCROLL; s++) {
      if (allVideos.length >= maxVideos) { log(`reached target ${allVideos.length}/${maxVideos}`); break; }
      if (pagesFetched >= MAX_PAGES) break;
      if (!latestHasMore && pagesFetched > 0) {
        if (hasMoreFalseRetries >= 3) break;
        hasMoreFalseRetries++;
        await humanDelay(3000, 5000);
      }
      const prev = allVideos.length;
      await page.evaluate((idx) => {
        const c = document.querySelector("#grid-main");
        if (c) c.scrollTop = c.scrollHeight;
        for (const sel of ['[data-e2e="search-common-infinite-scroll"]', '[class*="InfiniteScroll"]', '[class*="LoadMore"]']) {
          const el = document.querySelector(sel);
          if (el) { el.scrollIntoView({ behavior: "instant", block: "center" }); break; }
        }
        window.scrollTo(0, Math.max(document.body.scrollHeight, (idx + 1) * 3000));
      }, s);
      await humanDelay(3000, 4500);
      if (allVideos.length > prev) { noNew = 0; hasMoreFalseRetries = 0; }
      else { noNew++; if (noNew >= 5) { log("no new data x5, stop"); break; } if (noNew >= 2) await humanDelay(2000, 3000); }
    }

    return allVideos.slice(0, maxVideos);
  } finally {
    await page.close();
    await context.close();
  }
}

// ---- main ----
async function main() {
  if (!args.query) {
    process.stdout.write(JSON.stringify({ ok: false, error: "query が空です" }));
    process.exit(1);
  }
  let browser;
  const result = { ok: false, query: args.query, type: args.type, count: 0, videos: [], error: null };
  try {
    const chrome = findChrome();
    log(`launch chrome: ${chrome} (headless=${!args.headful})`);
    const chromeArgs = [
      "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
      "--disable-gpu", "--window-size=1280,900", "--lang=ja-JP",
    ];
    if (process.env.PROXY_SERVER) {
      chromeArgs.push(`--proxy-server=${process.env.PROXY_SERVER}`);
      if (process.env.PROXY_KYC_VERIFIED !== "true") chromeArgs.push("--ignore-certificate-errors");
    }
    browser = await puppeteer.launch({
      executablePath: chrome,
      headless: !args.headful,
      args: chromeArgs,
    });
    let videos = await searchOnce(browser, args.query, args.type, args.max);

    // タグページ (hashtag) は challenge API が発火せず空振りしやすい。
    // 空なら検索エンドポイント (keyword) にフォールバックする (検索 API は安定)。
    if (videos.length === 0 && args.type === "hashtag") {
      log("hashtag 空振り → keyword 検索にフォールバック");
      videos = await searchOnce(browser, args.query, "keyword", args.max);
      if (videos.length > 0) result.type = "keyword(fallback)";
    }

    result.ok = videos.length > 0;
    result.count = videos.length;
    result.videos = videos;
    if (videos.length === 0) result.error = "動画を取得できませんでした (captcha/地域制限/0件の可能性)";
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

main();
