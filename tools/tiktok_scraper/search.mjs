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
import {
  isPublicIp,
  startDnsPinnedProxy,
} from "./dns_pinned_proxy.mjs";

const ACQUIRE_HOST_SUFFIXES = Object.freeze([
  "youtube.com",
  "youtu.be",
  "tiktok.com",
  "instagram.com",
  "instagr.am",
]);
const TIKTOK_HOST_SUFFIXES = ACQUIRE_HOST_SUFFIXES.filter((host) => host === "tiktok.com");

function hostMatches(host, suffixes) {
  return suffixes.some((suffix) => host === suffix || host.endsWith(`.${suffix}`));
}

// ---- 引数パース ----
function parseArgs(argv) {
  const a = {
    mode: "search", // "search" | "comments" | "download"
    query: "",
    type: "keyword",
    max: 10,
    url: "", // comments モードの対象動画 URL
    maxComments: 50,
    maxBytes: 30 * 1024 * 1024,
    out: null,
    headful: false,
    networkGuardSelfTest: false,
    sessions: 1, // 独立セッション数 (>1 で複数回検索→出現頻度ランク=単発失敗に強くする)
  };
  for (let i = 2; i < argv.length; i++) {
    const k = argv[i];
    if (k === "--mode") a.mode = argv[++i];
    else if (k === "--query") a.query = argv[++i];
    else if (k === "--type") a.type = argv[++i];
    else if (k === "--max") a.max = parseInt(argv[++i], 10) || 10;
    else if (k === "--url") a.url = argv[++i];
    else if (k === "--max-comments") a.maxComments = parseInt(argv[++i], 10) || 50;
    else if (k === "--max-bytes") a.maxBytes = parseInt(argv[++i], 10) || a.maxBytes;
    else if (k === "--out") a.out = argv[++i];
    else if (k === "--sessions") a.sessions = Math.max(1, parseInt(argv[++i], 10) || 1);
    else if (k === "--headful") a.headful = true;
    else if (k === "--network-guard-self-test") a.networkGuardSelfTest = true;
  }
  return a;
}

const args = parseArgs(process.argv);
const log = (...m) => console.error("[tiktok]", ...m); // stderr

// ---- Chrome 実行パス自動検出 (Mac/Linux 両対応) ----
import fs from "fs";

async function assertPublicHttps(rawUrl, { tiktokOnly = false } = {}) {
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    throw new Error("invalid network URL");
  }
  if (
    parsed.protocol !== "https:" || parsed.username || parsed.password ||
    (parsed.port && parsed.port !== "443")
  ) throw new Error("only canonical HTTPS network URLs are allowed");
  const host = parsed.hostname.replace(/^\[|\]$/g, "").replace(/\.$/, "").toLowerCase();
  if (
    tiktokOnly &&
    !hostMatches(host, TIKTOK_HOST_SUFFIXES)
  ) throw new Error("TikTok URL is outside the allowlist");
  // DNS resolution and the matching TCP connection are performed atomically
  // by the mandatory local CONNECT proxy. Chromium never resolves this host.
  return parsed.toString();
}

function runNetworkGuardSelfTest() {
  const allowed = ["8.8.8.8", "2606:4700:4700::1111"];
  const blocked = [
    "0.0.0.0",
    "10.0.0.1",
    "100.64.0.1",
    "127.0.0.1",
    "169.254.169.254",
    "192.0.2.1",
    "198.18.0.1",
    "224.0.0.1",
    "255.255.255.255",
    "::",
    "::1",
    "::ffff:127.0.0.1",
    "2001:db8::1",
    "fc00::1",
    "fe80::1",
    "ff02::1",
  ];
  if (!allowed.every(isPublicIp) || blocked.some(isPublicIp)) {
    throw new Error("network guard self-test failed");
  }
  process.stdout.write(JSON.stringify({
    ok: true,
    allowed,
    blocked,
    acquireHostSuffixes: ACQUIRE_HOST_SUFFIXES,
  }));
}

function isCanonicalTikTokUrl(rawUrl) {
  try {
    const parsed = new URL(rawUrl);
    const host = parsed.hostname.replace(/\.$/, "").toLowerCase();
    return (
      parsed.protocol === "https:" &&
      !parsed.username &&
      !parsed.password &&
      (!parsed.port || parsed.port === "443") &&
      hostMatches(host, TIKTOK_HOST_SUFFIXES)
    );
  } catch {
    return false;
  }
}

