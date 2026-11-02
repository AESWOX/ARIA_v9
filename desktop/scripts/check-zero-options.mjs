// DoD "0-OPTIONS": the real front-end, served by the backend, must never trigger
// a CORS preflight (OPTIONS) request. Backend answers OPTIONS from a foreign Origin with 400.
// Usage: node desktop/scripts/check-zero-options.mjs <baseUrl> [waitMs]
// Needs: playwright (installed by the workflow with `npm i --no-save playwright@1.45.0` in desktop/).
import { chromium } from 'playwright';

const base = (process.argv[2] || '').replace(/\/+$/, '');
const waitMs = Number(process.argv[3] || 8000);
if (!base) {
  console.error('usage: node check-zero-options.mjs <baseUrl> [waitMs]');
  process.exit(2);
}

const options = [];
const failed = [];
let total = 0;

const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  page.on('request', (req) => {
    total += 1;
    if (req.method() === 'OPTIONS') options.push(req.url());
  });
  page.on('requestfailed', (req) => {
    failed.push(`${req.method()} ${req.url()} ${req.failure()?.errorText ?? ''}`);
  });
  const resp = await page.goto(`${base}/`, { waitUntil: 'load', timeout: 30000 });
  if (!resp || !resp.ok()) {
    throw new Error(`index page status: ${resp ? resp.status() : 'no response'}`);
  }
  await page.waitForTimeout(waitMs);
} catch (err) {
  console.error(`0-OPTIONS check could not run: ${err && err.message ? err.message : err}`);
  await browser.close();
  process.exit(3);
}
await browser.close();

console.log(`0-OPTIONS: requests seen=${total}, OPTIONS=${options.length}, failed(info only)=${failed.length}`);
for (const f of failed.slice(0, 10)) console.log(`  failed: ${f}`);
if (options.length > 0) {
  for (const u of options) console.error(`  OPTIONS ${u}`);
  console.error('0-OPTIONS FAILED: front-end triggered a CORS preflight');
  process.exit(1);
}
console.log('0-OPTIONS OK');
