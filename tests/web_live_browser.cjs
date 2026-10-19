/* Manual browser-to-upstream round trip. No API fixtures or remote assets. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require(process.env.EFINANCE_PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.EFINANCE_TEST_URL || 'http://127.0.0.1:8876';
const output = path.resolve(process.env.EFINANCE_BROWSER_OUTPUT || 'docs');
const report = { checked_at: new Date().toISOString(), source: 'real browser and upstream, no API fixtures', checks: [] };

async function main() {
  await fs.mkdir(path.join(output, 'images'), { recursive: true });
  const browser = await chromium.launch({ headless: true, ...(process.platform === 'win32' ? { channel: 'msedge' } : {}) });
  try {
    for (const [asset, code, trace] of [
      ['stock', '1.600519', 'candlestick'], ['fund', '000001', 'scatter'],
      ['bond', '0.128142', 'candlestick'], ['futures', '113.rbm', 'candlestick']
    ]) {
      const context = await browser.newContext({ viewport: { width: 390, height: 844 }, acceptDownloads: true });
      const page = await context.newPage(), record = { asset, code };
      try {
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.route('**/*', route => new URL(route.request().url()).origin === new URL(base).origin ? route.continue() : route.abort());
        await page.goto(base);
        await page.locator('#asset').selectOption(asset);
        await page.locator('#codes').fill(code);
        await page.locator('#beg').fill('2026-10-01');
        await page.locator('#end').fill('2026-10-16');
        const [response] = await Promise.all([
          page.waitForResponse(r => r.url().endsWith('/api/history'), { timeout: 60000 }),
          page.locator('#submit').click()
        ]);
        assert.equal(response.status(), 200, await response.text());
        const result = (await response.json()).results[0];
        assert.equal(result.code, code);
        assert.ok(result.records.length > 0);
        await page.locator('#status').filter({ hasText: '查询完成' }).waitFor();
        await page.locator('.js-plotly-plot').waitFor();
        assert.equal(await page.locator('tbody tr').count(), result.records.length);
        assert.equal(await page.locator('.js-plotly-plot').evaluate(el => el.data[0].type), trace);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false);
        const image = `query-live-${asset}-mobile.png`;
        await page.screenshot({ path: path.join(output, 'images', image), fullPage: true });
        const [download] = await Promise.all([page.waitForEvent('download', { timeout: 60000 }), page.locator('#download').click()]);
        const bytes = await fs.readFile(await download.path());
        assert.deepEqual([...bytes.subarray(0, 3)], [239, 187, 191]);
        assert.ok(bytes.toString('utf8').includes(code));
        assert.equal(download.suggestedFilename(), 'efinance.csv');
        assert.deepEqual(errors, []);
        Object.assign(record, { status: 'PASS', rows: result.records.length, csv_bytes: bytes.length, screenshot: `images/${image}` });
      } catch (error) {
        Object.assign(record, { status: 'FAIL', error_type: error.name, message: error.message });
      } finally {
        report.checks.push(record);
        console.log(record.status, asset);
        await context.close();
      }
    }
  } finally {
    await browser.close();
  }
  await fs.writeFile(path.join(output, 'live-browser-acceptance-results.json'), JSON.stringify(report, null, 2) + '\n');
  if (report.checks.some(check => check.status !== 'PASS')) process.exitCode = 1;
}
main().catch(error => { console.error(error); process.exitCode = 1; });
