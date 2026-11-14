import { JSDOM } from 'jsdom';
import fs from 'fs';
import assert from 'assert';

const html = fs.readFileSync(process.argv[2] + '/apex.html', 'utf8');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const text = (obj) => ({ content: [{ type: 'text', text: JSON.stringify(obj, null, 2) }], structuredContent: obj });

const CLIENTS = [
  { id: 'c1', name: 'Acme Corp', billingMode: 'TIMESHEET', hourlyRate: 120, chargeHst: 13 },
  { id: 'c2', name: 'Beta Ltd', billingMode: 'DIRECT', hourlyRate: 0, chargeHst: 0 }
];
const SHEETS = [{ id: 't1', clientId: 'c1', periodStart: '2026-10-01', periodEnd: '2026-10-15', notes: 'First half',
  entries: [{ workDate: '2026-10-02', description: 'Design', hours: 6 }, { workDate: '2026-10-03', description: 'Review', hours: 2.5 }] }];

function boot({ toolName = 'apex_list_clients', args = {}, locale = 'en-US', theme = 'light', initial, failTool, rawList }) {
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
        send({ method: 'ui/notifications/tool-result', params: rawList
          ? { content: CLIENTS.map((c) => ({ type: 'text', text: JSON.stringify(c, null, 2) })) }
          : text(initial ?? { clients: CLIENTS }) });
      } else if (msg.method === 'ui/notifications/size-changed') {
        sizes.push(msg.params);
      } else if (msg.method === 'tools/call') {
        calls.push(msg.params);
        const { name, arguments: a } = msg.params;
        if (failTool === name) return reply(msg.id, { isError: true, content: [{ type: 'text', text: 'permission_denied' }] });
        const confirmed = (obj) => (a.confirmation_token ? reply(msg.id, text(obj)) :
          reply(msg.id, text({ status: 'confirmation_required', preview: 'Please confirm ' + name, confirmation_token: 'tok' })));
        if (name === 'apex_list_clients') reply(msg.id, text({ clients: CLIENTS }));
        else if (name === 'apex_list_timesheets') reply(msg.id, text({ timesheets: SHEETS }));
        else if (name === 'apex_get_last_timesheet') reply(msg.id, text({ id: 'tL', periodStart: '2026-10-16', periodEnd: '2026-10-31', entries: [] }));
        else if (name === 'apex_list_workspaces') reply(msg.id, { content: [{ type: 'text', text: JSON.stringify({ id: 'w1', name: 'Main', kind: 'BUSINESS' }) }, { type: 'text', text: JSON.stringify({ id: 'w2', name: 'Side', kind: 'BUSINESS' }) }] });
        else if (name === 'apex_set_workspace') reply(msg.id, text({ status: 'workspace_set', workspace_id: a.workspace_id, name: 'Side' }));
        else if (name === 'apex_update_timesheet' || name === 'apex_create_timesheet' || name === 'apex_create_invoice') confirmed({ id: 'new1' });
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
const setValue = (el, v) => { el.value = v; el.dispatchEvent(new el.ownerDocument.defaultView.Event('input', { bubbles: true })); };
const named = (calls, n) => calls.filter((c) => c.name === n);

async function run() {
  // 1. Clients list from the model-called tool; tolerant of unwrapped list text.
  let t = boot({ rawList: true });
  await sleep(60);
  let d = t.document;
  assert.equal(d.documentElement.getAttribute('data-theme'), 'light');
  assert.equal(d.documentElement.dir, 'ltr');
  assert.equal(d.querySelectorAll('.row').length, 2, 'two clients (list of bare objects parsed)');
  assert.match(d.body.textContent, /Acme Corp/);
  assert.match(d.body.textContent, /\$120\.00/);
  assert.ok(t.sizes.length > 0, 'reported size');
  assert.ok(d.querySelector('select'), 'workspace picker for >1 workspace');
  click(d.querySelector('select')); // no-op
  const sel = d.querySelector('select');
  sel.value = 'w2';
  sel.dispatchEvent(new t.window.Event('change'));
  await sleep(60);
  assert.deepEqual(named(t.calls, 'apex_set_workspace')[0].arguments, { workspace_id: 'w2' });
  assert.match(d.body.textContent, /Side/, 'workspace name shown after switch');

  // 2. New timesheet for a TIMESHEET client: defaults, validation, handshake.
  click(d.querySelectorAll('.row')[0]);
  await sleep(60);
  assert.deepEqual(named(t.calls, 'apex_get_last_timesheet')[0].arguments, { client_id: 'c1' });
  const dates = d.querySelectorAll('input[type=date]');
  assert.equal(dates[0].value, '2026-11-01', 'start defaults to the day after the last period end');
  click([...d.querySelectorAll('button')].find((b) => /File timesheet/.test(b.textContent)));
  await sleep(20);
  assert.match(d.querySelector('.error').textContent, /valid start and end/, 'validation blocks empty form');
  assert.equal(named(t.calls, 'apex_create_timesheet').length, 0, 'nothing sent when invalid');
  const f = () => d.querySelectorAll('input[type=date]');
  setValue(f()[1], '2026-11-15');
  const entry = d.querySelector('.entry');
  setValue(entry.querySelector('input[type=text]'), 'Build widget');
  setValue(entry.querySelector('input[type=number]'), '7.5');
  assert.match(d.querySelector('.total').textContent, /7\.5/);
  click([...d.querySelectorAll('button')].find((b) => /File timesheet/.test(b.textContent)));
  await sleep(60);
  const sheet = d.querySelector('.sheet');
  assert.ok(sheet, 'inline confirmation with the gateway preview');
  assert.match(sheet.textContent, /Please confirm apex_create_timesheet/);
  click(sheet.querySelector('.btn:not(.btn-danger)'));
  await sleep(40);
  assert.equal(named(t.calls, 'apex_create_timesheet').length, 1, 'cancel sends no token call');
  assert.ok(d.querySelector('input[type=date]'), 'form stays after cancel');
  assert.equal(d.querySelector('.error'), null, 'cancel is not an error');
  click([...d.querySelectorAll('button')].find((b) => /File timesheet/.test(b.textContent)));
  await sleep(60);
  click(d.querySelector('.sheet .btn-danger'));
  await sleep(60);
  const created = named(t.calls, 'apex_create_timesheet');
  assert.equal(created.length, 3, 'preview call, cancelled retry preview, then confirmed call');
  assert.equal(created[2].arguments.confirmation_token, 'tok');
  assert.equal(created[2].arguments.period_start, '2026-11-01');
  assert.deepEqual(created[2].arguments.entries, [{ work_date: '2026-11-01', description: 'Build widget', hours: 7.5 }]);
  assert.match(d.querySelector('.note').textContent, /draft invoice/);
  assert.equal(d.querySelector('.card-head h1').textContent, 'Apex billing', 'back on the list');

  // 3. Direct client -> invoice with line items.
  click(d.querySelectorAll('.row')[1]);
  await sleep(40);
  setValue(d.querySelector('.entry.item input[type=text]'), 'Logo design');
  setValue(d.querySelectorAll('.entry.item input[type=number]')[1], '250');
  assert.match(d.querySelector('.total').textContent, /\$250\.00/);
  click([...d.querySelectorAll('button')].find((b) => /Create invoice/.test(b.textContent)));
  await sleep(60);
  click(d.querySelector('.sheet .btn-danger'));
  await sleep(60);
  const inv = named(t.calls, 'apex_create_invoice').at(-1).arguments;
  assert.equal(inv.confirmation_token, 'tok');
  assert.deepEqual(inv.line_items, [{ description: 'Logo design', quantity: 1, rate: 250 }]);
  assert.ok(!('timesheet' in inv), 'exactly one billing payload');

  // 4. Timesheets tab and editing; failure leaves the list untouched (rollback).
  t = boot({ toolName: 'apex_list_timesheets', initial: { timesheets: SHEETS }, failTool: 'apex_update_timesheet' });
  await sleep(60);
  d = t.document;
  assert.equal(d.querySelector('.tab[aria-selected=true]').textContent, 'Timesheets');
  assert.match(d.body.textContent, /8\.5 h/);
  click(d.querySelector('.row'));
  await sleep(20);
  assert.equal(d.querySelectorAll('.entry').length, 2);
  setValue(d.querySelectorAll('.entry input[type=number]')[0], '9');
  click([...d.querySelectorAll('button')].find((b) => /Save changes/.test(b.textContent)));
  await sleep(60);
  assert.match(d.querySelector('.error').textContent, /permission_denied/);
  assert.ok(d.querySelector('.entry'), 'editor keeps the user input after a failure');
  click([...d.querySelectorAll('button')].find((b) => b.textContent === 'Back'));
  await sleep(20);
  assert.match(d.body.textContent, /8\.5 h/, 'list unchanged after failed save');

  // 5. Successful edit via handshake, then list reflects it.
  t = boot({ toolName: 'apex_list_timesheets', initial: { timesheets: SHEETS } });
  await sleep(60);
  d = t.document;
  click(d.querySelector('.row'));
  await sleep(20);
  setValue(d.querySelectorAll('.entry input[type=number]')[0], '9');
  click([...d.querySelectorAll('button')].find((b) => /Save changes/.test(b.textContent)));
  await sleep(60);
  click(d.querySelector('.sheet .btn-danger'));
  await sleep(60);
  const upd = named(t.calls, 'apex_update_timesheet').at(-1).arguments;
  assert.equal(upd.timesheet_id, 't1');
  assert.equal(upd.confirmation_token, 'tok');
  assert.equal(upd.entries[0].hours, 9);
  assert.match(d.body.textContent, /11\.5 h/, 'updated hours shown');

  // 6. Last-timesheet tool opens the editor; fa/RTL/dark; hostile text stays text.
  t = boot({ toolName: 'apex_get_last_timesheet', locale: 'fa-IR', theme: 'dark',
    initial: { id: 'x', periodStart: '2026-10-16', periodEnd: '2026-10-31', notes: '<img src=x onerror=alert(1)>',
      entries: [{ workDate: '2026-10-17', description: '<b>bold</b>', hours: 1 }] } });
  await sleep(60);
  d = t.document;
  assert.equal(d.documentElement.dir, 'rtl');
  assert.equal(d.documentElement.getAttribute('data-theme'), 'dark');
  assert.ok(d.querySelector('.entry'), 'editor opened');
  assert.match(d.querySelector('.btn-primary').textContent, /ذخیره/);
  assert.equal(d.querySelector('img'), null, 'no markup injection');
  assert.equal(d.querySelector('textarea').value, '<img src=x onerror=alert(1)>');
  console.log('apex widget harness: all assertions passed');
}
run().catch((e) => { console.error(e); process.exit(1); });
