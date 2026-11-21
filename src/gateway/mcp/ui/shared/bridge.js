/* Minimal MCP Apps client (protocol 2026-01-26) shared by every gateway widget.
 * No dependencies: the host CSP blocks the network, so everything is inline.
 * API: const app = GatewayApp({ onResult(result, ctx), onInput(args), onContext(ctx) });
 *      app.callTool(name, args, { confirm }) -> parsed object (handles the confirmation handshake)
 *      app.openLink(url), app.ctx (host context), app.args (tool arguments), app.toolName
 */
(function () {
  'use strict';
  var PROTOCOL = '2026-01-26';

  function parseResult(result) {
    if (!result) return {};
    if (result.structuredContent && typeof result.structuredContent === 'object') {
      return result.structuredContent;
    }
    var text = (result.content || [])
      .filter(function (c) { return c && c.type === 'text'; })
      .map(function (c) { return c.text; })
      .join('\n');
    try { return JSON.parse(text); } catch (e) { return { text: text }; }
  }

  function errorText(result) {
    var text = (result.content || []).map(function (c) { return c.text || ''; }).join(' ').trim();
    return text || 'The tool call failed.';
  }

  window.GatewayApp = function GatewayApp(handlers) {
    handlers = handlers || {};
    var nextId = 1;
    var pending = {};
    var app = { ctx: {}, args: {}, toolName: null };

    function post(message) {
      message.jsonrpc = '2.0';
      window.parent.postMessage(message, '*');
    }
    function request(method, params) {
      return new Promise(function (resolve, reject) {
        var id = nextId++;
        pending[id] = { resolve: resolve, reject: reject };
        post({ id: id, method: method, params: params || {} });
      });
    }
    function notify(method, params) { post({ method: method, params: params || {} }); }

    function applyContext(ctx) {
      app.ctx = Object.assign(app.ctx, ctx || {});
      var theme = app.ctx.theme;
      if (theme === 'dark' || theme === 'light') {
        document.documentElement.setAttribute('data-theme', theme);
      }
      var locale = app.ctx.locale || navigator.language || 'en';
      document.documentElement.lang = locale;
      var rtl = /^(fa|ar|he|ur)\b/i.test(locale);
      document.documentElement.dir = rtl ? 'rtl' : 'ltr';
      var tool = app.ctx.toolInfo && app.ctx.toolInfo.tool;
      if (tool && tool.name) app.toolName = tool.name;
      if (handlers.onContext) handlers.onContext(app.ctx);
    }

    window.addEventListener('message', function (event) {
      if (event.source !== window.parent) return;
      var msg = event.data;
      if (!msg || msg.jsonrpc !== '2.0') return;
      if (msg.method === undefined && msg.id !== undefined) {
        var entry = pending[msg.id];
        if (!entry) return;
        delete pending[msg.id];
        if (msg.error) entry.reject(new Error(msg.error.message || 'Request failed'));
        else entry.resolve(msg.result);
        return;
      }
      var params = msg.params || {};
      switch (msg.method) {
        case 'ui/notifications/tool-input':
          app.args = params.arguments || {};
          if (handlers.onInput) handlers.onInput(app.args);
          break;
        case 'ui/notifications/tool-result':
          if (params.isError) {
            if (handlers.onError) handlers.onError(new Error(errorText(params)));
          } else if (handlers.onResult) {
            handlers.onResult(parseResult(params), app);
          }
          break;
        case 'ui/notifications/host-context-changed':
          applyContext(params);
          break;
        case 'ui/resource-teardown':
        case 'ping':
          if (msg.id !== undefined) post({ id: msg.id, result: {} });
          break;
        default:
          if (msg.id !== undefined) {
            post({ id: msg.id, error: { code: -32601, message: 'Method not found' } });
          }
      }
    });

    app.callTool = function (name, args, options) {
      options = options || {};
      function run(extra) {
        var payload = Object.assign({}, args || {}, extra || {});
        // Keep follow-up actions on the connected account the widget was opened for.
        if (app.args && app.args.account && !payload.account) payload.account = app.args.account;
        return request('tools/call', { name: name, arguments: payload }).then(function (result) {
          if (result && result.isError) throw new Error(errorText(result));
          return parseResult(result);
        });
      }
      return run().then(function (data) {
        if (!data || data.status !== 'confirmation_required') return data;
        var confirm = options.confirm || app.confirm;
        return Promise.resolve(confirm(data.preview || 'Confirm this action?')).then(function (ok) {
          if (!ok) { var e = new Error('cancelled'); e.cancelled = true; throw e; }
          return run({ confirmation_token: data.confirmation_token });
        });
      });
    };

    app.openLink = function (url) {
      return request('ui/open-link', { url: url }).catch(function () {});
    };

    // Inline confirmation sheet (the gateway's preview is the message).
    app.confirm = function (message, labels) {
      labels = labels || {};
      return new Promise(function (resolve) {
        var sheet = document.createElement('div');
        sheet.className = 'sheet';
        sheet.setAttribute('role', 'alertdialog');
        var text = document.createElement('p');
        text.textContent = message;
        var row = document.createElement('div');
        row.className = 'sheet-actions';
        var no = document.createElement('button');
        no.className = 'btn';
        no.textContent = labels.cancel || 'Cancel';
        var yes = document.createElement('button');
        yes.className = 'btn btn-danger';
        yes.textContent = labels.ok || 'Confirm';
        function done(value) { sheet.remove(); resolve(value); }
        no.onclick = function () { done(false); };
        yes.onclick = function () { done(true); };
        row.append(no, yes);
        sheet.append(text, row);
        document.body.appendChild(sheet);
        yes.focus();
      });
    };

    // Report content height so the host can size the iframe (scrollbars are hidden).
    var lastHeight = 0;
    function reportSize() {
      var root = document.documentElement;
      var previous = root.style.height;
      root.style.height = 'max-content';
      var height = Math.ceil(root.getBoundingClientRect().height);
      root.style.height = previous;
      if (height !== lastHeight) {
        lastHeight = height;
        notify('ui/notifications/size-changed', { width: Math.ceil(window.innerWidth), height: height });
      }
    }
    app.reportSize = reportSize;

    request('ui/initialize', {
      appInfo: { name: 'workspace-mcp-gateway-widget', version: '1.0.0' },
      appCapabilities: {},
      protocolVersion: PROTOCOL
    }).then(function (result) {
      applyContext((result && result.hostContext) || {});
      notify('ui/notifications/initialized');
      if (typeof ResizeObserver === 'function') {
        var observer = new ResizeObserver(reportSize);
        observer.observe(document.documentElement);
        observer.observe(document.body);
      }
      reportSize();
      if (handlers.onReady) handlers.onReady(app);
    }).catch(function (error) {
      document.body.textContent = 'Could not start: ' + error.message;
    });

    return app;
  };
})();