async function installPageNetworkGuard(page, { blockHeavy = false } = {}) {
  await page.setRequestInterception(true);
  page.on("request", async (req) => {
    try {
      const url = req.url();
      if (url.startsWith("data:") || url.startsWith("blob:")) {
        await req.continue();
        return;
      }
      await assertPublicHttps(url);
      const resourceType = req.resourceType();
      if (
        (blockHeavy && (resourceType === "media" || resourceType === "font")) ||
        url.includes("google-analytics.com") ||
        url.includes("googletagmanager.com") ||
        url.includes("doubleclick.net")
      ) {
        await req.abort();
        return;
      }
      await req.continue();
    } catch {
      await req.abort().catch(() => {});
    }
  });
}

function findChrome() {
  if (process.env.CHROMIUM_PATH && fs.existsSync(process.env.CHROMIUM_PATH)) {
    return process.env.CHROMIUM_PATH;
  }
  const candidates = [
    "/usr/lib/chromium/chromium",
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

// captcha/verify ページの能動検出 (推測でなく実検知)。0件の原因切り分け用。
async function detectBotWall(page) {
  try {
    return await page.evaluate(() => {
      const url = location.href || "";
      const title = document.title || "";
      if (/\/(verify|captcha|security-check)/i.test(url)) return true;
      if (/captcha|verif|セキュリティ|認証|ロボットでは/i.test(title)) return true;
      const sels = [
        "#captcha-verify-page", ".captcha_verify_container", '[id*="captcha" i]',
        '[class*="captcha" i]', '[class*="Captcha"]', '[data-e2e="verify-bar"]',
      ];
      for (const s of sels) { try { if (document.querySelector(s)) return true; } catch (e) {} }
      return false;
    });
  } catch (e) { return false; }
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
    // ダウンロード用 URL（download モードが使う。検索/board には非破壊で追加するだけ）
    playAddr: v.video?.playAddr || "",
    downloadAddr: v.video?.downloadAddr || "",
    bitrateUrls: Array.isArray(v.video?.bitrateInfo)
      ? v.video.bitrateInfo.map((b) => b?.PlayAddr?.UrlList?.[0]).filter(Boolean)
      : [],
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
  let captchaDetected = false;
  let gridFound = false;
  let ssrCount = 0;

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

    // 全 browser request を HTTPS + public DNS に制限する。画像は DOM 高さ維持のため許可。
    await installPageNetworkGuard(page, { blockHeavy: true });

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

    // 初回 Cookie/トークン取得 (ウォームアップ): トップで滞在＋軽いスクロール/マウスで ttwid/msToken を成熟させてから検索へ
    await page.goto("https://www.tiktok.com/", { waitUntil: "domcontentloaded", timeout: 30000 });
    await humanDelay(2500, 4000);
    try {
      await page.evaluate(() => window.scrollTo(0, 600));
      await page.mouse.move(400 + Math.random() * 300, 300 + Math.random() * 200);
    } catch (e) { /* warmup は best-effort */ }
    await humanDelay(1500, 2500);

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
      gridFound = true;
    } catch { log("grid selector timeout, continue with SSR/scroll"); }
    // captcha/verify を能動検出 (0件の原因を推測でなく実検知で切り分け)
    captchaDetected = await detectBotWall(page);
    if (captchaDetected) log("CAPTCHA/verify page detected (bot wall)");
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
            if (v && !allVideos.find((e) => e.id === v.id)) { allVideos.push(v); ssrCount++; }
          }
          log(`SSR extraction: ${allVideos.length} videos (ssr+${ssrCount})`);
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

    const diag = { pagesFetched, captchaDetected, gridFound, ssrCount, videosFound: allVideos.length };
    return { videos: allVideos.slice(0, maxVideos), diag };
  } finally {
    await page.close();
    await context.close();
  }
}

