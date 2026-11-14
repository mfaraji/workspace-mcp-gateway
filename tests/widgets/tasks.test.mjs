import { JSDOM } from 'jsdom';
import fs from 'fs';
import assert from 'assert';

const html = fs.readFileSync(process.argv[2] + '/tasks.html', 'utf8');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function boot({ locale = 'en-US', theme = 'light', tasks }) {
  const calls = [];
  const sizes = [];
  let window;
  const db = tasks.map((t) => ({ ...t }));
  const host = {
    postMessage(msg) {
      if (msg.method === 'ui/initialize') {
        reply(msg.id, { protocolVersion: '2026-01-26', hostInfo: { name: 'test' }, hostCapabilities: {},
          hostContext: { theme, locale, toolInfo: { tool: { name: 'google_tasks_list_tasks' } } } });
      } else if (msg.method === 'ui/notifications/initialized') {
        send({ method: 'ui/notifications/tool-input', params: { arguments: { tasklist_id: 'L1' } } });
        send({ method: 'ui/notifications/tool-result', params: {
          content: [{ type: 'text', text: '{}' }],
          structuredContent: { tasklist_id: 'L1', tasks: db.filter((t) => t.status !== 'completed').map(({ ...t }) => ({ ...t, is_completed: false })), next_page_token: null } } });
      } else if (msg.method === 'ui/notifications/size-changed') {
        sizes.push(msg.params);
      } else if (msg.method === 'tools/call') {
        calls.push(msg.params);
        const { name, arguments: a } = msg.params;
        const ok = (obj) => reply(msg.id, { content: [{ type: 'text', text: JSON.stringify(obj) }], structuredContent: obj });
        if (name === 'google_tasks_update_task' && globalThis.FAIL) {
          reply(msg.id, { isError: true, content: [{ type: 'text', text: 'permission_denied' }] });
        } else if (name === 'google_tasks_update_task') {
          const t = db.find((x) => x.id === a.task_id);
          if (a.status) t.status = a.status;
          if (a.title) t.title = a.title;
          ok({ ...t, is_completed: t.status === 'completed', status: 'updated' });
        } else if (name === 'google_tasks_create_task') {
          const t = { id: 'n' + db.length, title: a.title, status: 'needsAction' };
          db.push(t); ok({ ...t, is_completed: false, status: 'created' });
        } else if (name === 'google_tasks_delete_task') {
          if (!a.confirmation_token) ok({ status: 'confirmation_required', preview: "Delete task 'x'", confirmation_token: 'tok' });
          else { db.splice(db.findIndex((x) => x.id === a.task_id), 1); ok({ status: 'deleted' }); }
        } else if (name === 'google_tasks_list_tasks') {
          ok({ tasklist_id: a.tasklist_id, tasks: db.filter((t) => a.show_completed || t.status !== 'completed').map((t) => ({ ...t, is_completed: t.status === 'completed' })), next_page_token: null });
        } else {
          reply(msg.id, { isError: true, content: [{ type: 'text', text: 'boom' }] });
        }
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
      w.HTMLElement.prototype.scrollIntoView = () => {};
      w.Element.prototype.getBoundingClientRect = () => ({ height: 120, width: 300 });
    }
  });
  return { dom, window: dom.window, document: dom.window.document, calls, sizes, db };
}

async function run() {
  // 1. English render, tick, untick, add, rename, delete with inline confirmation.
  let t = boot({ tasks: [
    { id: 'a', title: 'Pay electricity bill', status: 'needsAction', due: '2020-01-02T00:00:00.000Z', notes: 'Check autopay' },
    { id: 'b', title: 'Book dentist', status: 'needsAction' }] });
  await sleep(50);
  let d = t.document;
  assert.equal(d.documentElement.getAttribute('data-theme'), 'light');
  assert.equal(d.documentElement.dir, 'ltr');
  assert.equal(d.querySelectorAll('li.task').length, 2, 'two tasks rendered');
  assert.match(d.querySelector('.chip.overdue').textContent, /Jan/, 'overdue chip');
  assert.ok(t.sizes.length > 0, 'reported size');

  d.querySelector('li.task .check').click();
  await sleep(30);
  assert.deepEqual(t.calls.at(-1), { name: 'google_tasks_update_task', arguments: { tasklist_id: 'L1', task_id: 'a', status: 'completed' } });
  assert.ok(d.querySelector('li.task.done'), 'optimistic done state');
  d.querySelector('li.task .check').click();
  await sleep(30);
  assert.equal(t.calls.at(-1).arguments.status, 'needsAction', 'untick reopens');

  const input = d.querySelector('.add input');
  input.value = 'Call mom';
  input.dispatchEvent(new t.window.KeyboardEvent('keydown', { key: 'Enter' }));
  await sleep(30);
  assert.equal(t.calls.at(-1).name, 'google_tasks_create_task');
  assert.equal(d.querySelectorAll('li.task').length, 3);

  d.querySelectorAll('li.task .icon-btn')[1].click();   // delete "Book dentist"
  await sleep(30);
  const sheet = d.querySelector('.sheet');
  assert.ok(sheet, 'inline confirmation shown');
  assert.match(sheet.textContent, /Delete “Book dentist”\?/);
  sheet.querySelector('.btn-danger').click();
  await sleep(30);
  const del = t.calls.filter((c) => c.name === 'google_tasks_delete_task');
  assert.equal(del.length, 2, 'handshake: first call, then call with token');
  assert.equal(del[1].arguments.confirmation_token, 'tok');
  assert.equal(d.querySelectorAll('li.task').length, 2);

  // cancel keeps the task
  d.querySelectorAll('li.task .icon-btn')[0].click();
  await sleep(30);
  d.querySelector('.sheet .btn').click();
  await sleep(30);
  assert.equal(d.querySelectorAll('li.task').length, 2, 'cancel keeps task');
  assert.equal(d.querySelector('.error'), null, 'cancel is not an error');

  // 2. Persian, dark, RTL; rollback on error.
  t = boot({ locale: 'fa-IR', theme: 'dark', tasks: [{ id: 'p', title: 'پرداخت قبض برق', status: 'needsAction' }] });
  await sleep(50);
  d = t.document;
  assert.equal(d.documentElement.dir, 'rtl');
  assert.equal(d.documentElement.getAttribute('data-theme'), 'dark');
  assert.match(d.body.textContent, /پرداخت قبض برق/);
  assert.match(d.querySelector('h1').textContent, /کارها/);
  globalThis.FAIL = true;
  d.querySelector('li.task .check').click();
  await sleep(30);
  assert.ok(!d.querySelector('li.task.done'), 'rolled back after failure');
  assert.match(d.querySelector('.error').textContent, /permission_denied/);
  assert.match(d.querySelector('.error').textContent, /انجام نشد/);
  console.log('widget harness: all assertions passed');
}
run().catch((e) => { console.error(e); process.exit(1); });
