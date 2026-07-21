#!/usr/bin/env node

import assert from "node:assert/strict";
import fs from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require(
  "/app/tools/tiktok_scraper/node_modules/playwright-core"
);

const destination = process.argv[2];
assert(destination, "screenshot destination is required");

let blockedRequests = 0;
const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH || "/usr/bin/chromium-browser",
  headless: true,
  chromiumSandbox: true,
  args: ["--disable-gpu", "--disable-dev-shm-usage", "--disable-setuid-sandbox"],
});
try {
  const page = await browser.newPage({ viewport: { width: 640, height: 360 } });
  await page.route("**/*", async (route) => {
    if (/^https?:/.test(route.request().url())) {
      blockedRequests += 1;
    }
    await route.abort();
  });
  await page.setContent(
    "<main style='font:48px sans-serif'>Node Playwright" +
      "<img src='https://example.invalid/no-egress.png'></main>",
    { waitUntil: "domcontentloaded" }
  );
  await page.waitForTimeout(100);
  await page.screenshot({ path: destination, type: "png" });
} finally {
  await browser.close();
}

assert(blockedRequests >= 1, "network route interception did not run");
assert(fs.statSync(destination).size > 1000, "Node screenshot is empty");
process.stdout.write(
  JSON.stringify({
    nodePlaywright: require(
      "/app/tools/tiktok_scraper/node_modules/playwright-core/package.json"
    ).version,
    blockedRequests,
  })
);
