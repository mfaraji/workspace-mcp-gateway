import { JSDOM } from 'jsdom';
import fs from 'fs';
import assert from 'assert';

const html = fs.readFileSync(process.argv[2] + '/calendar.html', 'utf8');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const dayKey = (offset = 0) => new Date(Date.now() + offset * 86400000).toISOString().slice(0, 10);

function boot({ locale = 'en-US', theme = 'light', timeZone = 'UTC', events, firstResult }) {
  const calls = [];
  const links = [];
  const sizes = [];
  let window;
  let fail = false;
  const db = events.map((e) => ({ ...e }));
  const trim = (e) => ({ id: e.id, summary: e.summary, start: e.start, end: e.end, location: e.location, attendees_count: 0, html_link: e.html_link, status: 'confirmed' });
  const host = {
    postMessage(msg) {
      if (msg.method === 'ui/initialize') {
        reply(msg.id, { protocolVersion: '2026-01-26', hostInfo: { name: 'test' }, hostCapabilities: {},
          hostContext: { theme, locale, timeZone, toolInfo: { tool: { name: 'google_calendar_list_events' } } } });
      } else if (msg.method === 'ui/notifications/initialized') {
        send({ method: 'ui/notifications/tool-input', params: { arguments: { calendar_id: 'primary' } } });
        const sc = firstResult ? firstResult(db, trim) : { calendar_id: 'primary', events: db.map(trim) };
        send({ method: 'ui/notifications/tool-result', params: { content: [{ type: 'text', text: '{}' }], structuredContent: sc } });
      } else if (msg.method === 'ui/notifications/size-changed') {
        sizes.push(msg.params);
      } else if (msg.method === 'ui/open-link') {
        links.push(msg.params.url);
        reply(msg.id, {});
      } else if (msg.method === 'tools/call') {
        calls.push(msg.params);
        const { name, arguments: a } = msg.params;
        const ok = (obj) => reply(msg.id, { content: [{ type: 'text', text: JSON.stringify(obj) }], structuredContent: obj });
        const needsToken = () => { if (!a.confirmation_token) { ok({ status: 'confirmation_required', preview: 'preview', confirmation_token: 'tok' }); return true; } return false; };
        if (name === 'google_calendar_list_calendars') {
          ok({ calendars: [{ id: 'primary', summary: 'Me', primary: true }, { id: 'work', summary: 'Work', primary: false }] });
        } else if (name === 'google_calendar_list_events') {
          ok({ calendar_id: a.calendar_id, events: db.map(trim) });
        } else if (name === 'google_calendar_get_event') {
          const e = db.find((x) => x.id === a.event_id);
          ok({ ...trim(e), description: e.description || null, organizer: null });
        } else if (name === 'google_calendar_delete_event') {
          if (needsToken()) return;
          db.splice(db.findIndex((x) => x.id === a.event_id), 1);
          ok({ status: 'deleted', event_id: a.event_id, calendar_id: a.calendar_id });
        } else if (name === 'google_calendar_update_event') {
          if (needsToken()) return;
          if (fail) return reply(msg.id, { isError: true, content: [{ type: 'text', text: 'permission_denied' }] });
          const e = db.find((x) => x.id === a.event_id);
          if (a.summary) e.summary = a.summary;
          if (a.start) { e.start = a.start.length === 10 ? { date: a.start } : { dateTime: a.start + 'Z' }; e.end = a.end.length === 10 ? { date: a.end } : { dateTime: a.end + 'Z' }; }
          ok({ status: 'updated', ...trim(e) });
        } else if (name === 'google_calendar_create_event') {
          if (needsToken()) return;
          const e = { id: 'n' + db.length, summary: a.summary, location: a.location,
            start: a.start.length === 10 ? { date: a.start } : { dateTime: a.start + 'Z' },
            end: a.end.length === 10 ? { date: a.end } : { dateTime: a.end + 'Z' } };
          db.push(e);
          ok({ status: 'created', ...trim(e) });
        } else {
          reply(msg.id, { isError: true, content: [{ type: 'text', text: 'unknown tool' }] });
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
      w.Element.prototype.getBoundingClientRect = () => ({ height: 200, width: 320 });
    }
  });
  return { window: dom.window, document: dom.window.document, calls, links, sizes, db, setFail: (v) => { fail = v; } };
}

const buttonByText = (d, text) => [...d.querySelectorAll('button')].find((b) => b.textContent.trim() === text);
const today = dayKey(0);
const tomorrow = dayKey(1);
const mkEvents = () => [
  { id: 'e1', summary: 'Team standup', location: 'Room 4', description: 'Daily sync\nbring notes', html_link: 'https://calendar.google.com/e1',
    start: { dateTime: today + 'T10:00:00Z' }, end: { dateTime: today + 'T11:00:00Z' } },
  { id: 'e2', summary: 'Holiday', start: { date: today }, end: { date: tomorrow } },
  { id: 'e3', summary: 'Dentist', start: { dateTime: tomorrow + 'T09:30:00Z' }, end: { dateTime: tomorrow + 'T10:00:00Z' } }
];

async function run() {
  // 1. English / UTC render.
  let t = boot({ events: mkEvents() });
  await sleep(60);
  let d = t.document;
  assert.equal(d.documentElement.dir, 'ltr');
  assert.equal(d.querySelectorAll('.day').length, 2, 'two day groups');
  assert.match(d.querySelector('.day').textContent, /today/i, 'relative day label');
  const rows = [...d.querySelectorAll('li.ev')];
  assert.equal(rows.length, 3);
  assert.match(rows[0].textContent, /All day/, 'all-day sorts first and is labelled');
  assert.match(rows[1].textContent, /10:00/);
  assert.ok(t.sizes.length > 0, 'reported size');
  assert.ok(d.querySelector('select'), 'calendar switcher appears with 2 calendars');
  assert.equal([...d.querySelectorAll('button')].filter((b) => b.textContent.trim() === 'New event').length, 1, 'a single add button');

  // 2. Expand loads details; open link.
  rows[1].querySelector('.row').click();
  await sleep(40);
  assert.equal(t.calls.filter((c) => c.name === 'google_calendar_get_event').length, 1);
  assert.match(d.querySelector('.detail .desc').textContent, /bring notes/);
  assert.match(d.querySelector('.detail').textContent, /Room 4/);
  buttonByText(d, 'Open in Calendar').click();
  await sleep(20);
  assert.deepEqual(t.links, ['https://calendar.google.com/e1']);

  // 3. Edit: only changed fields, confirmation handshake inline, then rollback on failure.
  buttonByText(d, 'Edit').click();
  await sleep(30);
  let form = d.querySelector('form.ev-form');
  assert.ok(form, 'edit form');
  assert.equal(form.elements.namedItem('summary').value, 'Team standup');
  assert.equal(form.elements.namedItem('sTime').value, '10:00');
  form.elements.namedItem('summary').value = 'Standup (moved)';
  form.dispatchEvent(new t.window.Event('submit', { cancelable: true }));
  await sleep(30);
  let sheet = d.querySelector('.sheet');
  assert.ok(sheet, 'inline confirm before saving');
  assert.match(sheet.textContent, /Save “Standup \(moved\)”\?/);
  sheet.querySelector('.btn-danger').click();
  await sleep(40);
  const upd = t.calls.filter((c) => c.name === 'google_calendar_update_event');
  assert.equal(upd.length, 2, 'handshake: first call, then call with token');
  assert.equal(upd[1].arguments.confirmation_token, 'tok');
  assert.equal(upd[1].arguments.summary, 'Standup (moved)');
  assert.equal(upd[1].arguments.start, undefined, 'unchanged times are not sent');
  assert.match(d.body.textContent, /Standup \(moved\)/);

  t.setFail(true);
  buttonByText(d, 'Edit').click();
  await sleep(30);
  form = d.querySelector('form.ev-form');
  form.elements.namedItem('summary').value = 'Should roll back';
  form.dispatchEvent(new t.window.Event('submit', { cancelable: true }));
  await sleep(30);
  d.querySelector('.sheet .btn-danger').click();
  await sleep(40);
  assert.match(d.body.textContent, /Standup \(moved\)/, 'rolled back');
  assert.ok(!/Should roll back/.test(d.body.textContent));
  assert.match(d.querySelector('.error').textContent, /permission_denied/);
  t.setFail(false);

  // 4. Delete with inline confirm; cancel keeps the event.
  buttonByText(d, 'Delete').click();
  await sleep(30);
  d.querySelector('.sheet .btn').click(); // Cancel
  await sleep(30);
  assert.equal(d.querySelectorAll('li.ev').length, 3, 'cancel keeps event');
  buttonByText(d, 'Delete').click();
  await sleep(30);
  assert.match(d.querySelector('.sheet').textContent, /Delete “Standup \(moved\)”\?/);
  d.querySelector('.sheet .btn-danger').click();
  await sleep(40);
  const del = t.calls.filter((c) => c.name === 'google_calendar_delete_event');
  assert.equal(del.length, 3, 'cancelled attempt + handshake pair');
  assert.equal(del[2].arguments.confirmation_token, 'tok');
  assert.equal(d.querySelectorAll('li.ev').length, 2);

  // 5. New timed event, then new all-day event (end is exclusive).
  buttonByText(d, 'New event').click();
  await sleep(20);
  form = d.querySelector('form.ev-form');
  form.elements.namedItem('summary').value = 'Lunch';
  form.elements.namedItem('sDate').value = tomorrow; form.elements.namedItem('eDate').value = tomorrow; form.elements.namedItem('sTime').value = '12:00'; form.elements.namedItem('eTime').value = '13:00';
  form.dispatchEvent(new t.window.Event('submit', { cancelable: true }));
  await sleep(30);
  d.querySelector('.sheet .btn-danger').click();
  await sleep(40);
  const create = t.calls.filter((c) => c.name === 'google_calendar_create_event').at(-1);
  assert.equal(create.arguments.start, tomorrow + 'T12:00:00');
  assert.equal(create.arguments.time_zone, 'UTC');
  assert.equal(create.arguments.confirmation_token, 'tok');
  assert.match(d.body.textContent, /Lunch/);

  buttonByText(d, 'New event').click();
  await sleep(20);
  form = d.querySelector('form.ev-form');
  form.elements.namedItem('summary').value = 'Conference';
  form.elements.namedItem('allDay').checked = true;
  form.elements.namedItem('allDay').dispatchEvent(new t.window.Event('change'));
  form.elements.namedItem('sDate').value = today; form.elements.namedItem('eDate').value = today;
  form.dispatchEvent(new t.window.Event('submit', { cancelable: true }));
  await sleep(30);
  d.querySelector('.sheet .btn-danger').click();
  await sleep(40);
  const allDay = t.calls.filter((c) => c.name === 'google_calendar_create_event').at(-1).arguments;
  assert.equal(allDay.start, today);
  assert.equal(allDay.end, tomorrow, 'single all-day event ends the next day (exclusive)');
  assert.equal(allDay.time_zone, undefined);

  // 6. Empty title is rejected client-side.
  buttonByText(d, 'New event').click();
  await sleep(20);
  form = d.querySelector('form.ev-form');
  const before = t.calls.length;
  form.dispatchEvent(new t.window.Event('submit', { cancelable: true }));
  await sleep(20);
  assert.equal(t.calls.length, before, 'no call for an empty title');
  assert.match(d.querySelector('.error').textContent, /title/i);
  buttonByText(d, 'Cancel').click();

  // 7. Week navigation asks for a bounded window.
  buttonByText(d, 'Later').click();
  await sleep(40);
  const nav = t.calls.filter((c) => c.name === 'google_calendar_list_events').at(-1).arguments;
  assert.equal(nav.include_past, true);
  assert.ok(nav.time_min && nav.time_max && nav.time_min < nav.time_max, 'bounded window');
  assert.ok(buttonByText(d, 'Today'), 'Today button after navigating');

  // 8. A single event produced by the model (create/get) opens the agenda around it.
  t = boot({ events: mkEvents(), firstResult: (db, trim) => ({ status: 'created', ...trim(db[0]) }) });
  await sleep(80);
  d = t.document;
  assert.ok(d.querySelectorAll('li.ev').length >= 1);
  assert.ok(d.querySelector('.detail'), 'the event is expanded');

  // 9. Persian, Asia/Tehran, dark, RTL.
  t = boot({ locale: 'fa-IR', theme: 'dark', timeZone: 'Asia/Tehran', events: mkEvents() });
  await sleep(60);
  d = t.document;
  assert.equal(d.documentElement.dir, 'rtl');
  assert.equal(d.documentElement.getAttribute('data-theme'), 'dark');
  assert.match(d.querySelector('h1').textContent, /تقویم/);
  assert.match(d.querySelector('.day').textContent, /امروز/);
  assert.match(d.body.textContent, /تمام روز/);
  assert.match(d.body.textContent, /۱۳:۳۰|13:30/, 'times shown in the host time zone (UTC 10:00 -> Tehran 13:30)');
  assert.ok(buttonByText(d, 'رویداد جدید'));
  console.log('calendar widget harness: all assertions passed');
}
run().catch((e) => { console.error(e); process.exit(1); });
