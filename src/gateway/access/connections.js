let csrf = '';
const cards = document.querySelector('#cards');
const statusLine = document.querySelector('#status');
const noticeLine = document.querySelector('#notice');
const labels = {
  connected: 'Connected',
  not_connected: 'Not connected',
  reauth_required: 'Needs reauthorization',
  available: 'Available',
  not_configured: 'Not configured'
};

function message(text, error = false) {
  statusLine.textContent = text;
  statusLine.classList.toggle('error', error);
}

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

async function call(method, url, body) {
  const response = await fetch(url, {
    method,
    headers: { 'X-CSRF-Token': csrf, ...(body ? { 'Content-Type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined
  });
  if (!response.ok) {
    let detail = '';
    try { detail = (await response.json()).detail || ''; } catch { /* ignore */ }
    throw Error(detail || `Request failed (${response.status})`);
  }
}

async function act(label, fn) {
  message(`${label}…`);
  try {
    await fn();
    message(`${label} done.`);
    await load();
  } catch (error) {
    message(error.message, true);
  }
}

function inputRow(c, placeholder, send) {
  const input = el('input', { type: 'password', placeholder, autocomplete: 'off' });
  const row = el('div', { className: 'inline-form' }, input,
    el('button', { className: 'primary', textContent: 'Save' }),
    el('button', { className: 'text-button', textContent: 'Cancel' }));
  const [save, cancel] = row.querySelectorAll('button');
  save.onclick = () => input.value.trim() && act(`Saving ${c.name}`, () => send(input.value.trim()));
  cancel.onclick = () => row.remove();
  return row;
}

function actions(c, card) {
  const box = el('div', { className: 'card-actions' });
  const connected = c.status === 'connected';
  if (c.action === 'oauth') {
    const product = encodeURIComponent(c.oauth_product || c.slug);
    box.append(el('a', {
      className: 'primary link-button',
      href: `/oauth/google/start?product=${product}`,
      textContent: c.bundle ? 'Connect an account to all'
        : (c.accounts && c.accounts.length ? 'Add account' : 'Connect')
    }));
  } else if (c.action === 'bot_token') {
    box.append(el('button', {
      className: 'secondary', textContent: connected ? 'Replace bot' : 'Connect bot',
      onclick: () => card.append(inputRow(c, 'Paste bot token',
        token => call('PUT', '/api/telegram/connection', { bot_token: token })))
    }));
    if (connected) {
      box.append(el('button', {
        className: 'text-button', textContent: 'Disconnect',
        onclick: () => act('Disconnecting Telegram', () => call('DELETE', '/api/telegram/connection'))
      }));
    }
  }
  return box;
}

function accountRows(c) {
  const list = el('ul', { className: 'accounts' });
  for (const a of c.accounts || []) {
    const row = el('li', { className: `account ${a.status}` });
    row.append(el('span', { className: 'account-email', textContent: a.email || 'Unknown account' }));
    if (a.is_default && (c.accounts || []).length > 1) {
      row.append(el('span', { className: 'pill connected', textContent: 'Default' }));
    }
    if (a.status === 'reauth_required') {
      row.append(el('a', {
        className: 'secondary',
        href: `/oauth/google/start?product=${encodeURIComponent(c.slug)}`,
        textContent: 'Reconnect'
      }));
    }
    if (!a.is_default && a.status === 'connected') {
      row.append(el('button', {
        className: 'secondary', textContent: 'Make default',
        onclick: () => act('Updating default', () =>
          call('POST', `/oauth/google/connections/${encodeURIComponent(a.id)}/default`))
      }));
    }
    row.append(el('button', {
      className: 'secondary danger', textContent: 'Disconnect',
      onclick: () => confirm(`Disconnect ${a.email || 'this account'} from ${c.name}?`)
        && act('Disconnecting', () =>
          call('POST', `/oauth/google/connections/${encodeURIComponent(a.id)}/disconnect`))
    }));
    list.append(row);
  }
  return list;
}

function card(c) {
  const node = el('article', { className: `card ${c.status}` });
  node.append(
    el('div', { className: 'card-top' },
      el('h2', { textContent: c.name }),
      el('span', { className: `pill ${c.status}`, textContent: labels[c.status] || c.status })),
    el('span', {
      className: `scope ${c.scope}`,
      textContent: c.scope === 'universal' ? 'Universal · same data for everyone' : 'Personal · your own account',
      title: c.scope === 'universal'
        ? 'Shared connector: no per-user credentials, available to every signed-in user.'
        : 'Uses your own connected account or data.'
    }),
    c.accounts && c.accounts.length
      ? accountRows(c)
      : el('p', { className: 'muted', textContent: c.account ? `Account: ${c.account}` : ' ' }),
    el('p', { className: 'muted', textContent: `${c.tool_count} ${c.tool_count === 1 ? 'tool' : 'tools'}` }),
    el('div', { className: 'url-row' },
      el('code', { textContent: c.mcp_url }),
      el('button', {
        className: 'text-button', textContent: 'Copy',
        onclick: async () => {
          try { await navigator.clipboard.writeText(c.mcp_url); message('MCP URL copied.'); }
          catch { message('Copy failed.', true); }
        }
      })),
    actions(c, node)
  );
  return node;
}

async function load() {
  const response = await fetch('/api/connections', { cache: 'no-store' });
  if (response.status === 401) { location.href = '/access/login'; return; }
  if (!response.ok) { message('Could not load connections.', true); return; }
  const data = await response.json();
  csrf = data.csrf;
  if (data.notice?.message) {
    noticeLine.textContent = data.notice.message;
    noticeLine.hidden = false;
  }
  cards.replaceChildren(...data.connectors.map(card));
  cards.setAttribute('aria-busy', 'false');
}

load();
