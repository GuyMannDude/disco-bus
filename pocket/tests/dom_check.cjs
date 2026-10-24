// Drives a real headless Chrome over CDP (node 22's global WebSocket, no npm)
// against a running Pocket Bus, and prints one JSON object of DOM facts for
// test_app_dom.py to assert on.
//   node dom_check.cjs <browser-ws-url> <base-url> <pairing-code> <login> <xss-id> <long-id> [screenshot-dir]
'use strict';
const fs = require('node:fs');
const path = require('node:path');

const [wsUrl, base, code, login, xssId, longId, shotDir] = process.argv.slice(2);

function cdp(url) {
  const ws = new WebSocket(url);
  let next = 1;
  const waiting = new Map();
  const listeners = [];
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && waiting.has(msg.id)) {
      const { resolve, reject } = waiting.get(msg.id);
      waiting.delete(msg.id);
      msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
    } else if (msg.method) {
      listeners.forEach((fn) => fn(msg));
    }
  };
  const open = new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
  return {
    open,
    on: (fn) => listeners.push(fn),
    send(method, params, sessionId) {
      const id = next++;
      ws.send(JSON.stringify({ id, method, params: params || {}, sessionId }));
      return new Promise((resolve, reject) => waiting.set(id, { resolve, reject }));
    },
    close: () => ws.close(),
  };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const c = cdp(wsUrl);
  await c.open;
  const { targetId } = await c.send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await c.send('Target.attachToTarget', { targetId, flatten: true });
  const s = (method, params) => c.send(method, params, sessionId);

  const facts = { dialogs: [], exceptions: [], cspViolations: 0 };
  c.on((msg) => {
    if (msg.sessionId !== sessionId) return;
    if (msg.method === 'Page.javascriptDialogOpening') {
      facts.dialogs.push(msg.params.message);
      s('Page.handleJavaScriptDialog', { accept: true });
    }
    if (msg.method === 'Runtime.exceptionThrown') {
      facts.exceptions.push(msg.params.exceptionDetails.exception?.description || msg.params.exceptionDetails.text);
    }
  });
  await s('Page.enable');
  await s('Runtime.enable');
  await s('Network.enable');
  // Tailscale Serve adds this header on the real path; here we add it ourselves.
  await s('Network.setExtraHTTPHeaders', { headers: { 'Tailscale-User-Login': login } });
  await s('Emulation.setDeviceMetricsOverride', { width: 412, height: 915, deviceScaleFactor: 2, mobile: true });

  const evaluate = async (expression) => {
    const r = await s('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
    return r.result.value;
  };
  const waitFor = async (expr, what) => {
    for (let i = 0; i < 100; i++) {
      if (await evaluate(expr)) return;
      await sleep(100);
    }
    throw new Error('timed out waiting for ' + what);
  };
  // A screen that prints the word null or undefined has leaked a JS value.
  facts.leaks = [];
  const leakCheck = async (where) => {
    // Per text node: the view's whole textContent glues "Fleet" + "null" into one word.
    const bad = await evaluate(`(() => {
      const w = document.createTreeWalker(document.getElementById('view'), NodeFilter.SHOW_TEXT);
      for (let n = w.nextNode(); n; n = w.nextNode()) {
        if (/\\b(null|undefined|NaN)\\b|\\[object Object\\]/.test(n.data)) return true;
      }
      return false;
    })()`);
    if (bad) facts.leaks.push(where);
  };
  const go = async (hash, ready) => {
    await evaluate(`location.hash = ${JSON.stringify(hash)}`);
    await waitFor(ready, hash);
    await sleep(150);
    await leakCheck(hash);
  };
  const shot = async (name) => {
    if (!shotDir) return;
    const { data } = await s('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(shotDir, name + '.png'), Buffer.from(data, 'base64'));
  };

  // Count CSP violations from the first byte of every page (CDP-injected, so
  // the page's own CSP does not apply to this one script).
  await s('Page.addScriptToEvaluateOnNewDocument', { source:
    "document.addEventListener('securitypolicyviolation', () => { window.__csp = (window.__csp || 0) + 1; });" });

  // Load once and pair the way a person does: a wrong code, then the right one.
  await s('Page.navigate', { url: base + '/app/' });
  await waitFor(`document.readyState === 'complete' && !!document.getElementById('setup')`, 'setup screen');
  facts.setupShownWithoutToken = await evaluate(`!document.getElementById('setup').hidden`);
  facts.codeFieldFirst = await evaluate(`(() => {
    const c = document.getElementById('code');
    return { focused: document.activeElement === c, inputmode: c.getAttribute('inputmode'),
             pasteCollapsed: !document.getElementById('token').closest('details').open };
  })()`);
  await shot('0-setup');
  const typeCode = (value) => evaluate(`(() => {
    document.getElementById('setup-msg').textContent = '';
    document.getElementById('code').value = ${JSON.stringify(value)};
    document.getElementById('btn-pair').click();
  })()`);
  await typeCode(code === '000000' ? '999999' : '000000');
  await waitFor(`document.getElementById('setup-msg').textContent !== ''`, 'wrong-code message');
  facts.wrongCode = await evaluate(`({ msg: document.getElementById('setup-msg').textContent,
    stillSetup: !document.getElementById('setup').hidden, token: localStorage.getItem('pocketbus.token') })`);
  await typeCode(code);
  await waitFor(`!!document.querySelector('#view .card')`, 'fleet cards after pairing');
  facts.paired = await evaluate(`({ setupHidden: document.getElementById('setup').hidden,
    tokenStored: (JSON.parse(localStorage.getItem('pocketbus.token') || '""')).startsWith('pb-') })`);
  // The token survives a reload. (Same path + a hash is same-document: reload for real.)
  await evaluate(`location.hash = '#fleet'`);
  await s('Page.reload', { ignoreCache: true });
  await waitFor(`!!document.querySelector('#view .card')`, 'fleet cards');
  await sleep(300);
  await leakCheck('#fleet');
  facts.fleetCards = await evaluate(`document.querySelectorAll('#view .card').length`);
  await shot('1-fleet');

  await go('#inbox', `!!document.querySelector('#view .letter')`);
  facts.inboxRows = await evaluate(`document.querySelectorAll('#view .letter').length`);
  facts.inboxImgs = await evaluate(`document.querySelectorAll('#view img').length`);
  facts.inboxShowsPayloadAsText = await evaluate(`document.getElementById('view').textContent.includes('<img src=x')`);
  await shot('2-inbox');

  await go('#thread/' + xssId, `!!document.getElementById('m${xssId}')`);
  facts.xss = await evaluate(`(() => {
    const b = document.getElementById('m${xssId}');
    return { text: b.querySelector('.text').textContent, subject: b.querySelector('.subj').textContent,
             imgs: document.querySelectorAll('#view img, #view script, #view iframe').length,
             pwned: window.__pwned === undefined ? null : window.__pwned };
  })()`);
  await shot('3-thread-xss');

  await go('#thread/' + longId, `!!document.getElementById('m${longId}')`);
  facts.long = await evaluate(`(() => {
    const b = document.getElementById('m${longId}');
    const more = b.querySelector('button.more');
    const before = b.querySelector('.text').textContent.length;
    const label0 = more ? more.textContent : null;
    if (more) more.click();
    const after = b.querySelector('.text').textContent.length;
    const label1 = more ? more.textContent : null;
    if (more) more.click();
    return { before, after, label0, label1, back: b.querySelector('.text').textContent.length };
  })()`);
  await evaluate(`window.scrollTo(0, 0)`);
  await shot('4-thread-long');

  await go('#compose/CC', `!!document.getElementById('compose-text')`);
  await shot('5-compose');
  await go('#traffic', `!!document.querySelector('#view .letter')`);
  await shot('6-traffic');
  await go('#hold', `!!document.querySelector('#view .hold')`);
  await shot('7-hold');

  facts.cspViolations = await evaluate(`window.__csp || 0`);
  facts.pwnedAtEnd = await evaluate(`window.__pwned === undefined ? null : window.__pwned`);
  process.stdout.write(JSON.stringify(facts));
  await c.send('Target.closeTarget', { targetId });
  c.close();
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