// 1 本の動画 URL からコメントを取得する (コメント API /api/comment/list/ を傍受)。
// 出典: vseo-analytics-web の scrapeTikTokComments を移植。スクロールで追加コメントを誘発。
async function scrapeComments(browser, videoUrl, maxComments) {
  const context = await browser.createBrowserContext();
  const page = await context.newPage();
  const comments = [];
  const seen = new Set();

  try {
    await assertPublicHttps(videoUrl, { tiktokOnly: true });
    await installPageNetworkGuard(page);
    await page.setViewport({ width: 1280, height: 900 });
    await page.setUserAgent(USER_AGENTS[Math.floor(Math.random() * USER_AGENTS.length)]);
    await page.setExtraHTTPHeaders({ "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.7" });

    if (process.env.PROXY_USERNAME && process.env.PROXY_PASSWORD) {
      await page.authenticate({
        username: `${process.env.PROXY_USERNAME}-session-${Date.now()}`,
        password: process.env.PROXY_PASSWORD,
      });
    }

    // コメント API を傍受 (json.comments[].text)
    page.on("response", async (resp) => {
      const url = resp.url();
      if (!url.includes("/api/comment/list/")) return;
      try {
        const text = await resp.text();
        if (!text || text.includes("<html")) return;
        const json = JSON.parse(text);
        for (const c of json?.comments || []) {
          const t = (c?.text || "").trim();
          if (t && !seen.has(t)) {
            seen.add(t);
            comments.push({
              text: t,
              likes: c?.digg_count || 0,
              author: c?.user?.unique_id || c?.user?.nickname || "",
            });
          }
        }
        log(`comment API: +intercepted (total ${comments.length})`);
      } catch {
        /* 非 JSON は無視 */
      }
    });

    log(`goto video: ${videoUrl}`);
    await page.goto(videoUrl, { waitUntil: "domcontentloaded", timeout: 30000 });
    await humanDelay(3000, 4500);

    // コメント欄を読み込ませるためにスクロール (最大 8 回、目標件数まで)
    for (let s = 0; s < 8 && comments.length < maxComments; s++) {
      await page.evaluate(() => {
        const panel = document.querySelector(
          '[data-e2e="comment-list"], [class*="DivCommentListContainer"]',
        );
        if (panel) panel.scrollTop = panel.scrollHeight;
        window.scrollBy(0, 1000);
      });
      await humanDelay(2000, 3500);
    }
    log(`comments complete: ${comments.length}`);
    return comments.slice(0, maxComments);
  } finally {
    await page.close();
    await context.close();
  }
}

// 動画オブジェクト v から DL 候補 URL を集める（playAddr > downloadAddr > bitrate variants）。
function collectPlayAddrs(v, arr) {
  if (!v) return;
  const push = (u) => {
    if (u && typeof u === "string" && u.startsWith("http") && !arr.includes(u)) arr.push(u);
  };
  push(v.playAddr);
  push(v.downloadAddr);
  if (Array.isArray(v.bitrateInfo)) {
    for (const b of v.bitrateInfo) {
      const list = b?.PlayAddr?.UrlList;
      if (Array.isArray(list) && list.length) push(list[list.length - 1]); // 末尾=軽量画質を優先
    }
  }
}

