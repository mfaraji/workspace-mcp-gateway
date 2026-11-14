import { JSDOM } from 'jsdom';
import fs from 'fs';
import assert from 'assert';

const html = fs.readFileSync(process.argv[2] + '/gmail.html', 'utf8');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const MESSAGES = [
  { id: 'm1', thread_id: 't1', snippet: 'Invoice attached', label_ids: ['L'],
    headers: { From: 'Ada Lovelace <ada@example.com>', To: 'me@example.com', Subject: 'Invoice <b>June</b>', Date: 'Mon, 1 Jun 2026 10:00:00 +0000' } },
  { id: 'm2', thread_id: 't2', snippet: 'Lunch?', label_ids: ['L'],
    headers: { From: 'bob@example.com', Subject: 'Lunch', Date: 'Tue, 2 Jun 2026 10:00:00 +0000' } }
];
const FULL = {
  m1: { ...MESSAGES[0], body_text: 'Hello\n<img src=x onerror="window.pwned=1">', body_html: '<p>Hello</p><script>window.pwned=1</script>', truncated: false,
    attachments: [{ filename: 'invoice.pdf', mime_type: 'application/pdf', size: 10 }] },
  m2: { ...MESSAGES[1], body_text: 'Lunch at noon?', body_html: '', truncated: false, attachments: [] }
};

