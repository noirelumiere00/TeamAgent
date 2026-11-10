import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { parseArgs, normalizeVideo, parseSearchItem, searchOnce, sales25Result } from "./search.mjs";

const fixture = (name) => JSON.parse(readFileSync(new URL(`./fixtures/anonymous-${name}.json`, import.meta.url)));
const saved = fixture("search-response");
const empty = fixture("empty-response");
const refused = fixture("refused-response");
const now = new Date("2026-10-23T23:45:00.000Z");
const cli = (...argv) => parseArgs(["node", "search.mjs", ...argv]);

function browserFixture(initial, { scroll = [], captcha = false, cdn = false, ssr = null, container = false, height = 1000 } = {}) {
  const calls = { contexts: 0, navigations: [], scrolls: 0, moves: [], waits: [], bodiesProcessed: 0, pageClosed: false, contextClosed: false };
  let responseHandler;
  const emit = (responses) => {
    for (const entry of responses) {
      responseHandler({
        url: () => "https://www.tiktok.com/api/search/general/",
        status: () => entry.httpStatus ?? 200,
        text: async () => {
          if (entry.slow) await new Promise((resolve) => setTimeout(resolve, 5));
          calls.bodiesProcessed++;
          return typeof entry.body === "string" ? entry.body : JSON.stringify(entry.body ?? entry);
        },
      });
    }
  };
  const page = {
    setViewport: async () => {}, setUserAgent: async () => {}, setExtraHTTPHeaders: async () => {},
    setRequestInterception: async () => {}, mouse: { move: async () => {} },
    on: (event, handler) => { if (event === "response") responseHandler = handler; },
    off: () => {},
    goto: async (url) => {
      calls.navigations.push(url);
      if (url.includes("/search?") || url.includes("/tag/")) emit(initial);
      return { status: () => 200 };
    },
    waitForSelector: async () => {},
    evaluate: async (fn, arg) => {
      const source = fn.toString();
      if (source.includes("location.href")) return calls.navigations.length > 1 && (typeof captcha === "function" ? captcha(calls) : captcha);
      if (source.includes("Access Denied")) return calls.navigations.length > 1 && cdn;
      if (source.includes("__UNIVERSAL_DATA_FOR_REHYDRATION__")) return ssr;
      if (source.includes("clientHeight")) {
        const viewport = container ? 600 : 900;
        const scroller = { clientHeight: viewport, scrollHeight: height,
          get scrollTop() { return calls.moves.at(-1) ?? 0; },
          set scrollTop(value) { calls.moves.push(Math.min(value, height - viewport)); } };
        const document = { querySelector: () => container ? scroller : null,
          scrollingElement: container ? {} : scroller };
        const bottom = runInNewContext(`(${source})()`, { document, window: { innerHeight: 900 } });
        emit(scroll[calls.scrolls++] ?? []);
        return bottom;
      }
      if (typeof arg === "number") emit(scroll[calls.scrolls++] ?? []);
      return null;
    },
    close: async () => { calls.pageClosed = true; },
  };
  return {
    calls,
    browser: { createBrowserContext: async () => {
      calls.contexts++;
      return { newPage: async () => page, close: async () => { calls.contextClosed = true; } };
    } },
  };
}

async function capture(initial, options = {}, target = 100, edition = "sales25") {
  const { browser, calls } = browserFixture(initial, options);
  let elapsed = 0;
  const search = await searchOnce(browser, "anonymous", "keyword", target, { edition, clock: () => elapsed,
    delay: async (min, max) => { calls.waits.push({ min, max, processed: calls.bodiesProcessed }); elapsed += min; } });
  const envelope = { ok: search.videos.length > 0, query: "anonymous", type: "keyword", count: search.videos.length,
    videos: search.videos, error: null, errorCode: null, diag: { ...search.diag, sessionsRun: 1 } };
  return { result: edition === "sales25" ? sales25Result(envelope, target, now) : envelope, calls };
}

test("legacy normalization stays byte-for-byte compatible with the saved pre-PR3 output", () => {
  assert.deepEqual(saved.data.map((row) => parseSearchItem(row)), fixture("legacy-videos"));
  assert.equal("edition" in cli("--max", "30"), false);
});

