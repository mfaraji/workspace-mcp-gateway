import { JSDOM } from 'jsdom';
import fs from 'fs';
import assert from 'assert';

const html = fs.readFileSync(process.argv[2] + '/finance.html', 'utf8');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const CURRENT = { pair: 'USD/IRR', averageBuy: 61000, averageSell: 61500, averageRate: 61250, rowsUsed: 2,
  sources: [{ sourceTitle: 'Alpha', buy: 60900, sell: 61400, thisToBase: 61150, dateTime: '2026-11-14T10:00:00Z' },
    { sourceTitle: 'بنچ', buy: 61100, sell: 61600, thisToBase: 61350, dateTime: '2026-11-14T10:05:00Z' }],
  excluded: [{ sourceTitle: 'Bad', reason: 'outlier' }] };
const hist = (n) => ({ pair: 'USD/IRR', view: n > 200 ? 'all' : '1W',
  points: Array.from({ length: n }, (_, i) => ({ date: new Date(Date.UTC(2026, 10, 1 + i % 28, i % 24)).toISOString(), rate: 60000 + i * 10 })) });

function boot({ locale = 'en-US', theme = 'light', firstResult, failTool }) {
  const calls = [];
  let window;
  const host = {
    postMessage(msg) {
      if (msg.method === 'ui/initialize') {
        reply(msg.id, { protocolVersion: '2026-01-26', hostInfo: { name: 'test' }, hostCapabilities: {},
          hostContext: { theme, locale, timeZone: 'UTC', toolInfo: { tool: { name: 'finance_get_current_rate' } } } });
      } else if (msg.method === 'ui/notifications/initialized') {
        send({ method: 'ui/notifications/tool-input', params: { arguments: { from_currency: 'USD' } } });
        send({ method: 'ui/notifications/tool-result', params: { content: [{ type: 'text', text: '{}' }], structuredContent: firstResult } });
      } else if (msg.method === 'tools/call') {
        calls.push(msg.params);
        const { name, arguments: a } = msg.params;
        const ok = (obj) => reply(msg.id, { content: [{ type: 'text', text: JSON.stringify(obj) }], structuredContent: obj });
        if (failTool === name) return reply(msg.id, { isError: true, content: [{ type: 'text', text: 'upstream_error' }] });
        const pair = `${a.from_currency}/${a.to}`;
        if (name === 'finance_get_current_rate') ok(a.from_currency === 'XXX' ? { pair, noData: true } : { ...CURRENT, pair });
        else if (name === 'finance_get_rate_change') ok({ pair, window: a.window, current: 61250, previous: 60000, change: a.window === 'daily' ? -1250 : 1250, changePercent: a.window === 'daily' ? -2.04 : 2.08, note: 'Average rate only.' });
        else if (name === 'finance_get_rate_history') ok({ ...hist(a.view === '1W' ? 40 : a.limit > 100 ? 120 : 30), pair, view: a.view });
        else if (name === 'finance_list_currencies') ok({ currencies: [{ id: 1, abbreviation: 'USD', title: 'US Dollar' }, { id: 2, abbreviation: 'IRR', title: 'Rial' }, { id: 3, abbreviation: 'XXX', title: 'Nothing' }] });
        else reply(msg.id, { isError: true, content: [{ type: 'text', text: 'unknown' }] });
      }
    }
  };
  function send(msg) {
    const e = new window.Event('message');
    e.data = { jsonrpc: '2.0', ...msg };
    Object.defineProperty(e, 'source', { value: host });
    window.dispatchEvent(e);
  }
  function reply(id, result) { setTimeout(() => send({ id, result }), 0); }
  const dom = new JSDOM(html, { runScripts: 'dangerously', pretendToBeVisual: true,
    beforeParse(w) {
      window = w;
      Object.defineProperty(w, 'parent', { value: host });
      w.Element.prototype.getBoundingClientRect = () => ({ height: 200, width: 600, left: 0 });
    } });
  return { window: dom.window, document: dom.window.document, calls };
}