// 1 本の動画 URL から動画バイトを取得して outPath に保存する。
// 検索と同一 Chrome session（ブラウザが署名/Cookie/UA/proxy を自前管理）で playAddr を確定し、
//  (第一) playAddr へ page.goto → response.buffer()  … ナビゲーション＝CORS非該当・最堅牢
//  (第二) goto 全滅時のみ動画ページに戻って fetch → arrayBuffer  … opaque リスク有の最後の手段
async function downloadVideoFromUrl(browser, videoUrl, outPath, maxBytes) {
  const context = await browser.createBrowserContext();
  const page = await context.newPage();
  const playAddrs = [];
  let lastErr = null;
  try {
    await assertPublicHttps(videoUrl, { tiktokOnly: true });
    await installPageNetworkGuard(page);
    await page.setViewport({ width: 1280, height: 900 });
    await page.setUserAgent(USER_AGENTS[Math.floor(Math.random() * USER_AGENTS.length)]);
    await page.setExtraHTTPHeaders({ "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.7" });
    if (process.env.PROXY_USERNAME && process.env.PROXY_PASSWORD) {
      await page.authenticate({
        username: `${process.env.PROXY_USERNAME}-session-${Date.now()}`,
        password: process.env.PROXY_PASSWORD,
      });
    }

    // 動画詳細 API を傍受して playAddr を集める
    page.on("response", async (resp) => {
      const url = resp.url();
      if (!url.includes("/api/item/detail/") && !url.includes("/aweme/v1/")) return;
      try {
        const text = await resp.text();
        if (!text || text.includes("<html")) return;
        const json = JSON.parse(text);
        const v = json?.itemInfo?.itemStruct?.video || json?.aweme_detail?.video || null;
        collectPlayAddrs(v, playAddrs);
      } catch {
        /* 非 JSON は無視 */
      }
    });

    log(`goto video: ${videoUrl}`);
    await page.goto(videoUrl, { waitUntil: "domcontentloaded", timeout: 30000 });
    await humanDelay(2500, 4000);

    // SSR フォールバック（傍受で 0 件のとき埋め込み JSON から）
    if (playAddrs.length === 0) {
      try {
        const ssrVideo = await page.evaluate(() => {
          const el = document.getElementById("__UNIVERSAL_DATA_FOR_REHYDRATION__");
          if (!el?.textContent) return null;
          try {
            const p = JSON.parse(el.textContent);
            const scope = p?.["__DEFAULT_SCOPE__"] || {};
            return scope["webapp.video-detail"]?.itemInfo?.itemStruct?.video || null;
          } catch {
            return null;
          }
        });
        collectPlayAddrs(ssrVideo, playAddrs);
      } catch (e) {
        log("SSR video extraction failed:", e.message);
      }
    }
    if (playAddrs.length === 0) throw new Error("playAddr を取得できませんでした (SSR/API 双方空)");
    log(`playAddr candidates: ${playAddrs.length}`);

    let buf = null;
    let mime = "video/mp4";

    // 第一: tiktok.com origin に留まったまま fetch（ブラウザが Cookie/UA/Referer/proxy を自前付与）。
    // TikTok web プレイヤー自身が同じ署名URLを fetch するため、同origin文脈が最も自然に通る。
    // ※媒体URLへ page.goto すると 'load' を待ち続けてハングするため、ナビゲーションは使わない。
    for (const addr of playAddrs) {
      const got = await page.evaluate(async ({ u, byteLimit }) => {
        try {
          const ctrl = new AbortController();
          const t = setTimeout(() => ctrl.abort(), 25000);
          const r = await fetch(u, { credentials: "include", signal: ctrl.signal });
          clearTimeout(t);
          if (!r.ok) return { ok: false, status: r.status };
          const declared = Number(r.headers.get("content-length") || 0);
          if (declared > byteLimit) return { ok: false, error: "size limit exceeded" };
          if (!r.body) return { ok: false, error: "response body missing" };
          const reader = r.body.getReader();
          const chunks = [];
          let total = 0;
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            total += value.byteLength;
            if (total > byteLimit) {
              await reader.cancel();
              return { ok: false, error: "size limit exceeded" };
            }
            chunks.push(value);
          }
          const bytes = new Uint8Array(total);
          let offset = 0;
          for (const chunk of chunks) {
            bytes.set(chunk, offset);
            offset += chunk.byteLength;
          }
          let s = "";
          const chunk = 0x8000;
          for (let i = 0; i < bytes.length; i += chunk) {
            s += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
          }
          return { ok: true, b64: btoa(s), ct: r.headers.get("content-type") || "" };
        } catch (e) {
          return { ok: false, error: String((e && e.message) || e) };
        }
      }, { u: addr, byteLimit: maxBytes });
      if (got && got.ok && got.b64) {
        const cand = Buffer.from(got.b64, "base64");
        if (cand.length > 0) {
          buf = cand;
          if (got.ct) mime = got.ct;
          log(`downloaded via in-page fetch (${cand.length} bytes)`);
          break;
        }
      } else {
        lastErr = new Error("fetch: " + JSON.stringify(got));
        log("fetch candidate failed:", JSON.stringify(got));
      }
    }

    // 第二: fetch が opaque/失敗のとき、commit 待ちナビゲーション + response.buffer()。
    // waitUntil:"commit" は応答受信時点で解決＝媒体URLで load を待ち続けるハングを避ける。
    if (!buf) {
      for (const addr of playAddrs) {
        try {
          const resp = await page.goto(addr, { waitUntil: "commit", timeout: 25000 });
          const declared = Number(resp?.headers()["content-length"] || 0);
          if (resp && resp.ok() && declared > 0 && declared <= maxBytes) {
            const b = await resp.buffer();
            if (b && b.length > 0 && b.length <= maxBytes) {
              buf = b;
              mime = resp.headers()["content-type"] || mime;
              log(`downloaded via page.goto/commit (${b.length} bytes)`);
              break;
            }
          }
        } catch (e) {
          lastErr = e;
          log("goto candidate failed:", String((e && e.message) || e));
        }
      }
    }
    if (!buf || buf.length === 0) {
      throw new Error("動画バイト取得失敗 " + (lastErr ? String(lastErr.message || lastErr) : ""));
    }
    mime = String(mime).split(";")[0].trim() || "video/mp4"; // charset 等を落とす
    fs.writeFileSync(outPath, buf);
    return { savedTo: outPath, mime, bytes: buf.length };
  } finally {
    await page.close().catch(() => {});
    await context.close().catch(() => {});
  }
}