test("sales25 accepts 100, rejects 101/noninteger limits and multiple sessions", () => {
  assert.equal(cli("--edition", "sales25", "--max", "100", "--sessions", "1").max, 100);
  for (const count of ["101", "0", "-1", "3x", "2.5", "NaN"]) {
    assert.throws(() => cli("--edition", "sales25", "--max", count));
  }
  for (const extra of [["--sessions", "2"], ["--sessions", "0"], ["--mode", "download"], ["--type", "other"]]) {
    assert.throws(() => cli("--edition", "sales25", ...extra));
  }
  assert.throws(() => cli("--edition", "unknown"));
  assert.throws(() => cli("--edition"));
  const rejected = spawnSync(process.execPath, [new URL("search.mjs", import.meta.url).pathname,
    "--edition", "sales25", "--max", "101"], { encoding: "utf8" });
  assert.equal(rejected.status, 2);
  assert.equal(JSON.parse(rejected.stdout).errorCode, "TIKTOK_INVALID_ARGUMENT");
});

test("nullable stats distinguish missing, zero, strings, ads and photos", () => {
  const [video, photo, zero] = saved.data.map((row) => parseSearchItem(row, "sales25"));
  assert.equal(video.stats.collectCount, 4);
  assert.equal(video.author.followerCount, 42);
  assert.equal(video.isAd, false);
  assert.equal(photo.stats.playCount, null);
  assert.equal(photo.stats.diggCount, 0);
  assert.equal(photo.author.followerCount, null);
  assert.equal(photo.isAd, true);
  assert.equal(photo.mediaType, "photo");
  assert.equal(photo.imageCount, 2);
  assert.equal(photo.images[1].imageURL.urlList[0], "https://www.tiktok.com/anonymous-photo-2.jpg");
  assert.equal(photo.textLanguage, "en");
  assert.match(photo.url, /\/photo\/7400000000000000002$/);
  assert.deepEqual(photo.missingFields, ["stats.playCount", "stats.commentCount", "stats.shareCount", "author.followerCount", "createTime", "duration"]);
  assert.equal(zero.author.followerCount, 0);
  assert.equal(zero.isAd, null);
  assert(zero.missingFields.includes("isAd"));
  assert(!zero.missingFields.includes("stats.playCount"));
  for (const value of [true, false, -1, 1.5, "1.5", "", "Infinity", Number.MAX_SAFE_INTEGER + 1]) {
    const row = normalizeVideo({ id: "7400000000000000001", stats: { playCount: value } }, "sales25");
    assert.equal(row.stats.playCount, null);
    assert(row.missingFields.includes("stats.playCount"));
  }
});

test("normal short acquisition records partial, timestamps, display order and single-session provenance", async () => {
  const { result, calls } = await capture([saved]);
  assert.equal(result.ok, true);
  assert.equal(result.status, "ok");
  assert.deepEqual(result.acquisition_status, { accepted: true, status: "partial", target: 100, actual: 3, shortfall: 97, stop_reason: "exhausted" });
  assert.equal(result.fetched_at, now.toISOString());
  assert.equal(result.fetched_on, "2026-10-24");
  assert.equal(result.sessions, 1);
  assert.equal(result.order_basis, "search_display_order");
  assert.equal(result.unfiltered, true);
  assert.equal(result.missingness_preserved, true);
  assert.deepEqual(result.videos.map((row) => row.rank_display), [1, 2, 3]);
  assert.equal(calls.contexts, 1);
  assert.equal(calls.navigations.length, 2);
  assert(calls.pageClosed && calls.contextClosed);
});

test("100 target caps a larger page, does not filter ads or reorder by popularity", async () => {
  const response = { ...saved, data: Array.from({ length: 101 }, (_, index) => ({ type: 1,
    item: { ...saved.data[index % 3].item, id: String(7400000000000000000n + BigInt(index)) } })) };
  const { result, calls } = await capture([response]);
  assert.equal(result.count, 100);
  assert.equal(result.acquisition_status.status, "complete");
  assert.equal(result.stop_reason, "capped");
  assert.equal(result.videos[1].isAd, true);
  assert.equal(result.videos[99].rank_display, 100);
  assert.equal(calls.scrolls, 0);
});

test("slow bodies finish in response-event order before capture finalization; duplicate IDs do not reorder", async () => {
  const { result } = await capture([
    { body: { ...saved, has_more: true, data: [saved.data[1]] }, slow: true },
    { body: { ...saved, data: [saved.data[0], saved.data[1]] } },
  ]);
  assert.deepEqual(result.videos.map((row) => row.id), [saved.data[1].item.id, saved.data[0].item.id]);
  assert.deepEqual(result.videos.map((row) => row.rank_display), [1, 2]);
});

