import { JSDOM } from 'jsdom';
import fs from 'fs';
import assert from 'assert';

const html = fs.readFileSync(process.argv[2] + '/wealth.html', 'utf8');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const text = (obj) => ({ content: [{ type: 'text', text: JSON.stringify(obj, null, 2) }], structuredContent: obj });

const ACCOUNTS = [
  { id: 'a1', name: 'TFSA', institution: 'Bank A', subtype: 'TFSA', currency: 'CAD', balance: 25000.5 },
  { id: 'a2', name: 'Brokerage USD', institution: 'Broker B', subtype: 'MARGIN', currency: 'USD', balance: 1200 },
  { id: 'a3', name: 'Visa', institution: 'Bank A', subtype: 'CREDIT', currency: 'CAD', balance: -840.25 },
  { id: 'a4', name: '<img src=x onerror=alert(1)>', currency: 'ZZZ1', balance: 5 }
];
const INVEST = { totalValue: 26200, accounts: [{ id: 'a1', name: 'TFSA' }, { id: 'a2', name: 'Brokerage USD' }],
  allocation: [{ label: 'Equity', percent: 70 }, { label: 'Bonds', percent: 20 }, { label: 'Cash', percent: 10 }] };
const positionsFor = (id) => ({ positions: id === 'a2'
  ? [{ symbol: 'AAPL', quantity: 3, price: 200, marketValue: 600, gain: -20, currency: 'USD' }]
  : [{ symbol: 'XEQT', quantity: 100.5, price: 30, marketValue: 3015, gain: 150, currency: 'CAD' }, { symbol: 'VFV', quantity: 2, marketValue: 220, gain: 0 }],
  summary: { marketValue: id === 'a2' ? 600 : 3235, gain: id === 'a2' ? -20 : 150 } });
const history = (months) => ({ series: Array.from({ length: Math.min(months, 8) }, (_, i) => ({ month: '2026-0' + (i + 1), totalAssets: 1000 + i * 100, totalLiabilities: -200, netWorth: 800 + i * 100 })) });