function buildChromeArgs(pinnedProxyUrl) {
  if (!pinnedProxyUrl.startsWith("http://127.0.0.1:")) {
    throw new Error("DNS-pinned proxy is required");
  }
  if (
    process.env.PROXY_SERVER ||
    process.env.PROXY_USERNAME ||
    process.env.PROXY_PASSWORD
  ) {
    throw new Error("external browser proxy is incompatible with DNS pinning");
  }
  const chromeArgs = [
    "--disable-dev-shm-usage", "--disable-gpu",
    "--disable-features=AsyncDns",
    "--disable-quic",
    "--disable-setuid-sandbox",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--host-resolver-rules=MAP * ~NOTFOUND",
    "--proxy-bypass-list=<-loopback>",
    `--proxy-server=${pinnedProxyUrl}`,
    "--window-size=1280,900", "--lang=ja-JP",
  ];
  return chromeArgs;
}

// ---- main: comments モード ----
async function mainComments() {
  const result = { ok: false, mode: "comments", url: args.url, count: 0, comments: [], error: null };
  if (!isCanonicalTikTokUrl(args.url)) {
    result.error = "有効な TikTok 動画 URL が必要です (--url)";
    process.stdout.write(JSON.stringify(result));
    process.exit(2);
  }
  let browser;
  let pinnedProxy;
  try {
    const chrome = findChrome();
    log(`launch chrome: ${chrome} (headless=${!args.headful})`);
    pinnedProxy = await startDnsPinnedProxy();
    browser = await puppeteer.launch({
      executablePath: chrome,
      headless: !args.headful,
      args: buildChromeArgs(pinnedProxy.url),
    });
    const comments = await scrapeComments(browser, args.url, args.maxComments);
    result.ok = comments.length > 0;
    result.count = comments.length;
    result.comments = comments;
    if (comments.length === 0) {
      result.error = "コメントを取得できませんでした (非公開/0件/captcha の可能性)";
    }
  } catch (e) {
    result.error = String((e && e.message) || e);
    log("ERROR:", result.error);
  } finally {
    if (browser) await browser.close().catch(() => {});
    if (pinnedProxy) await pinnedProxy.close().catch(() => {});
  }
  const out = JSON.stringify(result);
  if (args.out) fs.writeFileSync(args.out, out);
  process.stdout.write(out);
  process.exit(result.ok ? 0 : 2);
}

// ---- main: download モード ----
// バイトは --out（ファイル）に保存し、stdout には薄いメタ JSON のみ出す
// （巨大 base64 で stdout を汚さない＝Python の subprocess capture を膨らませない）。
async function mainDownload() {
  const result = {
    ok: false, mode: "download", url: args.url,
    savedTo: null, mime: null, bytes: 0, error: null,
  };
  if (!isCanonicalTikTokUrl(args.url)) {
    result.error = "有効な TikTok 動画 URL が必要です (--url)";
    process.stdout.write(JSON.stringify(result));
    process.exit(2);
  }
  if (!args.out) {
    result.error = "保存先 --out が必要です (バイトは stdout に載せません)";
    process.stdout.write(JSON.stringify(result));
    process.exit(2);
  }
  let browser;
  let pinnedProxy;
  try {
    const chrome = findChrome();
    log(`launch chrome: ${chrome} (headless=${!args.headful})`);
    pinnedProxy = await startDnsPinnedProxy();
    browser = await puppeteer.launch({
      executablePath: chrome,
      headless: !args.headful,
      args: buildChromeArgs(pinnedProxy.url),
    });
    const dl = await downloadVideoFromUrl(browser, args.url, args.out, args.maxBytes);
    result.ok = dl.bytes > 0;
    result.savedTo = dl.savedTo;
    result.mime = dl.mime;
    result.bytes = dl.bytes;
    if (!result.ok) result.error = "動画バイトを取得できませんでした";
  } catch (e) {
    result.error = String((e && e.message) || e);
    log("ERROR:", result.error);
  } finally {
    if (browser) await browser.close().catch(() => {});
    if (pinnedProxy) await pinnedProxy.close().catch(() => {});
  }
  process.stdout.write(JSON.stringify(result));
  process.exit(result.ok ? 0 : 2);
}

