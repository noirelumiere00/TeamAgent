#!/usr/bin/env node
// Selected-post acquisition only. Search behavior stays in search.mjs.
import fs from "node:fs";
import path from "node:path";
import puppeteer from "puppeteer-core";
import { startDnsPinnedProxy } from "./dns_pinned_proxy.mjs";

const [url, outDir, rawLimit] = process.argv.slice(2);
const maxBytes = Number(rawLimit);
const requested = new URL(url);
const route = requested.pathname.match(/^\/@[^/]+\/(video|photo)\/(\d{6,25})$/);
if (requested.protocol !== "https:" || !["www.tiktok.com", "tiktok.com"].includes(requested.hostname)
    || requested.username || requested.password || requested.port || requested.search || requested.hash
    || !route || !Number.isSafeInteger(maxBytes) || maxBytes < 1 || maxBytes > 64 * 1024 * 1024) {
  throw new Error("invalid selected-post request");
}

const address = value => typeof value === "string" ? value : (value?.urlList?.[0] || "");
const https = value => {
  const parsed = new URL(value);
  if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.port) {
    throw new Error("HTTPS resource required");
  }
  return parsed.toString();
};
let browser;
let proxy;
try {
  if (process.env.PROXY_SERVER || process.env.PROXY_USERNAME || process.env.PROXY_PASSWORD) {
    throw new Error("external proxy conflicts with DNS pinning");
  }
  proxy = await startDnsPinnedProxy();
  const executablePath = [process.env.CHROMIUM_PATH, "/usr/lib/chromium/chromium",
    "/usr/bin/chromium", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    .find(candidate => candidate && fs.existsSync(candidate));
  if (!executablePath) throw new Error("Chromium unavailable");
  // Fargate disables user namespaces; isolation is the roleless, nonroot,
  // cap-drop/read-only container, matching the existing search entrypoint.
  browser = await puppeteer.launch({ executablePath, headless: true, args: [
    "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
    "--disable-features=AsyncDns", "--disable-quic",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
    "--proxy-bypass-list=<-loopback>", `--proxy-server=${proxy.url}`,
  ] });
  const page = await browser.newPage();
  await page.setRequestInterception(true);
  page.on("request", async request => {
    try {
      https(request.url());
      if (["media", "font"].includes(request.resourceType())) await request.abort();
      else await request.continue();
    } catch { await request.abort().catch(() => {}); }
  });
  let detail;
  const responses = [];
  page.on("response", response => {
    if (!response.url().includes("/api/item/detail/")) return;
    responses.push((async () => {
      try {
        const size = Number(response.headers()["content-length"]);
        if (!Number.isFinite(size) || size <= 0 || size > 4 * 1024 * 1024) return;
        const body = await response.text();
        if (body.length > 4 * 1024 * 1024) return;
        const item = JSON.parse(body)?.itemInfo?.itemStruct;
        if (String(item?.id) === route[2]) detail = item;
      } catch { /* SSR remains the independent fallback. */ }
    })());
  });
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 });
  await Promise.all(responses);
  if (!detail) {
    detail = await page.evaluate(() => {
      const text = document.getElementById("__UNIVERSAL_DATA_FOR_REHYDRATION__")?.textContent;
      if (!text || text.length > 4 * 1024 * 1024) return null;
      const scope = JSON.parse(text)?.__DEFAULT_SCOPE__;
      return scope?.["webapp.video-detail"]?.itemInfo?.itemStruct
        || scope?.["webapp.photo-detail"]?.itemInfo?.itemStruct || null;
    });
  }
  if (String(detail?.id) !== route[2] || !/^[A-Za-z0-9._]+$/.test(detail?.author?.uniqueId || "")) {
    throw new Error("response identity unavailable or mismatched");
  }
  const mediaType = detail.imagePost ? "photo" : "video";
  if (mediaType !== route[1]) throw new Error("response media type mismatch");
  const returnedUrl = `https://${requested.hostname}/@${detail.author.uniqueId}/${mediaType}/${detail.id}`;
  if (returnedUrl !== url) throw new Error("response canonical URL mismatch");
  const images = detail.imagePost?.images || [];
  if (mediaType === "photo" && (!images.length || images.length > 35)) {
    throw new Error("all-photo count unavailable or exceeds limit");
  }
  fs.mkdirSync(outDir, { mode: 0o700 });
  let total = 0;
  async function save(resource, filename, cap = maxBytes) {
    const checked = https(address(resource));
    const limit = Math.min(cap, maxBytes - total);
    if (limit < 1) throw new Error("selected-post bytes exceed limit");
    const payload = await page.evaluate(async ({ target, limit }) => {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 25000);
      try {
        const response = await fetch(target, { credentials: "include", signal: controller.signal });
        if (!response.ok || !response.body || Number(response.headers.get("content-length")) > limit) {
          throw new Error("resource unavailable or oversized");
        }
        const reader = response.body.getReader();
        const chunks = [];
        let size = 0;
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          size += value.length;
          if (size > limit) { await reader.cancel(); throw new Error("resource size limit"); }
          chunks.push(value);
        }
        const bytes = new Uint8Array(size);
        let position = 0;
        for (const chunk of chunks) { bytes.set(chunk, position); position += chunk.length; }
        let binary = "";
        for (let i = 0; i < bytes.length; i += 0x8000) {
          binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
        }
        return btoa(binary);
      } finally { clearTimeout(timeout); }
    }, { target: checked, limit });
    const bytes = Buffer.from(payload, "base64");
    if (!bytes.length || bytes.length > limit) throw new Error("empty or oversized resource");
    fs.writeFileSync(path.join(outDir, filename), bytes, { flag: "wx", mode: 0o600 });
    total += bytes.length;
    return filename;
  }
  const files = [];
  let audio = null;
  if (mediaType === "photo") {
    for (let index = 0; index < images.length; index++) {
      files.push(await save(images[index].imageURL, `photo-${String(index + 1).padStart(3, "0")}.raw`, 8 * 1024 * 1024));
    }
    if (address(detail.music?.playUrl)) {
      try { audio = await save(detail.music.playUrl, "audio.raw", 16 * 1024 * 1024); }
      catch { /* Unmeasured original audio is explicit in the worker manifest. */ }
    }
  } else {
    files.push(await save(detail.video?.playAddr || detail.video?.downloadAddr, "video.mp4"));
  }
  let cover = null;
  let coverUrl = address(detail.video?.cover);
  const current = new URL(page.url());
  if (!coverUrl && current.origin === requested.origin && current.pathname === requested.pathname) {
    coverUrl = await page.evaluate(() =>
      document.querySelector('meta[property="og:image"]')?.getAttribute("content") || "");
  }
  if (coverUrl) {
    try { cover = await save(coverUrl, "cover.raw", 4 * 1024 * 1024); }
    catch { /* Core cannot claim a visual cover match without this evidence. */ }
  }
  process.stdout.write(JSON.stringify({ ok: true, post_id: String(detail.id), url: returnedUrl,
    media_type: mediaType, duration_sec: mediaType === "video" ? detail.video?.duration : null,
    photo_count: images.length, files, audio, cover }));
} catch {
  process.stdout.write(JSON.stringify({ ok: false, errorCode: "TIKTOK_MEDIA_UNAVAILABLE" }));
  process.exitCode = 2;
} finally {
  if (browser) await browser.close().catch(() => {});
  if (proxy) await proxy.close().catch(() => {});
}
