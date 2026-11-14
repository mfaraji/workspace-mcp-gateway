import { JSDOM } from 'jsdom';
import fs from 'fs';
import assert from 'assert';

const html = fs.readFileSync(process.argv[2] + '/telegram.html', 'utf8');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const DESTS = [
  { alias: 'market-report', title: 'Market Report', username: 'mkt', enabled: true },
  { alias: 'team', title: null, username: 'teamchan', enabled: true }
];

function boot({ locale = 'en-US', theme = 'light', dests = DESTS, sendMode = 'ok' } = {}) {
  const calls = [];
  const sizes = [];
  let window;
  const host = {
    postMessage(msg) {
      if (msg.method === 'ui/initialize') {
        reply(msg.id, { protocolVersion: '2026-01-26', hostInfo: { name: 'test' }, hostCapabilities: {},
          hostContext: { theme, locale, toolInfo: { tool: { name: 'telegram_list_destinations' } } } });
      } else if (msg.method === 'ui/notifications/initialized') {
        send({ method: 'ui/notifications/tool-result', params: { content: [{ type: 'text', text: '{}' }], structuredContent: { destinations: dests } } });
      } else if (msg.method === 'ui/notifications/size-changed') {
        sizes.push(msg.params);
      } else if (msg.method === 'tools/call') {
        calls.push(msg.params);
        const ok = (obj) => reply(msg.id, { content: [{ type: 'text', text: JSON.stringify(obj) }], structuredContent: obj });
        const err = (text) => reply(msg.id, { isError: true, content: [{ type: 'text', text: 'Error executing tool telegram_send_message: ' + text }] });
        if (msg.params.name === 'telegram_list_destinations') ok({ destinations: dests });
        else if (sendMode === 'ok') ok({ status: 'sent', duplicate: false, destination: msg.params.arguments.destination, message_id: '9' });
        else if (sendMode === 'dup') ok({ status: 'sent', duplicate: true, destination: msg.params.arguments.destination });
        else if (sendMode === 'timeout') err('Telegram did not confirm whether the request was delivered.');
        else if (sendMode === 'attempted') err('This Telegram publication was already attempted and will not be retried automatically.');
        else err('Telegram destination is not verified or enabled.');
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
const by = (d, sel, text) => [...d.querySelectorAll(sel)].find((el) => el.textContent.includes(text));
const type = (el, value) => { el.value = value; el.dispatchEvent(new el.ownerDocument.defaultView.Event('input', { bubbles: true })); };
const sends = (t) => t.calls.filter((c) => c.name === 'telegram_send_message');

async function compose(t, text) {
  await sleep(50);
  const d = t.document;
  type(d.querySelector('textarea'), text);
  click(by(d, 'button', 'Send'));
  await sleep(10);
  return d;
}

async function run() {
  // 1. Render, pick, cancel does not send, confirm sends exactly once.
  let t = boot();
  await sleep(50);
  let d = t.document;
  assert.equal(d.querySelectorAll('.dest').length, 2);
  assert.equal(d.querySelector('.dest[aria-checked="true"]').textContent.includes('Market Report'), true, 'first destination preselected');
  assert.equal(by(d, 'button', 'Send').disabled, true, 'send disabled with empty text');
  assert.ok(t.sizes.length > 0, 'reported size');
  click(d.querySelectorAll('.dest')[1]);
  type(d.querySelector('textarea'), 'Hello <b>team</b>');
  assert.equal(by(d, 'button', 'Send').disabled, false);
  click(by(d, 'button', 'Send'));
  await sleep(10);
  const sheet = d.querySelector('.sheet');
  assert.ok(sheet, 'inline confirmation shown');
  assert.match(sheet.textContent, /Send this message to “@teamchan”\?/);
  assert.equal(sends(t).length, 0, 'nothing sent before confirmation');
  click(sheet.querySelector('.btn'));            // Cancel
  await sleep(20);
  assert.equal(sends(t).length, 0, 'cancel does not send');
  assert.equal(d.querySelector('textarea').value, 'Hello <b>team</b>', 'text kept after cancel');

  click(by(d, 'button', 'Send'));
  await sleep(10);
  click(d.querySelector('.sheet .btn-danger'));
  await sleep(40);
  assert.equal(sends(t).length, 1);
  assert.deepEqual(sends(t)[0].arguments, { destination: 'team', text: 'Hello <b>team</b>' });
  assert.match(d.querySelector('.receipt.ok').textContent, /Sent to “@teamchan”/);
  assert.equal(d.querySelector('.preview b'), null, 'message echoed as text');

  // 2. Silent flag and a duplicate receipt.
  t = boot({ sendMode: 'dup' });
  await sleep(50);
  d = t.document;
  click(d.querySelector('input[type=checkbox]'));
  type(d.querySelector('textarea'), 'again');
  click(by(d, 'button', 'Send'));
  await sleep(10);
  click(d.querySelector('.sheet .btn-danger'));
  await sleep(40);
  assert.equal(sends(t)[0].arguments.disable_notification, true, 'silent flag sent when ticked');
  assert.match(d.querySelector('.receipt.ok').textContent, /had already been sent/);

  // 3. Honest unconfirmed delivery vs definite failure.
  for (const mode of ['timeout', 'attempted']) {
    t = boot({ sendMode: mode });
    d = await compose(t, 'maybe');
    click(d.querySelector('.sheet .btn-danger'));
    await sleep(40);
    const box = d.querySelector('.receipt.warn');
    assert.match(box.textContent, /Delivery not confirmed/, mode);
    assert.match(box.textContent, /may or may not be in the channel/);
    assert.ok(!/Sent to/.test(d.getElementById('root').textContent), 'never claims success');
    assert.equal(sends(t).length, 1, 'no automatic retry');
    click(by(d, 'button', 'Edit message'));
    assert.equal(d.querySelector('textarea').value, 'maybe', 'text preserved for editing');
  }
  t = boot({ sendMode: 'forbidden' });
  d = await compose(t, 'nope');
  click(d.querySelector('.sheet .btn-danger'));
  await sleep(40);
  assert.match(d.querySelector('.receipt.warn').textContent, /Not sent/);
  assert.match(d.querySelector('.receipt.warn').textContent, /not verified or enabled/);
  assert.ok(!/Error executing tool/.test(d.getElementById('root').textContent), 'tool prefix stripped');

  // 4. Empty destinations; Persian dark RTL.
  t = boot({ dests: [] });
  await sleep(50);
  assert.match(t.document.getElementById('root').textContent, /No verified destinations/);
  t = boot({ locale: 'fa-IR', theme: 'dark' });
  await sleep(50);
  d = t.document;
  assert.equal(d.documentElement.dir, 'rtl');
  assert.equal(d.documentElement.getAttribute('data-theme'), 'dark');
  assert.match(d.querySelector('h1').textContent, /تلگرام/);
  assert.match(by(d, 'button', 'ارسال').textContent, /ارسال/);
  console.log('telegram widget harness: all assertions passed');
}
run().catch((e) => { console.error(e); process.exit(1); });