// ---- main ----
async function main() {
  if (args.networkGuardSelfTest) {
    runNetworkGuardSelfTest();
    return;
  }
  if (args.mode === "comments") {
    await mainComments();
    return;
  }
  if (args.mode === "download") {
    await mainDownload();
    return;
  }
  if (!args.query) {
    process.stdout.write(JSON.stringify({ ok: false, error: "query が空です" }));
    process.exit(1);
  }
  let browser;
  let pinnedProxy;
  const result = { ok: false, query: args.query, type: args.type, count: 0, videos: [], error: null, errorCode: null, diag: null };
  try {
    const chrome = findChrome();
    log(`launch chrome: ${chrome} (headless=${!args.headful}) sessions=${args.sessions}`);
    pinnedProxy = await startDnsPinnedProxy();
    browser = await puppeteer.launch({
      executablePath: chrome,
      headless: !args.headful,
      args: buildChromeArgs(pinnedProxy.url),
    });

    let videos = [];
    let diag = { pagesFetched: 0, captchaDetected: false, gridFound: false, ssrCount: 0, sessionsRun: 0 };

    if (args.sessions <= 1) {
      // 単一セッション (既定・低レイテンシ)
      const r = await searchOnce(browser, args.query, args.type, args.max);
      videos = r.videos;
      diag = { ...r.diag, sessionsRun: 1 };
      // タグ空振り → keyword 検索フォールバック (検索 API は安定)
      if (videos.length === 0 && args.type === "hashtag") {
        log("hashtag 空振り → keyword 検索にフォールバック");
        const r2 = await searchOnce(browser, args.query, "keyword", args.max);
        videos = r2.videos;
        diag = { ...r2.diag, sessionsRun: 1 };
        if (videos.length > 0) result.type = "keyword(fallback)";
      }
    } else {
      // 複数セッション順次 → 出現頻度ランク (1回失敗しても他で代替=単発失敗に強い。VSEO の triple search 方式)
      const freq = new Map();
      for (let si = 0; si < args.sessions; si++) {
        if (si > 0) await humanDelay(10000, 20000); // セッション間ギャップ (同一IP連打回避)
        const r = await searchOnce(browser, args.query, args.type, args.max);
        diag.sessionsRun++;
        diag.pagesFetched += r.diag.pagesFetched;
        diag.captchaDetected = diag.captchaDetected || r.diag.captchaDetected;
        diag.gridFound = diag.gridFound || r.diag.gridFound;
        diag.ssrCount += r.diag.ssrCount;
        for (const v of r.videos) {
          const e = freq.get(v.id);
          if (e) { e.count++; } else { freq.set(v.id, { video: v, count: 1 }); }
        }
        log(`session ${si + 1}/${args.sessions}: unique ${freq.size}`);
      }
      videos = [...freq.values()]
        .sort((a, b) => b.count - a.count || (b.video.stats.playCount - a.video.stats.playCount))
        .map((x) => x.video)
        .slice(0, args.max);
    }

    result.ok = videos.length > 0;
    result.count = videos.length;
    result.videos = videos;
    result.diag = diag;
    if (videos.length === 0) {
      // 推測でなく診断で分類: captcha実検知 or 傍受0回 → BOT_WALL / API応答あり0件 → TRULY_EMPTY
      if (diag.captchaDetected) {
        result.errorCode = "TIKTOK_BOT_WALL";
        result.error = "TIKTOK_BOT_WALL: captcha/verify ページを検知 (Bot対策に阻まれた)";
      } else if (diag.pagesFetched === 0) {
        result.errorCode = "TIKTOK_BOT_WALL";
        result.error = "TIKTOK_BOT_WALL: 内部API応答0回 (Bot壁/DC-IP評価/署名欠落の可能性)";
      } else {
        result.errorCode = "TIKTOK_TRULY_EMPTY";
        result.error = "TIKTOK_TRULY_EMPTY: API応答はあるが該当動画0件";
      }
    }
  } catch (e) {
    result.error = String((e && e.message) || e);
    result.errorCode = "TIKTOK_EXCEPTION";
    log("ERROR:", result.error);
  } finally {
    if (browser) await browser.close().catch(() => {});
    if (pinnedProxy) await pinnedProxy.close().catch(() => {});
  }
  const out = JSON.stringify(result);
  if (args.out) fs.writeFileSync(args.out, out);
  process.stdout.write(out);
  process.exit(result.ok ? 0 : 2);
}

main();
