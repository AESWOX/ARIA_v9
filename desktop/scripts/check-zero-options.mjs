// check-zero-options.mjs — automated 0-OPTIONS check on real front-end traffic.
// Opens the served SPA in headless Chromium (Playwright), captures every network
// request the page makes while loading, and fails (exit 1) if any of them is an
// OPTIONS preflight. Mirrors the manual devtools-Network check from the report.
//
// Usage:
//   node check-zero-options.mjs <baseUrl> [waitMs]
//   e.g. node check-zero-options.mjs http://127.0.0.1:55308 8000

import { chromium } from "playwright";

const baseUrl = process.argv[2];
const waitMs = parseInt(process.argv[3] || "8000", 10);

if (!baseUrl) {
  console.error("usage: node check-zero-options.mjs <baseUrl> [waitMs]");
  process.exit(2);
}

const browser = await chromium.launch();
const page = await browser.newPage();

const requests = [];
const optionsRequests = [];

page.on("request", (req) => {
  requests.push({ method: req.method(), url: req.url() });
  if (req.method().toUpperCase() === "OPTIONS") {
    optionsRequests.push(req.url());
  }
});

console.log(`[playwright] opening ${baseUrl}/ ...`);
await page.goto(`${baseUrl}/`, { waitUntil: "domcontentloaded", timeout: 30000 });
// let the SPA fetch its API calls (auth/me, profiles, dashboard/plugins, ...)
await page.waitForTimeout(waitMs);

const body = await page.evaluate(() => document.body ? document.body.innerText.slice(0, 200) : "(no body)");
console.log(`[playwright] page body head: ${JSON.stringify(body)}`);

const methods = {};
for (const r of requests) {
  methods[r.method] = (methods[r.method] || 0) + 1;
}
console.log(`[playwright] captured ${requests.length} requests: ${JSON.stringify(methods)}`);

if (optionsRequests.length > 0) {
  console.error(`[playwright] FAIL: ${optionsRequests.length} OPTIONS preflight requests:`);
  for (const u of optionsRequests) console.error("   ", u);
  await browser.close();
  process.exit(1);
}

if (requests.length === 0) {
  console.error("[playwright] FAIL: no network requests captured at all — page did not load");
  await browser.close();
  process.exit(1);
}

console.log("[playwright] OK: 0 OPTIONS preflight requests on real front-end traffic");
await browser.close();
process.exit(0);
