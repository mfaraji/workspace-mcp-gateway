import { JSDOM } from 'jsdom';
import fs from 'fs';
import assert from 'assert';

const html = fs.readFileSync(process.argv[2] + '/drive.html', 'utf8');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const FILES = [
  { id: 'f1', name: 'Budget 2026.xlsx', source_name: 'Budget 2026', mime_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    source_mime_type: 'application/vnd.google-apps.spreadsheet', size: null, modified_time: '2026-11-10T09:00:00Z', web_view_link: 'https://docs.google.com/spreadsheets/d/f1', shared: true, owned_by_me: true },
  { id: 'f2', name: 'scan.pdf', mime_type: 'application/pdf', source_mime_type: 'application/pdf', size: 2621440, modified_time: '2026-11-01T09:00:00Z', web_view_link: 'https://drive.google.com/file/d/f2', shared: false, owned_by_me: false },
  { id: 'f3', name: 'Photos', mime_type: 'application/vnd.google-apps.folder', source_mime_type: 'application/vnd.google-apps.folder', size: null, modified_time: '2026-10-01T09:00:00Z', web_view_link: 'https://drive.google.com/drive/folders/f3' }
];

function boot({ locale = 'en-US', theme = 'light', firstResult }) {
  const calls = [];
  const links = [];
  const sizes = [];
  let window;
  const host = {
    postMessage(msg) {
      if (msg.method === 'ui/initialize') {
        reply(msg.id, { protocolVersion: '2026-01-26', hostInfo: { name: 'test' }, hostCapabilities: {},
          hostContext: { theme, locale, toolInfo: { tool: { name: 'google_drive_search_files' } } } });
      } else if (msg.method === 'ui/notifications/initialized') {
        send({ method: 'ui/notifications/tool-input', params: { arguments: { query: '' } } });
        send({ method: 'ui/notifications/tool-result', params: { content: [{ type: 'text', text: '{}' }],
          structuredContent: firstResult || { files: FILES, next_page_token: 'p2', incomplete_search: false } } });
      } else if (msg.method === 'ui/notifications/size-changed') {
        sizes.push(msg.params);
      } else if (msg.method === 'ui/open-link') {
        links.push(msg.params.url);
        reply(msg.id, {});
      } else if (msg.method === 'tools/call') {
        calls.push(msg.params);
        const a = msg.params.arguments;
        let obj;
        if (a.page_token) obj = { files: [{ id: 'f4', name: 'notes.txt', mime_type: 'text/plain', source_mime_type: 'text/plain', size: 12, modified_time: '2026-09-01T00:00:00Z' }], next_page_token: null };
        else if (a.query === 'boom') return reply(msg.id, { isError: true, content: [{ type: 'text', text: 'drive_unavailable' }] });
        else obj = { files: a.query ? FILES.slice(1, 2) : FILES, next_page_token: null };
        reply(msg.id, { content: [{ type: 'text', text: JSON.stringify(obj) }], structuredContent: obj });
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
      w.Element.prototype.getBoundingClientRect = () => ({ height: 200, width: 320 });
    }
  });
  return { window: dom.window, document: dom.window.document, calls, links, sizes };
}

const buttonByText = (d, text) => [...d.querySelectorAll('button')].find((b) => b.textContent.trim() === text);

async function run() {
  let t = boot({});
  await sleep(60);
  let d = t.document;
  assert.equal(d.documentElement.dir, 'ltr');
  assert.equal(d.documentElement.getAttribute('data-theme'), 'light');
  const rows = [...d.querySelectorAll('li.file')];
  assert.equal(rows.length, 3);
  assert.ok(rows[0].querySelector('.badge.sheet'), 'sheet icon');
  assert.ok(rows[1].querySelector('.badge.pdf'), 'pdf icon');
  assert.ok(rows[2].querySelector('.badge.folder'), 'folder icon');
  assert.match(rows[1].textContent, /2\.5 MB/, 'size formatted');
  assert.match(rows[1].textContent, /Nov/, 'date formatted');
  assert.ok(t.sizes.length > 0, 'reported size');
  assert.match(d.querySelector('h1').textContent, /Recent files/);

  // Details and open-in-Drive via ui/open-link.
  rows[0].querySelector('.row').click();
  await sleep(10);
  assert.match(d.querySelector('.detail').textContent, /Shared/);
  buttonByText(d, 'Open in Drive').click();
  await sleep(20);
  assert.deepEqual(t.links, ['https://docs.google.com/spreadsheets/d/f1']);

  // Search then load more.
  const input = d.querySelector('input[type=search]');
  input.value = 'scan';
  input.dispatchEvent(new t.window.KeyboardEvent('keydown', { key: 'Enter' }));
  await sleep(40);
  assert.equal(t.calls.at(-1).name, 'google_drive_search_files');
  assert.equal(t.calls.at(-1).arguments.query, 'scan');
  assert.equal(d.querySelectorAll('li.file').length, 1);
  assert.match(d.querySelector('h1').textContent, /scan/);

  t = boot({});
  await sleep(60);
  d = t.document;
  buttonByText(d, 'Load more').click();
  await sleep(40);
  assert.equal(d.querySelectorAll('li.file').length, 4, 'next page appended');
  assert.equal(t.calls.at(-1).arguments.page_token, 'p2');
  assert.equal(buttonByText(d, 'Load more'), undefined, 'no more pages');

  // Errors are shown, not thrown.
  const box = d.querySelector('input[type=search]');
  box.value = 'boom';
  box.dispatchEvent(new t.window.KeyboardEvent('keydown', { key: 'Enter' }));
  await sleep(40);
  assert.match(d.querySelector('.error').textContent, /drive_unavailable/);

  // Single file from get_file_metadata opens expanded.
  t = boot({ firstResult: FILES[1] });
  await sleep(60);
  d = t.document;
  assert.equal(d.querySelectorAll('li.file').length, 1);
  assert.ok(d.querySelector('.detail'), 'metadata result is expanded');

  // Persian, dark, RTL; names are text, never markup.
  t = boot({ locale: 'fa-IR', theme: 'dark', firstResult: { files: [{ ...FILES[1], name: '<img src=x onerror=alert(1)>گزارش.pdf' }], next_page_token: null } });
  await sleep(60);
  d = t.document;
  assert.equal(d.documentElement.dir, 'rtl');
  assert.equal(d.documentElement.getAttribute('data-theme'), 'dark');
  assert.match(d.querySelector('h1').textContent, /فایل‌های اخیر/);
  assert.match(d.querySelector('.name .n').textContent, /<img src=x/, 'shown literally');
  assert.equal(d.querySelector('li.file img'), null, 'no injected element');
  assert.ok(buttonByText(d, 'جستجو'));
  console.log('drive widget harness: all assertions passed');
}
run().catch((e) => { console.error(e); process.exit(1); });
