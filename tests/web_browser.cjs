/* Optional browser acceptance checks. Product serving needs no Node build. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const engines = require(process.env.EFINANCE_PLAYWRIGHT_MODULE || 'playwright');
const engine = process.env.EFINANCE_BROWSER_ENGINE || 'chromium';
assert.ok(['chromium', 'webkit', 'firefox'].includes(engine), 'Unknown browser engine');

const base = process.env.EFINANCE_TEST_URL || 'http://127.0.0.1:8876';
const output = path.resolve(process.env.EFINANCE_BROWSER_OUTPUT || 'docs');
const report = { checked_at: new Date().toISOString(), engine, source: 'fixed browser fixtures; external requests blocked', checks: [] };
const csv = '\ufeff股票代码,日期,收盘\n000001,2026-10-16,2\n';
const stock = {
  columns: ['股票代码', '日期', '开盘', '收盘', '最高', '最低', '成交量'],
  records: [
    { 股票代码: '000001', 日期: '2026-10-15', 开盘: 1, 收盘: 2, 最高: 3, 最低: .5, 成交量: 100 },
    { 股票代码: '000001', 日期: '2026-10-16', 开盘: 2, 收盘: 3, 最高: 4, 最低: 1, 成交量: 200 }
  ]
};
const fund = { columns: ['日期', '单位净值', '累计净值'], records: [{ 日期: '2026-10-16', 单位净值: 1.2, 累计净值: 2.4 }] };

async function main() {
  await fs.mkdir(path.join(output, 'images'), { recursive: true });
  const launch = { headless: true };
  if (process.env.EFINANCE_BROWSER_EXECUTABLE) launch.executablePath = process.env.EFINANCE_BROWSER_EXECUTABLE;
  else if (process.platform === 'win32' && engine === 'chromium') launch.channel = 'msedge';
  const browser = await engines[engine].launch(launch);
  try {
    for (const width of [320, 390, 1280]) {
      const context = await browser.newContext({ viewport: { width, height: 844 }, acceptDownloads: true });
      const page = await context.newPage();
      const errors = [], external = [];
      page.on('pageerror', error => errors.push(error.message));
      let mode = 'stock', queries = 0, exports = 0, hold = null, entered = null;
      await page.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url());
        if (url.origin !== new URL(base).origin) {
          external.push(url.origin);
          return route.abort();
        }
        if (!url.pathname.startsWith('/api/')) return route.continue();
        if (url.pathname === '/api/export') {
          exports++;
          assert.equal(request.postDataJSON().codes[0], '000001');
          return route.fulfill({ status: 200, contentType: 'text/csv; charset=utf-8',
            headers: { 'Content-Disposition': 'attachment; filename="efinance.csv"' }, body: csv });
        }
        queries++;
        if (entered) entered();
        if (hold) await hold;
        if (mode === 'timeout') return route.fulfill({ status: 504, json: { error: { type: 'TimeoutError', message: 'fixture timeout' } } });
        if (mode === 'limited') return route.fulfill({ status: 429, json: { error: { type: 'RateLimitError', message: 'fixture limit' } } });
        const data = mode === 'fund' ? fund : stock;
        return route.fulfill({ json: { results: [{ code: '000001', ...data,
          records: mode === 'empty' ? [] : data.records, queried_at: '2026-10-19T09:00:00+08:00',
          metadata: { label: mode === 'fund' ? '基金历史净值' : '行情', adjustment: '前复权', units: '固定样本', timezone: 'Asia/Shanghai' } }] } });
      });
      try {
        await page.goto(base);
        await page.locator('#codes').fill('000001');
        await page.locator('#submit').click();
        await page.locator('#status').filter({ hasText: '查询完成' }).waitFor();
        await page.locator('.js-plotly-plot').waitFor();
        assert.equal(await page.locator('tbody tr').count(), 2);
        assert.match(await page.locator('tbody').innerText(), /000001/);
        assert.deepEqual(await page.locator('.js-plotly-plot').evaluate(el => el.data.map(t => t.type)), ['candlestick', 'bar']);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false, `horizontal page overflow at ${width}px`);
        const image = `query-${width < 850 ? 'mobile-' : 'desktop-'}${width}.png`;
        await page.screenshot({ path: path.join(output, 'images', image), fullPage: true });
        const [download] = await Promise.all([page.waitForEvent('download'), page.locator('#download').click()]);
        const bytes = await fs.readFile(await download.path());
        assert.equal(bytes.toString('utf8'), csv);
        assert.equal(exports, 1);
        assert.equal(download.suggestedFilename(), 'efinance.csv');

        mode = 'fund';
        await page.locator('#asset').selectOption('fund');
        assert.equal(await page.locator('#market-options').isVisible(), false);
        await page.locator('#submit').click();
        await page.locator('#status').filter({ hasText: '查询完成' }).waitFor();
        assert.deepEqual(await page.locator('.js-plotly-plot').evaluate(el => el.data.map(t => t.type)), ['scatter']);
        await page.locator('#kind').selectOption('quote');
        assert.equal(await page.locator('#dates').isVisible(), false);
        await page.locator('#kind').selectOption('history');

        mode = 'empty';
        await page.locator('#submit').click();
        await page.getByText('该标的在查询范围内没有数据。').waitFor();
        assert.equal(await page.locator('table').count(), 0);
        for (const [errorMode, message] of [['timeout', '数据源响应超时'], ['limited', '数据源请求受限']]) {
          mode = errorMode;
          await page.locator('#submit').click();
          await page.locator('#status').filter({ hasText: message }).waitFor();
          assert.equal(await page.locator('#download').isDisabled(), true);
        }

        mode = 'stock';
        let release;
        hold = new Promise(resolve => { release = resolve; });
        const started = new Promise(resolve => { entered = resolve; });
        const before = queries;
        await page.locator('#submit').click();
        await started;
        assert.equal(await page.locator('#submit').isDisabled(), true);
        await page.locator('#query-form').evaluate(form => form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
        assert.equal(queries, before + 1);
        release(); hold = null; entered = null;
        await page.locator('#status').filter({ hasText: '查询完成' }).waitFor();
        assert.deepEqual(errors, []);
        assert.deepEqual(external, []);
        report.checks.push({ viewport: `${width}x844`, status: 'passed', queries, exports,
          checks: ['layout', 'candlestick-volume', 'NAV', 'table-leading-zero', 'empty', 'timeout', 'rate-limit', 'CSV-BOM', 'single-query', 'no-external-resources'], screenshot: `images/${image}` });
      } finally {
        await context.close();
      }
    }
  } finally {
    await browser.close();
  }
}

main().then(async () => {
  await fs.writeFile(path.join(output, 'browser-validation-results.json'), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report, null, 2));
}).catch(error => { console.error(error); process.exitCode = 1; });