async function run() {
  // 1. English: current-rate result loads change + history, renders hero, table, chart.
  let t = boot({ firstResult: { ...CURRENT } });
  await sleep(80);
  let d = t.document;
  const names = t.calls.map((c) => c.name).sort();
  assert.deepEqual(names, ['finance_get_rate_change', 'finance_get_rate_history', 'finance_list_currencies'], 'seed skips current, loads the rest');
  assert.match(d.querySelector('.big').textContent, /61,250/);
  assert.match(d.querySelector('.badge.up').textContent, /▲.*2\.08%/);
  assert.equal(d.querySelectorAll('tbody tr').length >= 2, true, 'sources table');
  assert.ok(d.querySelector('details summary').textContent.includes('Excluded'), 'excluded sources');
  assert.ok(d.querySelector('svg path.line'), 'chart line (built with createElementNS)');
  assert.equal(d.querySelector('svg').namespaceURI, 'http://www.w3.org/2000/svg');
  assert.equal(d.documentElement.dir, 'ltr');
  assert.match(d.querySelector('.unit').textContent, /IRR per 1 USD/);
  assert.equal(d.querySelectorAll('.pair select')[0].options.length, 3, 'currency picker from finance_list_currencies');

  // chart tooltip on pointer move
  d.querySelector('svg').dispatchEvent(new t.window.Event('pointermove'));
  assert.match(d.querySelector('.tip').textContent, /60,\d{3}/, 'tooltip falls back to the latest point');
  assert.ok(!d.querySelector('.tip').hidden, 'tooltip shown');

  // range toggle re-calls history with view/limit
  [...d.querySelectorAll('.seg')[1].querySelectorAll('button')].find((b) => b.textContent === '1Y').click();
  await sleep(40);
  const last = t.calls.filter((c) => c.name === 'finance_get_rate_history').at(-1);
  assert.deepEqual([last.arguments.view, last.arguments.limit], ['all', 365]);

  // window toggle -> daily shows a negative change
  [...d.querySelectorAll('.seg')[0].querySelectorAll('button')].find((b) => b.textContent === 'Daily').click();
  await sleep(40);
  assert.ok(d.querySelector('.badge.down'), 'negative change badge');

  // currency picker changes the pair and reloads everything
  const before = t.calls.length;
  const select = d.querySelector('.pair select');
  select.value = 'IRR';
  select.dispatchEvent(new t.window.Event('change'));
  await sleep(60);
  const reload = t.calls.slice(before);
  assert.ok(reload.some((c) => c.name === 'finance_get_current_rate' && c.arguments.from_currency === 'IRR'), 'reload with new from currency');

  // swap: set the quote to USD, then swap -> IRR/USD
  const quote = d.querySelectorAll('.pair select')[1];
  quote.value = 'USD';
  quote.dispatchEvent(new t.window.Event('change'));
  await sleep(60);
  d.querySelector('button[aria-label="Swap"]').click();
  await sleep(60);
  const sw = t.calls.filter((c) => c.name === 'finance_get_current_rate').at(-1).arguments;
  assert.deepEqual([sw.from_currency, sw.to], ['USD', 'IRR']);

  // 2. noData pair.
  t = boot({ firstResult: { pair: 'XXX/IRR', noData: true } });
  await sleep(80);
  assert.match(t.document.body.textContent, /No rate data for this pair/);

  // 3. Persian locale, dark theme, RTL, Persian digits; history-first result seeds history.
  t = boot({ locale: 'fa-IR', theme: 'dark', firstResult: hist(30) });
  await sleep(80);
  d = t.document;
  assert.equal(d.documentElement.dir, 'rtl');
  assert.equal(d.documentElement.getAttribute('data-theme'), 'dark');
  assert.match(d.querySelector('.big').textContent, /[۰-۹]/, 'persian digits');
  assert.match(d.querySelector('.stats').textContent, /خرید/);
  assert.equal(d.querySelector('.chart').style.direction || 'ltr', 'ltr');
  assert.ok(!t.calls.some((c) => c.name === 'finance_get_rate_history'), 'history seeded, not refetched');

  // 4. Error surfaces with localized prefix and no crash.
  t = boot({ locale: 'fa-IR', firstResult: { ...CURRENT }, failTool: 'finance_get_rate_change' });
  await sleep(80);
  assert.match(t.document.querySelector('.error').textContent, /upstream_error/);
  assert.match(t.document.querySelector('.error').textContent, /انجام نشد/);
  console.log('finance widget: all assertions passed');
}
run().catch((e) => { console.error(e); process.exit(1); });
