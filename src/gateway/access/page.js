let data;
let busy = false;
const client = document.querySelector('#client');
const status = document.querySelector('#status');
const save = document.querySelector('#save');
const revoke = document.querySelector('#revoke');
const titles = {
  telegram_list_destinations: 'List destinations',
  telegram_send_message: 'Send messages'
};
function message(text, error = false) {
  status.textContent = text;
  status.classList.toggle('error', error);
}
function summary() {
  const inputs = [...document.querySelectorAll('#tools input:checked')];
  const reads = inputs.filter(i => i.dataset.risk === 'read').length;
  const writes = inputs.length - reads;
  document.querySelector('#summary').textContent = `${reads} read ${reads === 1 ? 'tool' : 'tools'} · ${writes} write ${writes === 1 ? 'tool' : 'tools'} selected`;
}
function tierSelect(tool) {
  const select = document.createElement('select'); select.className = 'tier';
  select.setAttribute('aria-label', `Risk level for ${tool.name}`);
  select.title = 'Low runs directly. Medium makes the assistant ask you in chat before it acts.';
  for (const [value, text] of [['low', 'Low risk'], ['medium', 'Medium: ask me']]) {
    const option = document.createElement('option'); option.value = value; option.textContent = text; select.append(option);
  }
  select.value = (data.risk || {})[tool.name] || tool.default_tier;
  select.onclick = e => e.stopPropagation();
  select.onchange = async () => {
    select.disabled = true;
    try {
      const response = await fetch('/api/tool-risk', {
        method: 'PUT', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': data.csrf},
        body: JSON.stringify({tool: tool.name, tier: select.value})
      });
      if (!response.ok) throw Error();
      data.risk = {...(data.risk || {}), [tool.name]: select.value};
      message(`${tool.name} is now ${select.value} risk.`);
    } catch { message('Unable to change the risk level. Refresh and try again.', true); }
    finally { select.disabled = false; }
  };
  return select;
}
function setBusy(value) {
  busy = value;
  const personal=data?.connectors.some(c=>c.access_mode!=='authenticated_shared');
  save.hidden = revoke.hidden = !personal;
  save.disabled = busy || !client.value;
  revoke.disabled = busy || !data?.grants.some(g => g.client_id === client.value && !g.revoked);
}
function render() {
  const grant = data.grants.find(g => g.client_id === client.value);
  const selected = new Set(grant && !grant.revoked ? grant.tools : []);
  const root = document.querySelector('#tools');
  root.replaceChildren();
  root.setAttribute('aria-busy', 'false');
  const name = data.scoped ? data.connectors[0]?.name : null;
  document.querySelector('#title').textContent = name ? `Choose your ${name} permissions.` : 'Choose what your client can do.';
  document.querySelector('#connection-name').textContent = name || 'Connector permissions';
  document.querySelector('#scope-note').textContent = name ? `These permissions apply to ${name} for this client.` : 'Choose the tools this client may use.';
  document.querySelector('#intro').textContent = name ? `Select the ${name} tools this client can use. You can update or remove access at any time.` : 'Give your client access to the tools you need. You can change these permissions at any time.';
  revoke.textContent = name ? `Remove ${name} access` : 'Remove client access';
  for (const connector of data.connectors) {
    const group = document.createElement('fieldset');
    const legend = document.createElement('legend'); legend.textContent = connector.name;
    group.append(legend);
    if (connector.access_mode==='authenticated_shared') {
      const note=document.createElement('p');note.textContent='Available to signed-in users. Disconnect or revoke OAuth authorization to remove client access.';
      group.append(note);root.append(group);continue;
    }
    const actions = document.createElement('div'); actions.className = 'group-actions';
    const all = document.createElement('button'); all.textContent = 'Select read tools'; all.type = 'button';
    all.onclick = () => { group.querySelectorAll('input').forEach(i => { i.checked = i.dataset.risk === 'read'; }); summary(); };
    const clear = document.createElement('button'); clear.textContent = 'Clear selection'; clear.type = 'button';
    clear.onclick = () => { group.querySelectorAll('input').forEach(i => { i.checked = false; }); summary(); };
    actions.append(all, clear); group.append(actions);
    for (const tool of connector.tools) {
      const label = document.createElement('label'); label.className = 'tool';
      const input = document.createElement('input');
      input.type = 'checkbox'; input.value = tool.name; input.dataset.risk = tool.risk;
      input.checked = selected.has(tool.name); input.onchange = summary;
      const copy = document.createElement('span'); copy.className = 'tool-copy';
      const top = document.createElement('span'); top.className = 'tool-top';
      const title = document.createElement('span'); title.className = 'tool-name';
      title.textContent = titles[tool.name] || tool.name.replace(/^(google_calendar_|google_tasks_|google_gmail_|google_drive_|apex_wealth_|apex_transactions_|apex_|telegram_|recipes_|finance_)/, '').replaceAll('_', ' ').replace(/^./, c => c.toUpperCase());
      const risk = document.createElement('span'); risk.className = `risk${tool.risk === 'read' ? '' : ' write'}`;
      risk.textContent = tool.risk === 'read' ? 'Read' : 'Write';
      const description = document.createElement('span'); description.className = 'tool-description'; description.textContent = tool.description;
      const code = document.createElement('span'); code.className = 'tool-code'; code.textContent = tool.name;
      top.append(title, risk);
      if (tool.default_tier) top.append(tierSelect(tool));
      copy.append(top, description, code); label.append(input, copy); group.append(label);
    }
    root.append(group);
  }
  if (data.connectors.every(c=>c.access_mode==='authenticated_shared')) {
    document.querySelector('#title').textContent='Shared connector access';
    document.querySelector('#intro').textContent='These tools provide generic data available to every signed-in user.';
    document.querySelector('#scope-note').textContent='No personal tool permissions are required.';
    document.querySelector('#summary').hidden=true;
  }
  if (!client.value) message('Open the permissions link from your MCP client to get started.');
  summary(); setBusy(false);
}
async function load(preferred = client.value) {
  const response = await fetch('/api/access', {cache: 'no-store'});
  if (!response.ok) throw Error(response.status === 400 ? 'Open a fresh permissions link from your MCP client.' : 'Sign in again to manage access.');
  data = await response.json(); client.replaceChildren();
  const clients = new Set(data.grants.map(g => g.client_id)); if (data.target) clients.add(data.target);
  for (const id of clients) {
    const option = document.createElement('option'); option.value = id; option.textContent = id; client.append(option);
  }
  client.disabled = Boolean(data.target) || clients.size < 2;
  if (data.target) client.value = data.target;
  else if (clients.has(preferred)) client.value = preferred;
  render();
}
async function change(method) {
  if (!client.value || busy) return;
  setBusy(true); message('');
  try {
    const response = await fetch(`/api/access/${encodeURIComponent(client.value)}`, {
      method, headers: {'Content-Type': 'application/json', 'X-CSRF-Token': data.csrf},
      ...(method === 'PUT' ? {body: JSON.stringify({tools: [...document.querySelectorAll('#tools input:checked')].map(i => i.value)})} : {})
    });
    if (!response.ok) throw Error('Unable to update permissions. Refresh and try again.');
    await load();
    message(method === 'PUT' ? 'Permissions saved. Reconnect your client to refresh its tools.' : 'Access removed for this connection.');
  } finally { setBusy(false); }
}
client.onchange = () => { message(''); render(); };
save.onclick = () => change('PUT').catch(e => message(e.message, true));
revoke.onclick = () => change('DELETE').catch(e => message(e.message, true));
load().catch(e => { document.querySelector('#tools').replaceChildren(); message(e.message, true); });