function boot({ locale = 'en-US', theme = 'light', initial, drafts = [] }) {
  const calls = [];
  const sizes = [];
  let window;
  const db = { drafts: drafts.map((d) => ({ ...d })) };
  const host = {
    postMessage(msg) {
      if (msg.method === 'ui/initialize') {
        reply(msg.id, { protocolVersion: '2026-01-26', hostInfo: { name: 'test' }, hostCapabilities: {},
          hostContext: { theme, locale, toolInfo: { tool: { name: 'google_gmail_search_messages' } } } });
      } else if (msg.method === 'ui/notifications/initialized') {
        send({ method: 'ui/notifications/tool-input', params: { arguments: {} } });
        send({ method: 'ui/notifications/tool-result', params: { content: [{ type: 'text', text: '{}' }], structuredContent: initial } });
      } else if (msg.method === 'ui/notifications/size-changed') {
        sizes.push(msg.params);
      } else if (msg.method === 'tools/call') {
        calls.push(msg.params);
        const { name, arguments: a } = msg.params;
        const ok = (obj) => reply(msg.id, { content: [{ type: 'text', text: JSON.stringify(obj) }], structuredContent: obj });
        const err = (text) => reply(msg.id, { isError: true, content: [{ type: 'text', text }] });
        if (name === 'google_gmail_search_messages') ok({ messages: MESSAGES, next_page_token: null });
        else if (name === 'google_gmail_get_message') ok(FULL[a.message_id]);
        else if (name === 'google_gmail_get_thread') ok({ thread_id: a.thread_id, messages: [FULL.m1, FULL.m2] });
        else if (name === 'google_gmail_list_drafts') ok({ drafts: db.drafts });
        else if (name === 'google_gmail_create_draft') {
          if (globalThis.FAIL) return err('invalid email address');
          db.drafts.push({ draft_id: 'd' + db.drafts.length, headers: { To: a.to.join(', '), Subject: a.subject }, snippet: a.body_text });
          ok({ status: 'draft_created', draft_id: 'dn' });
        } else if (name === 'google_gmail_update_draft') ok({ status: 'draft_updated', draft_id: a.draft_id });
        else if (name === 'google_gmail_delete_draft') {
          if (globalThis.FAIL) return err('boom');
          db.drafts = db.drafts.filter((d) => d.draft_id !== a.draft_id);
          ok({ status: 'draft_deleted' });
        } else err('unknown tool');
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
  return { window: dom.window, document: dom.window.document, calls, sizes, db };
}

const click = (el) => el.dispatchEvent(new el.ownerDocument.defaultView.MouseEvent('click', { bubbles: true }));
const by = (d, sel, text) => [...d.querySelectorAll(sel)].find((el) => el.textContent.includes(text));
const type = (el, value) => { el.value = value; el.dispatchEvent(new el.ownerDocument.defaultView.Event('input', { bubbles: true })); };

async function run() {
  // 1. Message list -> reader (text only, no HTML executed) -> reply draft -> saved list.
  let t = boot({ initial: { messages: MESSAGES, next_page_token: null } });
  await sleep(50);
  let d = t.document;
  assert.equal(d.querySelectorAll('.row').length, 2, 'two messages');
  assert.match(d.getElementById('root').textContent, /Ada Lovelace/);
  assert.ok(!d.getElementById('root').textContent.includes('ada@example.com>'), 'sender shown as a name');
  assert.ok(d.getElementById('root').textContent.includes('Invoice <b>June</b>'), 'subject is shown as literal text');
  assert.equal(d.querySelector('.subject b'), null, 'no markup injected from subject');
  assert.ok(t.sizes.length > 0, 'reported size');

  click(d.querySelectorAll('.row')[0]);
  await sleep(30);
  assert.deepEqual(t.calls.at(-1), { name: 'google_gmail_get_message', arguments: { message_id: 'm1' } });
  assert.match(d.querySelector('.bodytext').textContent, /Hello/);
  assert.equal(d.querySelector('.bodytext img'), null, 'body_text is not parsed as HTML');
  assert.equal(d.querySelector('script:not([src])') && t.window.pwned, undefined, 'no script from body_html ran');
  assert.ok(!d.getElementById('root').textContent.includes('<p>Hello</p>'), 'body_html never shown');
  assert.match(d.getElementById('root').textContent, /invoice\.pdf/);
  assert.ok(!by(d, 'button', 'Send'), 'there is no send button');

  click(by(d, 'button', 'Whole thread'));
  await sleep(30);
  assert.equal(d.querySelectorAll('.msg').length, 2, 'thread shows both messages');

  click(by(d, 'button', 'Reply as draft'));
  await sleep(10);
  const fields = d.querySelectorAll('.form input');
  assert.equal(fields[0].value, 'ada@example.com', 'reply prefilled with sender address');
  assert.match(fields[2].value, /^Re: /);
  click(by(d, 'button', 'Save draft'));
  await sleep(10);
  assert.match(d.querySelector('.error').textContent, /Write a message first/, 'client-side body check');
  assert.equal(t.calls.filter((c) => c.name === 'google_gmail_create_draft').length, 0);
  type(d.querySelector('.form textarea'), 'Thanks, will pay.');
  click(by(d, 'button', 'Save draft'));
  await sleep(60);
  const create = t.calls.find((c) => c.name === 'google_gmail_create_draft');
  assert.deepEqual(create.arguments, { to: ['ada@example.com'], cc: [], subject: 'Re: Invoice <b>June</b>', body_text: 'Thanks, will pay.', reply_to_message_id: 'm1' });
  assert.match(d.querySelector('.notice').textContent, /Nothing was sent/);
  assert.equal(d.querySelectorAll('.item').length, 1, 'draft list refreshed');

  // 2. Failure keeps the form open with the error.
  click(by(d, 'button', 'New draft'));
  await sleep(10);
  globalThis.FAIL = true;
  type(d.querySelector('.form textarea'), 'x');
  click(by(d, 'button', 'Save draft'));
  await sleep(40);
  assert.match(d.querySelector('.error').textContent, /invalid email address/);
  assert.ok(d.querySelector('.form'), 'editor stays open after a failed save');
  globalThis.FAIL = false;
  click(by(d, 'button', 'Cancel'));
  await sleep(10);

  // 3. Delete: cancel keeps, confirm deletes, failure rolls back.
  const d1 = [{ draft_id: 'd1', headers: { To: 'a@b.co', Subject: 'Plan' }, snippet: 's' }];
  t = boot({ initial: { drafts: d1 }, drafts: d1 });
  await sleep(50);
  d = t.document;
  assert.equal(d.querySelectorAll('.item').length, 1);
  click(d.querySelector('.icon-btn[aria-label="Delete draft"]'));
  await sleep(10);
  assert.match(d.querySelector('.sheet').textContent, /Delete the draft “Plan”\?/);
  click(d.querySelector('.sheet .btn'));          // Cancel
  await sleep(20);
  assert.equal(t.calls.filter((c) => c.name === 'google_gmail_delete_draft').length, 0, 'cancel does not delete');
  assert.equal(d.querySelectorAll('.item').length, 1);

  globalThis.FAIL = true;
  click(d.querySelector('.icon-btn[aria-label="Delete draft"]'));
  await sleep(10);
  click(d.querySelector('.sheet .btn-danger'));
  await sleep(40);
  assert.equal(d.querySelectorAll('.item').length, 1, 'rolled back after failed delete');
  assert.match(d.querySelector('.error').textContent, /boom/);
  globalThis.FAIL = false;
  click(d.querySelector('.icon-btn[aria-label="Delete draft"]'));
  await sleep(10);
  click(d.querySelector('.sheet .btn-danger'));
  await sleep(40);
  assert.equal(d.querySelectorAll('.item').length, 0, 'deleted');

  // 4. Editing an existing draft: untouched body is not sent; a new body replaces html too.
  const d9 = [{ draft_id: 'd9', headers: { To: 'Cy <cy@x.io>', Subject: 'Old' }, snippet: 's' }];
  t = boot({ initial: { drafts: d9 }, drafts: d9 });
  await sleep(50);
  d = t.document;
  click(d.querySelector('.item .row'));
  await sleep(10);
  assert.equal(d.querySelectorAll('.form input')[0].value, 'cy@x.io');
  type(d.querySelectorAll('.form input')[2], 'New subject');
  click(by(d, 'button', 'Save draft'));
  await sleep(40);
  let upd = t.calls.find((c) => c.name === 'google_gmail_update_draft');
  assert.deepEqual(upd.arguments, { to: ['cy@x.io'], cc: [], subject: 'New subject', draft_id: 'd9' });
  click(d.querySelector('.item .row'));
  await sleep(10);
  type(d.querySelector('.form textarea'), 'Fresh');
  click(by(d, 'button', 'Save draft'));
  await sleep(40);
  upd = t.calls.filter((c) => c.name === 'google_gmail_update_draft').at(-1);
  assert.equal(upd.arguments.body_text, 'Fresh');
  assert.equal(upd.arguments.body_html, '', 'old HTML alternative is dropped');

  // 5. Persian, dark, RTL.
  t = boot({ locale: 'fa-IR', theme: 'dark', initial: { messages: MESSAGES } });
  await sleep(50);
  d = t.document;
  assert.equal(d.documentElement.dir, 'rtl');
  assert.equal(d.documentElement.getAttribute('data-theme'), 'dark');
  assert.match(d.querySelector('h1').textContent, /ایمیل/);
  assert.match(d.getElementById('root').textContent, /پیش‌نویس‌ها/);
  console.log('gmail widget harness: all assertions passed');
}
run().catch((e) => { console.error(e); process.exit(1); });