test("CAPTCHA and CDN refusal reject already captured rows", async () => {
  for (const options of [{ captcha: true }, { cdn: true }]) {
    const { result } = await capture([saved], options);
    assert.equal(result.ok, false);
    assert.equal(result.status, "failed");
    assert.equal(result.acquisition_status.accepted, false);
    assert.equal(result.count, 3);
    assert.equal(result.error_code, options.cdn ? "TIKTOK_CDN_DENIED" : "TIKTOK_BOT_WALL");
  }
});

test("nonzero, malformed and HTTP-denied API pages reject partial capture", async () => {
  for (const response of [refused, { data: [], has_more: false }, { status_code: 0, has_more: false },
    { status_code: 0, data: [], has_more: "false" }, { body: "bad JSON" },
    { body: empty, httpStatus: 403 },
    { body: "<html>Access Denied</html>", httpStatus: 403 }]) {
    const { result } = await capture([{ ...saved, has_more: true }], { scroll: [[response]] });
    assert.equal(result.acquisition_status.accepted, false);
    assert.equal(result.status, "failed");
    assert.equal(result.count, 3);
    assert.equal(result.error_code, response.httpStatus === 403 ? "TIKTOK_CDN_DENIED" : "TIKTOK_API_ERROR");
  }
});

test("true-empty requires affirmative successful exhausted API evidence", async () => {
  const { result } = await capture([empty]);
  assert.equal(result.ok, true);
  assert.equal(result.status, "observed_zero");
  assert.equal(result.error_code, "TIKTOK_TRULY_EMPTY");
  assert.equal(result.acquisition_status.actual, 0);
  assert.equal(result.stop_reason, "exhausted");
  for (const responses of [[], [refused], [{ ...empty, has_more: true }]]) {
    const { result: failed } = await capture(responses);
    assert.equal(failed.acquisition_status.accepted, false);
    assert.notEqual(failed.error_code, "TIKTOK_TRULY_EMPTY");
  }
});

test("normal no-new partial is accepted; SSR without normal API is not measured zero", async () => {
  const { result } = await capture([{ ...saved, has_more: true }]);
  assert.equal(result.stop_reason, "no_new");
  assert.equal(result.acquisition_status.status, "partial");
  const { result: ssrOnly } = await capture([], { ssr: saved.data });
  assert.equal(ssrOnly.count, 3);
  assert.equal(ssrOnly.acquisition_status.accepted, false);
});

test("edition OFF preserves legacy result and diagnostic keys", async () => {
  const legacy = (await capture([saved], {}, 30, null)).result;
  assert.deepEqual(legacy.videos, fixture("legacy-videos"));
  assert.deepEqual(Object.keys(legacy), ["ok", "query", "type", "count", "videos", "error", "errorCode", "diag"]);
  assert.deepEqual(Object.keys(legacy.diag), ["pagesFetched", "captchaDetected", "gridFound", "ssrCount", "videosFound", "sessionsRun"]);
});

test("sales25 reads page one, moves one viewport at a time, awaits bodies and stops at page-two CAPTCHA", async () => {
  for (const container of [false, true]) {
    const { result, calls } = await capture([{ body: { ...saved, has_more: true }, slow: true }], {
      container, height: 20000,
      // Six viewports with no new rows must not stop traversal before the bottom.
      scroll: [...Array.from({ length: 6 }, () => []), [{ body: "<html>captcha</html>", slow: true }]],
    }, 50);
    const viewport = container ? 600 : 900;
    assert.deepEqual(calls.moves, Array.from({ length: 7 }, (_, i) => (i + 1) * viewport));
    assert.deepEqual(calls.waits.slice(2), [
      { min: 20000, max: 20000, processed: 1 },
      ...Array.from({ length: 6 }, () => ({ min: 10000, max: 14000, processed: 1 })),
    ]);
    assert.equal(result.count, 3);
    assert.equal(result.stop_reason, "captcha");
    assert.equal(result.diag.scrollCount, 7);
    assert.deepEqual(result.diag.pageTimings, [
      { elapsedSeconds: 4, added: 3, accepted: true },
      { elapsedSeconds: 84, added: 0, accepted: false },
    ]);
    assert.equal(result.diagnostics.stop_reason, "captcha");
    assert(calls.pageClosed && calls.contextClosed);
  }
});

test("sales25 bounds scrolling even when no response arrives before the bottom", async () => {
  const { result, calls } = await capture([{ ...saved, has_more: true }], { height: 1000000 });
  assert.equal(calls.scrolls, 40);
  assert.equal(result.stop_reason, "capped");
  assert.equal(result.diagnostics.stop_reason, "scroll_limit");
});
