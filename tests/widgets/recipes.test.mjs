import { JSDOM } from 'jsdom';
import fs from 'fs';
import assert from 'assert';

const html = fs.readFileSync(process.argv[2] + '/recipes.html', 'utf8');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const RECIPE = () => ({
  id: 'r1', version: '7', title: 'Ghormeh Sabzi', description: 'A herb stew', language: 'en', cuisine: 'Persian', category: 'main',
  servings: 4, prep_time_minutes: 30, cook_time_minutes: 120, tags: ['stew'], personal_notes: 'Use dried limes', source_url: 'https://example.com/r',
  ingredients: [{ name: 'Beef', quantity: 500, unit: 'g' }, { name: 'Flour', quantity: 1.5, quantity_text: '1 1/2', unit: 'cup' },
    { name: 'Salt', quantity: null, quantity_text: 'a pinch' }],
  instructions: ['Fry the herbs', 'Simmer'], feedback: [{ id: 'f1', rating: 5, comment: 'Great', cooked_on: '2026-10-01' }],
  rating_avg: 5, feedback_count: 1
});

function boot({ locale = 'en-US', theme = 'light', firstResult, args = {}, fail = {} }) {
  const calls = [];
  const db = { r1: RECIPE(), r2: { ...RECIPE(), id: 'r2', title: 'قورمه سبزی', language: 'fa', servings: 6, tags: ['خورش'], feedback: [], rating_avg: null, feedback_count: 0 } };
  let window;
  const host = {
    postMessage(msg) {
      if (msg.method === 'ui/initialize') {
        reply(msg.id, { protocolVersion: '2026-01-26', hostInfo: { name: 'test' }, hostCapabilities: {},
          hostContext: { theme, locale, toolInfo: { tool: { name: 'recipes_list' } } } });
      } else if (msg.method === 'ui/notifications/initialized') {
        send({ method: 'ui/notifications/tool-input', params: { arguments: args } });
        send({ method: 'ui/notifications/tool-result', params: { content: [{ type: 'text', text: '{}' }], structuredContent: firstResult } });
      } else if (msg.method === 'ui/open-link') {
        calls.push({ name: 'open-link', arguments: msg.params }); reply(msg.id, {});
      } else if (msg.method === 'tools/call') {
        calls.push(JSON.parse(JSON.stringify(msg.params)));
        const { name, arguments: a } = msg.params;
        const ok = (obj) => reply(msg.id, { content: [{ type: 'text', text: JSON.stringify(obj) }], structuredContent: obj });
        const err = (text) => reply(msg.id, { isError: true, content: [{ type: 'text', text }] });
        if (fail[name]) return err(fail[name]);
        const summary = (r) => ({ id: r.id, title: r.title, language: r.language, cuisine: r.cuisine, category: r.category, tags: r.tags, servings: r.servings, rating_avg: r.rating_avg });
        if (name === 'recipes_list' || name === 'recipes_search') ok({ items: Object.values(db).map(summary), limit: 20, offset: a.offset || 0, next_offset: null });
        else if (name === 'recipes_get') ok(JSON.parse(JSON.stringify(db[a.recipe_id])));
        else if (name === 'recipes_add_feedback') { const r = db[a.recipe_id]; r.feedback.push({ id: 'new1', rating: a.rating, comment: a.comment || null }); r.rating_avg = 4.5; ok({ recipe_id: a.recipe_id, feedback_id: 'new1', added: true, rating_avg: 4.5, feedback_count: 2, version: '8' }); }
        else if (name === 'recipes_delete_feedback' || name === 'recipes_delete') {
          if (!a.confirmation_token) ok({ status: 'confirmation_required', preview: 'Really?', confirmation_token: 'tok' });
          else if (name === 'recipes_delete') { delete db[a.recipe_id]; ok({ deleted: true, id: a.recipe_id }); }
          else { db[a.recipe_id].feedback = []; ok({ recipe_id: a.recipe_id, deleted: true, rating_avg: null, version: '9' }); }
        } else if (name === 'recipes_update') ok({ id: a.recipe_id, version: '8', updated: true, already_applied: false });
        else if (name === 'recipes_save') { db.r3 = { ...RECIPE(), id: 'r3', title: a.title }; ok({ id: 'r3', version: '1', title: a.title, created: true }); }
        else err('unknown tool');
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
    beforeParse(w) { window = w; Object.defineProperty(w, 'parent', { value: host }); w.Element.prototype.getBoundingClientRect = () => ({ height: 300, width: 400 }); } });
  return { window: dom.window, document: dom.window.document, calls, db };
}
const LIST = { items: [{ id: 'r1', title: 'Ghormeh Sabzi', cuisine: 'Persian', tags: ['stew'], servings: 4, rating_avg: 5 },
  { id: 'r2', title: 'قورمه سبزی', tags: ['خورش'], servings: 6 }], limit: 20, offset: 0, next_offset: null };
const q = (d, sel) => d.querySelector(sel);
const byText = (d, sel, text) => [...d.querySelectorAll(sel)].find((e) => e.textContent.trim() === text || e.textContent.includes(text));
const type = (w, el, value) => { el.value = value; el.dispatchEvent(new w.Event('input')); };

async function run() {
  // 1. List -> detail -> scaler / steps
  let t = boot({ firstResult: LIST });
  await sleep(50);
  let d = t.document;
  assert.equal(d.querySelectorAll('.rc').length, 2, 'two cards');
  assert.equal(d.querySelectorAll('.rc')[1].querySelector('.t').getAttribute('dir'), 'auto', 'dir=auto on titles');
  assert.ok(q(d, '.rate'), 'rating stars on card');
  d.querySelector('.rc').click();
  await sleep(40);
  assert.deepEqual(t.calls.at(-1), { name: 'recipes_get', arguments: { recipe_id: 'r1' } });
  assert.match(q(d, 'h1').textContent, /Ghormeh Sabzi/);
  let ing = [...d.querySelectorAll('ul.ing li')].map((l) => l.textContent);
  assert.match(ing[0], /500\s*g\s*Beef/);
  assert.match(ing[1], /1 1\/2/, 'original text kept at factor 1');
  assert.match(ing[2], /a pinch/, 'unparseable quantity untouched');
  byText(d, 'button', '+').click();               // 4 -> 5 servings
  ing = [...d.querySelectorAll('ul.ing li')].map((l) => l.textContent);
  assert.match(ing[0], /625/, 'scaled 500 -> 625');
  assert.match(ing[1], /1\.88/, 'scaled flour 1.5 -> 1.875');
  assert.match(ing[2], /a pinch/);
  d.querySelector('ol.steps li').click();
  assert.ok(d.querySelector('ol.steps li.done'), 'step checked off');
  assert.equal(d.documentElement.dir, 'ltr');

  // 2. Rating: optimistic + request, then rollback on failure
  const stars = d.querySelectorAll('.stars button');
  stars[3].click();
  assert.equal(d.querySelectorAll('.fb').length, 2, 'optimistic feedback row');
  await sleep(40);
  const rated = t.calls.find((c) => c.name === 'recipes_add_feedback');
  assert.equal(rated.arguments.rating, 4); assert.ok(rated.arguments.idempotency_key);
  assert.equal(d.querySelectorAll('.fb').length, 2);

  // 3. Delete feedback: gateway handshake rendered inline
  d.querySelector('.fb .icon-btn').click();
  await sleep(40);
  const sheet = q(d, '.sheet');
  assert.ok(sheet && /Really\?/.test(sheet.textContent), 'inline confirmation');
  sheet.querySelector('.btn-danger').click();
  await sleep(40);
  const df = t.calls.filter((c) => c.name === 'recipes_delete_feedback');
  assert.equal(df.length, 2); assert.equal(df[1].arguments.confirmation_token, 'tok');
  assert.equal(df[1].arguments.feedback_id, 'new1', 'newest entry listed first');
  assert.equal(d.querySelectorAll('.fb').length, 1, 'only that entry removed');

  // 4. Edit -> update with version, clear_fields; conflict banner
  byText(d, 'button', 'Edit').click();
  const title = d.querySelectorAll('input.field')[0];
  type(t.window, title, 'Ghormeh Sabzi 2');
  const desc = d.querySelector('textarea.field');
  type(t.window, desc, '');                           // clearing an optional field
  byText(d, 'button', 'Add ingredient').click();
  const rows = d.querySelectorAll('.row');
  assert.ok(rows.length >= 4);
  byText(d, 'button', 'Save').click();
  await sleep(60);
  const upd = t.calls.find((c) => c.name === 'recipes_update').arguments;
  assert.equal(upd.recipe_id, 'r1'); assert.equal(upd.version, '9', 'uses the version returned by the feedback calls'); assert.equal(upd.title, 'Ghormeh Sabzi 2');
  assert.deepEqual(upd.clear_fields, ['description']);
  assert.deepEqual(upd.ingredients[1], { name: 'Flour', quantity: '1 1/2', unit: 'cup' }, 'original quantity text preserved');
  assert.equal(upd.ingredients.length, 3, 'blank ingredient row dropped');
  assert.equal(t.calls.at(-1).name, 'recipes_get', 'reloads after save');

  t = boot({ firstResult: { ...RECIPE() }, fail: { recipes_update: 'Error executing tool recipes_update: the recipe changed since that version was read' } });
  await sleep(50); d = t.document;
  byText(d, 'button', 'Edit').click();
  byText(d, 'button', 'Save').click();
  await sleep(50);
  assert.ok(q(d, '.warn'), 'conflict banner'); assert.ok(byText(d, '.warn button', 'Reload'));
  assert.equal(q(d, '.error'), null);

  // 5. Delete recipe: cancel keeps; confirm deletes then returns to list
  t = boot({ firstResult: { ...RECIPE() } });
  await sleep(50); d = t.document;
  byText(d, 'button', 'Delete').click();
  await sleep(40);
  byText(d, '.sheet button', 'Cancel').click();
  await sleep(40);
  assert.ok(q(d, 'ol.steps'), 'still on the recipe after cancel');
  byText(d, 'button', 'Delete').click();
  await sleep(40);
  q(d, '.sheet .btn-danger').click();
  await sleep(60);
  assert.equal(t.calls.filter((c) => c.name === 'recipes_delete').length, 3, 'cancel (1) + handshake (2)');
  assert.ok(q(d, '.cards'), 'back to list'); assert.ok(!t.db.r1);

  // 6. New recipe + search
  t = boot({ firstResult: LIST });
  await sleep(50); d = t.document;
  const search = q(d, 'input[type=search]');
  search.value = 'sabzi';
  search.dispatchEvent(new t.window.KeyboardEvent('keydown', { key: 'Enter' }));
  await sleep(40);
  assert.deepEqual(t.calls.at(-1), { name: 'recipes_search', arguments: { limit: 20, offset: 0, query: 'sabzi' } });
  byText(d, 'button', 'New recipe').click();
  byText(d, 'button', 'Save').click();
  assert.match(q(d, '.error').textContent, /title is required/);
  type(t.window, d.querySelectorAll('input.field')[0], 'Tahdig');
  byText(d, 'button', 'Save').click();
  await sleep(60);
  const saved = t.calls.find((c) => c.name === 'recipes_save').arguments;
  assert.equal(saved.title, 'Tahdig'); assert.ok(saved.idempotency_key);
  assert.equal(t.calls.at(-1).name, 'recipes_get'); assert.equal(t.calls.at(-1).arguments.recipe_id, 'r3');

  // 7. Persian + dark + RTL, Persian digits, failure rollback on rating
  t = boot({ locale: 'fa-IR', theme: 'dark', firstResult: { ...RECIPE(), title: 'قورمه سبزی' }, fail: { recipes_add_feedback: 'permission_denied' } });
  await sleep(50); d = t.document;
  assert.equal(d.documentElement.dir, 'rtl'); assert.equal(d.documentElement.getAttribute('data-theme'), 'dark');
  assert.match(q(d, '.scaler output').textContent, /۴/, 'persian digits');
  assert.match(d.body.textContent, /مواد لازم/);
  assert.equal(q(d, 'h1').getAttribute('dir'), 'auto');
  d.querySelectorAll('.stars button')[2].click();
  await sleep(40);
  assert.equal(d.querySelectorAll('.fb').length, 1, 'optimistic rating rolled back');
  assert.match(q(d, '.error').textContent, /permission_denied/); assert.match(q(d, '.error').textContent, /انجام نشد/);
  console.log('recipes widget: all assertions passed');
}
run().catch((e) => { console.error(e); process.exit(1); });
