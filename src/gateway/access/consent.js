const form = document.querySelector('#consent-form');
const status = document.querySelector('#status');
const allow = document.querySelector('#allow');
const deny = document.querySelector('#deny');
const params = new URLSearchParams(window.location.search);
let clerk, config, context, busy = false;
function fail(error) {
  status.textContent = error.message || 'Unable to load this connection. Return to your client and try again.';
  status.classList.add('error');
}
function summary() {
  const selected = [...document.querySelectorAll('#tools input:checked')];
  const reads = selected.filter(i => i.dataset.risk === 'read').length;
  document.querySelector('#summary').textContent = `${reads} read tools · ${selected.length - reads} write tools selected`;
}
async function api(path, body) {
  const token = await clerk.session.getToken();
  const response = await fetch(path, {method:'POST', headers:{
    'Content-Type':'application/json', 'Authorization':`Bearer ${token}`
  }, body:JSON.stringify(body)});
  if (!response.ok) throw Error('Unable to validate this permission request. Return to your client and reconnect.');
  return response.json();
}
function renderTools() {
  const root = document.querySelector('#tools'); root.replaceChildren(); root.setAttribute('aria-busy','false');
  for (const connector of context.connectors) {
    const group = document.createElement('fieldset');
    const legend = document.createElement('legend'); legend.textContent = connector.name; group.append(legend);
    if (connector.access_mode==='authenticated_shared') {
      const note=document.createElement('p');note.textContent='Available to signed-in users';
      group.append(note);root.append(group);continue;
    }
    const actions = document.createElement('div'); actions.className = 'group-actions';
    const read = document.createElement('button'); read.type='button'; read.textContent='Select read tools';
    read.onclick=()=>{group.querySelectorAll('input').forEach(i=>i.checked=i.dataset.risk==='read');summary();};
    const clear = document.createElement('button'); clear.type='button'; clear.textContent='Clear selection';
    clear.onclick=()=>{group.querySelectorAll('input').forEach(i=>i.checked=false);summary();};
    actions.append(read,clear);group.append(actions);
    for (const tool of connector.tools) {
      const label=document.createElement('label');label.className='tool';
      const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.value=tool.name;
      checkbox.dataset.risk=tool.risk;checkbox.checked=context.tools.includes(tool.name);checkbox.onchange=summary;
      const copy=document.createElement('span');copy.className='tool-copy';
      const top=document.createElement('span');top.className='tool-top';
      const title=document.createElement('span');title.className='tool-name';
      title.textContent=tool.name.replace(/^(telegram_|google_calendar_|google_tasks_|google_drive_|apex_wealth_|apex_|recipes_|finance_)/,'').replaceAll('_',' ').replace(/^./,c=>c.toUpperCase());
      const risk=document.createElement('span');risk.className=`risk${tool.risk==='read'?'':' write'}`;risk.textContent=tool.risk==='read'?'Read':'Write';
      const description=document.createElement('span');description.className='tool-description';description.textContent=tool.description;
      const code=document.createElement('span');code.className='tool-code';code.textContent=tool.name;
      top.append(title,risk);copy.append(top,description,code);label.append(checkbox,copy);group.append(label);
    }
    root.append(group);
  }
  summary();
}
async function start() {
  for (const key of ['client_id','redirect_uri','scope','resource','state','code_challenge','code_challenge_method']) {
    if (params.getAll(key).length > 1) throw Error('Duplicate OAuth parameters. Return to your client and reconnect.');
  }
  const clientId=params.get('client_id'), redirect=params.get('redirect_uri');
  if (!clientId || !redirect) throw Error('Start this connection from your MCP client.');
  const response=await fetch('/oauth/consent/config',{cache:'no-store'});
  if (!response.ok) throw Error('Connector consent is not configured.');
  config=await response.json();
  await new Promise((resolve,reject)=>{
    const sdk=document.createElement('script');sdk.src=config.sdk_url;sdk.crossOrigin='anonymous';
    sdk.setAttribute('data-clerk-publishable-key',config.publishable_key);
    sdk.onload=resolve;sdk.onerror=()=>reject(Error('Unable to load secure sign-in. Please try again.'));
    document.head.append(sdk);
  });
  const requestedResource=params.get('resource');
  const sharedOnly=config.shared_resources.includes(requestedResource);
  const useNativeUI=sharedOnly || clientId===config.browser_client_id || !requestedResource || !(requestedResource===config.resource_root || requestedResource.startsWith(config.resource_root+'/'));
  if (useNativeUI) {
    await new Promise((resolve,reject)=>{
      const ui=document.createElement('script');ui.src=config.ui_url;ui.crossOrigin='anonymous';
      ui.onload=resolve;ui.onerror=()=>reject(Error('Unable to load secure consent. Please try again.'));
      document.head.append(ui);
    });
  }
  clerk=window.Clerk;await clerk.load({telemetry:{disabled:true}, ...(useNativeUI?{ui:{ClerkUI:window.__internal_ClerkUICtor}}:{})});
  if (!clerk.session || !clerk.user) {
    await clerk.redirectToSignIn({redirectUrl:window.location.href});return;
  }
  const info=await clerk.oauthApplication.getConsentInfo({oauthClientId:clientId,redirectUri:redirect,scope:params.get('scope')||undefined});
  document.querySelector('#app-name').textContent=info.oauthApplicationName;
  document.querySelector('#account').textContent=clerk.user.primaryEmailAddress?.emailAddress||clerk.user.username||clerk.user.id;
  document.querySelector('#redirect-domain').textContent=info.redirectDomain||new URL(redirect).hostname;
  document.querySelector('#redirect-url').textContent=redirect;
  const resource=params.get('resource');
  if (sharedOnly || clientId===config.browser_client_id || !resource || !(resource===config.resource_root || resource.startsWith(config.resource_root+'/'))) {
    if (!sharedOnly && clientId!==config.browser_client_id && (params.get('scope')||'').split(' ').includes('mcp:tools')) throw Error('The client must request a connector resource. Reconnect using its MCP endpoint.');
    // Unrelated OAuth applications keep Clerk's prebuilt consent and organization handling.
    form.hidden=true;document.querySelector('#title').textContent='Review account access.';
    if (sharedOnly) document.querySelector('#resource-name').textContent='shared data, available to signed-in users';
    clerk.mountOAuthConsent(document.querySelector('#native-consent'));return;
  }
  if (info.scopes.some(s=>s.scope==='user:org:read')) throw Error('Organization access must use the standard Clerk consent flow.');
  const scopes=document.querySelector('#scopes');
  for (const scope of info.scopes) {
    const item=document.createElement('li');item.textContent=scope.description?`${scope.description} (${scope.scope})`:scope.scope;scopes.append(item);
  }
  context=await api('/api/consent/context',{client_id:clientId,redirect_uri:redirect,resource,scope:params.get('scope')||''});
  const name=context.connectors.length===1?context.connectors[0].name:'Workspace connectors';
  document.querySelector('#title').textContent=`Choose your ${name} permissions.`;
  document.querySelector('#connection-name').textContent=name;
  document.querySelector('#resource-name').textContent=name;
  renderTools();allow.disabled=false;deny.disabled=false;
}
function submitToClerk(consented) {
  const action=clerk.oauthApplication.buildConsentActionUrl({clientId:params.get('client_id')});
  const expected=new URL(config.sdk_url).origin;
  if (new URL(action).origin!==expected) throw Error('Unexpected authorization destination.');
  form.action=action;
  for (const [key,value] of params.entries()) {
    if (['consented','organization_id','_clerk_session_id','__clerk_db_jwt'].includes(key)) continue;
    const field=document.createElement('input');field.type='hidden';field.name=key;field.value=value;form.append(field);
  }
  const decision=document.createElement('input');decision.type='hidden';decision.name='consented';decision.value=String(consented);form.append(decision);
  HTMLFormElement.prototype.submit.call(form);
}
form.addEventListener('submit',async event=>{
  event.preventDefault();if (busy || !context) return;
  busy=true;allow.disabled=true;deny.disabled=true;status.textContent='';
  const consented=event.submitter===allow;
  try {
    if (consented) {
      status.textContent='Saving permissions and returning to your client…';
      await api('/api/consent/approve',{intent:context.intent,tools:[...document.querySelectorAll('#tools input:checked')].map(i=>i.value)});
    }
    submitToClerk(consented);
  } catch(error) {fail(error);busy=false;allow.disabled=false;deny.disabled=false;}
});
start().catch(fail);