function boot({ toolName = 'apex_wealth_list_accounts', args = {}, locale = 'en-US', theme = 'light', initial, failTool }) {
  const calls = [];
  const sizes = [];
  let window;
  const host = {
    postMessage(msg) {
      if (msg.method === 'ui/initialize') {
        reply(msg.id, { protocolVersion: '2026-01-26', hostInfo: { name: 'test' }, hostCapabilities: {},
          hostContext: { theme, locale, toolInfo: { tool: { name: toolName } } } });
      } else if (msg.method === 'ui/notifications/initialized') {
        send({ method: 'ui/notifications/tool-input', params: { arguments: args } });
        send({ method: 'ui/notifications/tool-result', params: text(initial ?? { accounts: ACCOUNTS }) });
      } else if (msg.method === 'ui/notifications/size-changed') {
        sizes.push(msg.params);
      } else if (msg.method === 'tools/call') {
        calls.push(msg.params);
        const { name, arguments: a } = msg.params;
        if (failTool === name) return reply(msg.id, { isError: true, content: [{ type: 'text', text: 'permission_denied' }] });
        if (name === 'apex_wealth_list_accounts') reply(msg.id, text({ accounts: ACCOUNTS }));
        else if (name === 'apex_wealth_get_accounts_summary') reply(msg.id, text({ cashCad: 26200, debtCad: -840.25, netLiquidCad: 25359.75 }));
        else if (name === 'apex_wealth_get_investments_summary') reply(msg.id, text(INVEST));
        else if (name === 'apex_wealth_get_positions') reply(msg.id, text(positionsFor(a.account_id)));
        else if (name === 'apex_wealth_get_net_worth_history') reply(msg.id, text(history(a.months)));
        else if (name === 'apex_wealth_list_workspaces') reply(msg.id, text({ workspaces: [{ id: 'w1', name: 'Personal' }, { id: 'w2', name: 'Family' }] }));
        else reply(msg.id, { isError: true, content: [{ type: 'text', text: 'unknown tool' }] });
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
  const dom = new JSDOM(html, {
    runScripts: 'dangerously', pretendToBeVisual: true,
    beforeParse(w) {
      window = w;
      Object.defineProperty(w, 'parent', { value: host });
      w.Element.prototype.getBoundingClientRect = () => ({ height: 120, width: 300 });
    }
  });
  return { window: dom.window, document: dom.window.document, calls, sizes };
}
const click = (el) => el.dispatchEvent(new el.ownerDocument.defaultView.MouseEvent('click', { bubbles: true }));
const named = (calls, n) => calls.filter((c) => c.name === n);
const tab = (d, label) => [...d.querySelectorAll('.tab')].find((b) => b.textContent.startsWith(label));

async function run() {
  // 1. Accounts: grouping, currencies, summary, hostile text, unknown currency code.
  let t = boot({});
  await sleep(80);
  let d = t.document;
  assert.equal(d.documentElement.getAttribute('data-theme'), 'light');
  assert.equal(d.documentElement.dir, 'ltr');
  const groups = [...d.querySelectorAll('.group')].map((g) => g.textContent);
  assert.deepEqual(groups, ['Assets', 'Debts']);
  assert.match(d.body.textContent, /CA\$25,000\.50/);
  assert.match(d.body.textContent, /(?<!CA)\$1,200\.00/, 'USD account keeps its own currency');
  assert.match(d.querySelector('.neg').textContent, /840\.25/, 'liability under debts');
  assert.match(d.body.textContent, /25,359\.75/, 'summary stat shown');
  assert.equal(d.querySelector('img'), null, 'account names are text');
  assert.ok(d.body.textContent.includes('<img src=x onerror=alert(1)>'));
  assert.ok(t.sizes.length > 0, 'reported size');
  assert.ok(d.querySelector('select'), 'workspace picker');
  const sel = d.querySelector('select');
  sel.value = 'w2';
  sel.dispatchEvent(new t.window.Event('change'));
  await sleep(80);
  assert.equal(named(t.calls, 'apex_wealth_list_accounts').at(-1).arguments.workspace_id, 'w2', 'reload uses the new workspace');
  assert.match(d.body.textContent, /Family/);

  // 2. Drill into an investment account -> positions table with that account's filter.
  click([...d.querySelectorAll('button.row')].find((b) => b.textContent.includes('Brokerage USD')));
  await sleep(80);
  assert.deepEqual(named(t.calls, 'apex_wealth_get_positions').at(-1).arguments, { account_id: 'a2', workspace_id: 'w2' });
  assert.equal(d.querySelector('.tab[aria-selected=true]').textContent, 'Investments');
  assert.match(d.querySelector('table').textContent, /AAPL/);
  assert.match(d.querySelector('td.neg').textContent, /20/, 'negative gain styled');
  assert.equal(d.querySelectorAll('.bar span').length, 3, 'allocation bar segments');
  assert.match(d.body.textContent, /Equity 70%/);
  const accSel = d.querySelectorAll('select')[d.querySelectorAll('select').length - 1];
  accSel.value = '';
  accSel.dispatchEvent(new t.window.Event('change'));
  await sleep(80);
  assert.deepEqual(named(t.calls, 'apex_wealth_get_positions').at(-1).arguments, { workspace_id: 'w2' }, 'all accounts = no filter');
  assert.match(d.querySelector('table').textContent, /XEQT/);

  // 3. Net worth chart and range toggle.
  click(tab(d, 'Net worth'));
  await sleep(80);
  assert.deepEqual(named(t.calls, 'apex_wealth_get_net_worth_history')[0].arguments, { months: 12, workspace_id: 'w2' });
  assert.equal(d.querySelectorAll('svg.line circle').length, 8, 'a point per month');
  assert.ok(d.querySelector('svg.line path[stroke]').getAttribute('d').startsWith('M'));
  click([...d.querySelectorAll('.seg .tab')].find((b) => b.textContent.startsWith('24')));
  await sleep(80);
  assert.deepEqual(named(t.calls, 'apex_wealth_get_net_worth_history').at(-1).arguments, { months: 24, workspace_id: 'w2' });
  assert.equal(d.querySelector('.seg [aria-pressed=true]').textContent.startsWith('24'), true);
  click(d.querySelectorAll('svg.line circle')[3]);
  assert.match(d.querySelector('#tip').textContent, /1,100/, 'tapping a point shows its value');

  // 4. Opened by the net-worth tool itself, in Persian + dark.
  t = boot({ toolName: 'apex_wealth_get_net_worth_history', args: { months: 6 }, locale: 'fa-IR', theme: 'dark', initial: history(6) });
  await sleep(80);
  d = t.document;
  assert.equal(d.documentElement.dir, 'rtl');
  assert.equal(d.documentElement.getAttribute('data-theme'), 'dark');
  assert.equal(d.querySelector('.tab[aria-selected=true]').textContent, 'ارزش خالص');
  assert.equal(d.querySelectorAll('svg.line circle').length, 6);
  assert.equal(d.querySelector('.seg [aria-pressed=true]').textContent.startsWith('۶') || d.querySelector('.seg [aria-pressed=true]').textContent.startsWith('6'), true);
  assert.equal(named(t.calls, 'apex_wealth_get_net_worth_history').length, 0, 'uses the result it was given');

  // 5. Errors show and do not blank the card; empty data is handled.
  t = boot({ toolName: 'apex_wealth_get_positions', initial: { positions: [] }, failTool: 'apex_wealth_get_investments_summary' });
  await sleep(80);
  d = t.document;
  assert.match(d.querySelector('.error').textContent, /permission_denied/);
  assert.ok(d.querySelector('.tabs'), 'chrome still rendered after failure');
  t = boot({ toolName: 'apex_wealth_get_net_worth_history', initial: { series: [] } });
  await sleep(80);
  assert.match(t.document.body.textContent, /Nothing here yet/);
  console.log('wealth widget harness: all assertions passed');
}
run().catch((e) => { console.error(e); process.exit(1); });
