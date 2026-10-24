/* Pocket Bus — the phone face for the bus.
 * Every letter is untrusted text: it reaches the page through textContent
 * and text nodes only. Nothing in this file parses a string as HTML (a test
 * scans for every such sink), and the CSP forbids inline script.
 */
(function () {
  'use strict';

  // ---- pure helpers (also loaded by the node tests) ----------------------
  const PB = {};
  PB.COLLAPSE_AT = 1200;

  PB.collapse = function (text, limit) {
    limit = limit || PB.COLLAPSE_AT;
    if (text.length <= limit) return { short: text, more: false };
    let cut = text.lastIndexOf(' ', limit);
    if (cut < limit * 0.6) cut = limit;
    return { short: text.slice(0, cut).trimEnd() + ' …', more: true };
  };

  PB.ago = function (iso, now) {
    if (!iso) return '';
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return '';
    const s = Math.max(0, Math.round(((now === undefined ? Date.now() : now) - t) / 1000));
    if (s < 60) return 'just now';
    const m = Math.round(s / 60);
    if (m < 60) return m + ' min ago';
    const h = Math.round(m / 60);
    if (h < 24) return h + (h === 1 ? ' hour ago' : ' hours ago');
    const d = Math.round(h / 24);
    return d + (d === 1 ? ' day ago' : ' days ago');
  };

  PB.clock = function (iso, now) {
    const t = new Date(iso);
    if (Number.isNaN(t.getTime())) return '';
    const n = new Date(now === undefined ? Date.now() : now);
    const hm = t.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    if (t.toDateString() === n.toDateString()) return hm;
    return t.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ', ' + hm;
  };

  PB.receiptLine = function (r, label) {
    const name = label || r.to;
    if (!r.ok) return { cls: 'bad', text: 'Didn’t send to ' + name + ': ' + (r.error || 'unknown error') };
    if (r.state === 'HELD') return { cls: 'held', text: 'Held, not sent: you’re paused on the bus (#' + r.id + ')' };
    return { cls: '', text: 'Sent to ' + name + ' — #' + r.id };
  };

  PB.stateNote = function (state) {
    return ({ HELD: ['held, not sent yet', ''], DROPPED: ['dropped, never sent', 'bad'],
              FAILED: ['didn’t arrive', 'bad'], ACKED: ['acknowledged', ''] })[state] || null;
  };

  // A reply goes to the other side of the conversation, on its newest letter.
  PB.replyTarget = function (messages, me) {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].from !== me) return { to: messages[i].from, reply_to: messages[i].id };
    }
    const last = messages[messages.length - 1];
    return last ? { to: last.to, reply_to: last.id } : null;
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = PB;
  if (typeof document === 'undefined') return;

  // ---- storage (localStorage can throw or vanish; never trust it) ---------
  const store = {
    get(k, d) { try { const v = localStorage.getItem('pocketbus.' + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem('pocketbus.' + k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
    del(k) { try { localStorage.removeItem('pocketbus.' + k); } catch (e) { /* private mode */ } },
  };

  // ---- DOM: text only -----------------------------------------------------
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : String(v));
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(typeof kid === 'string' ? document.createTextNode(kid) : kid);
    }
    return el;
  }
  const $ = (id) => document.getElementById(id);
  const view = $('view');

  // ---- state ------------------------------------------------------------------
  let token = store.get('token', '');
  let me = store.get('me', 'Guy');
  let hostLabel = store.get('host', 'the bus host');
  let fleet = store.get('fleet', []);          // [{agent,label,...}] — names for every screen
  let inboxFilter = 'all';
  let renderSeq = 0;

  const labelOf = (agent) => {
    if (agent === me) return 'You';
    const f = fleet.find((a) => a.agent === agent);
    return f ? f.label : agent;
  };

  // ---- API ----------------------------------------------------------------------
  async function api(method, path, body) {
    const opts = { method, cache: 'no-store', headers: { Authorization: 'Bearer ' + token } };
    if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    let res;
    try {
      const r = await fetch('/api/' + path, opts);
      let data = {};
      try { data = await r.json(); } catch (e) { /* not JSON */ }
      res = { status: r.status, ok: r.ok, data };
    } catch (e) {
      res = { status: 0, ok: false, data: { error: 'Can’t reach Pocket Bus on ' + hostLabel + '.' } };
    }
    if (res.status === 401) { forgetToken('That token didn’t work. Paste it again.'); }
    else if (res.status === 0) lamp('bad');
    else if (res.status === 503 && res.data.disabled) lamp('off');
    else if (res.data && res.data.dispatcher_down) lamp('nodispatch');
    return res;
  }

  // ---- header lamp + banner (the bus's health, said out loud) -----------
  function lamp(kind, detail) {
    const el = $('lamp'), banner = $('banner');
    const last = store.get('lastGood', null);
    const since = last ? ' Last good contact ' + PB.clock(last) + '.' : '';
    el.className = 'lamp';
    banner.hidden = true; banner.className = 'banner';
    if (kind === 'ok') {
      store.set('lastGood', new Date().toISOString());
      el.classList.add('ok'); el.textContent = 'Bus: connected · ' + hostLabel;
    } else if (kind === 'unregistered') {
      el.classList.add('warn'); el.textContent = 'Bus: connected · ' + hostLabel;
      banner.hidden = false; banner.classList.add('warn');
      banner.textContent = 'You’re not on the bus yet, so letters to you can’t arrive. Ask CC to finish setting up Pocket Bus.';
    } else if (kind === 'off') {
      el.classList.add('bad'); el.textContent = 'Pocket Bus is switched off';
      banner.hidden = false; banner.textContent = 'Pocket Bus is switched off. Your mail is still being collected; it shows up when it’s switched back on.';
    } else if (kind === 'nodispatch') {
      el.classList.add('bad'); el.textContent = 'Bus: not reachable';
      banner.hidden = false; banner.textContent = 'Pocket Bus is up, but it can’t reach the bus on ' + hostLabel + '.' + since + (detail ? ' (' + detail + ')' : '');
    } else {
      el.classList.add('bad'); el.textContent = 'Bus: not reachable';
      banner.hidden = false; banner.textContent = 'Can’t reach the bus on ' + hostLabel + '.' + since;
    }
  }

  async function checkHealth() {
    if (!token) return;
    const res = await api('GET', 'health');
    if (!res.ok) return;
    const d = res.data;
    me = d.agent || me; store.set('me', me);
    hostLabel = d.host_label || hostLabel; store.set('host', hostLabel);
    if (!d.dispatcher || !d.dispatcher.ok) lamp('nodispatch', d.dispatcher && d.dispatcher.error);
    else if (!d.registered) lamp('unregistered');
    else lamp('ok');
    const badge = $('badge');
    badge.hidden = !(d.unread > 0);
    badge.textContent = d.unread > 99 ? '99+' : String(d.unread || '');
    $('menu-about').textContent = 'Signed in as ' + me + ' · bus host ' + hostLabel + '.';
  }

  async function loadFleet() {
    const res = await api('GET', 'fleet');
    if (res.ok) { fleet = res.data.agents || []; store.set('fleet', fleet); }
    return res;
  }

  // ---- screens --------------------------------------------------------------------
  function title(text, ...extra) {
    return h('div', { class: 'screen-title' }, h('h2', { text }), ...extra);
  }
  function problem(res) {
    return h('p', { class: 'empty', text: (res.data && res.data.error) || 'Something went wrong.' });
  }
  function paint(seq, ...nodes) {
    if (seq !== renderSeq) return false;        // a newer screen already took over
    view.replaceChildren(...nodes.filter(Boolean));   // replaceChildren(null) would print "null"
    return true;
  }
  function letterRow(m, opts) {
    const who = opts && opts.traffic ? labelOf(m.from) + ' → ' + labelOf(m.to)
      : (m.from === me ? 'You → ' + labelOf(m.to) : labelOf(m.from));
    return h('button', { class: 'letter' + (opts && opts.unread && !m.read_at ? ' unread' : ''), type: 'button',
                         onclick: () => openLetter(m, opts) },
      h('div', { class: 'who' }, h('span', { text: who }), h('span', { class: 'when', text: PB.ago(m.created_at) })),
      h('div', { class: 'subj', text: m.subject || '(no subject)' }),
      m.preview ? h('div', { class: 'prev', text: m.preview }) : null,
      m.outside_door ? h('span', { class: 'tag', text: 'came outside the single door' }) : null);
  }
  async function openLetter(m, opts) {
    if (opts && opts.unread && !m.read_at && m.to === me) {
      await api('POST', 'read/' + m.id, {});
    }
    location.hash = '#thread/' + m.id;
  }

  function doorLine(card) {
    const d = card.door || {};
    const text = d.wakes ? 'Wakes on a letter' : (d.how || 'A letter waits until someone checks the bus');
    return h('div', { class: 'door' + (d.wakes ? ' wakes' : '') },
      h('span', { class: 'dot', 'aria-hidden': 'true' }), text,
      d.note ? h('span', { class: 'note', text: d.note }) : null);
  }

  async function showFleet(seq, quiet) {
    if (!quiet) paint(seq, title('Fleet'), h('p', { class: 'loading', text: 'Loading the fleet…' }));
    const res = await loadFleet();
    if (!res.ok) return paint(seq, title('Fleet'), problem(res));
    const cards = fleet.map((c) => h('button', { class: 'card' + (c.door && c.door.wakes ? ' wakes' : ''), type: 'button',
                                                 onclick: () => { location.hash = '#agent/' + encodeURIComponent(c.agent); } },
      h('div', null, h('span', { class: 'name', text: c.label }), c.host ? h('span', { class: 'host', text: c.host }) : null,
        c.unread > 0 ? h('span', { class: 'pill', text: c.unread + ' unread' }) : null),
      h('div', { class: 'heard', text: !c.on_bus ? 'Not on the bus registry' :
        (c.last_heard ? 'Last heard ' + PB.ago(c.last_heard) : 'Not heard from yet') }),
      doorLine(c)));
    paint(seq, title('Fleet'),
      res.data.doors_error ? h('p', { class: 'msg-err', text: 'No fleet list yet (' + res.data.doors_error + '). Ask CC to add the doors file.' }) : null,
      res.data.unread_partial ? h('p', { class: 'small', text: 'Lots of unread mail: the counts below cover your newest ' + res.data.unread_partial + ' unread letters.' }) : null,
      cards.length ? h('div', { class: 'cards' }, cards) : h('p', { class: 'empty', text: 'Nobody in the fleet list yet.' }));
  }

  async function showInbox(seq, quiet) {
    const seg = h('div', { class: 'seg', role: 'group', 'aria-label': 'Which letters' },
      ...[['unread', 'Unread'], ['all', 'All']].map(([k, t]) => h('button', {
        type: 'button', 'aria-pressed': String(inboxFilter === k), text: t,
        onclick: () => { inboxFilter = k; route(); } })));
    if (!quiet) paint(seq, title('Inbox', seg), h('p', { class: 'loading', text: 'Getting your mail…' }));
    const res = await api('GET', 'inbox?filter=' + inboxFilter + '&limit=60');
    if (!res.ok) return paint(seq, title('Inbox', seg), problem(res));
    const list = res.data.letters || [];
    paint(seq, title('Inbox', seg),
      list.length ? h('div', { class: 'letters' }, list.map((m) => letterRow(m, { unread: true })))
        : h('p', { class: 'empty', text: inboxFilter === 'unread' ? 'Nothing unread. All caught up.' : 'No letters yet.' }));
    checkHealth();
  }

  function bubble(m, focus) {
    const parts = PB.collapse(m.text || '');
    const textEl = h('div', { class: 'text', text: parts.short });
    const st = PB.stateNote(m.state);
    let open = false;
    const details = h('pre', { class: 'details', hidden: true,
      text: 'letter #' + m.id + (m.reply_to ? ' · reply to #' + m.reply_to : '') + '\n' +
            'from ' + m.from + ' to ' + m.to + ' · ' + m.state + '\n' +
            'subject: ' + m.subject + '\n\n' + JSON.stringify(m.body, null, 2) });
    return h('div', { class: 'bubble' + (m.mine ? ' mine' : '') + (focus ? ' focus' : ''), id: 'm' + m.id },
      m.mine ? null : h('div', { class: 'from', text: labelOf(m.from) + (m.to !== me ? ' → ' + labelOf(m.to) : '') }),
      m.mine && m.to ? h('div', { class: 'from', text: 'You → ' + labelOf(m.to) }) : null,
      m.subject ? h('div', { class: 'subj', text: m.subject }) : null,
      textEl,
      parts.more ? h('button', { class: 'more', type: 'button', text: 'Show more',
        onclick: (e) => { const full = textEl.textContent === parts.short;
          textEl.textContent = full ? m.text : parts.short; e.target.textContent = full ? 'Show less' : 'Show more'; } }) : null,
      m.outside_door ? h('span', { class: 'tag', text: 'came outside the single door' }) : null,
      h('div', { class: 'meta' },
        h('span', { text: PB.clock(m.created_at) }),
        st ? h('span', { class: 'state ' + st[1], text: st[0] }) : null,
        h('button', { type: 'button', text: 'Details', 'aria-expanded': 'false',
          onclick: (e) => { open = !open; details.hidden = !open; e.target.setAttribute('aria-expanded', String(open)); } })),
      details);
  }

  async function showThread(seq, id, quiet) {
    const box = quiet ? $('reply-text') : null;
    const draft = box ? box.value : '';
    if (!quiet) paint(seq, title('Conversation'), h('p', { class: 'loading', text: 'Opening…' }));
    const res = await api('GET', 'thread/' + id);
    if (!res.ok) return paint(seq, title('Conversation', backBtn()), problem(res));
    const msgs = res.data.messages || [];
    // Seen on screen = read: mark the letters to me in this conversation.
    msgs.filter((m) => m.to === me && !m.read_at).forEach((m) => { api('POST', 'read/' + m.id, {}); });
    const target = PB.replyTarget(msgs, me);
    const text = h('textarea', { id: 'reply-text', rows: '2', placeholder: target ? 'Reply to ' + labelOf(target.to) + '…' : 'Reply…', 'aria-label': 'Your reply' });
    text.value = draft;
    const out = h('div', { class: 'receipts', 'aria-live': 'polite' });
    const send = h('button', { class: 'primary', type: 'button', text: 'Send', onclick: async () => {
      if (!target || !text.value.trim()) return;
      send.disabled = true;
      const r = await api('POST', 'send', { to: [target.to], text: text.value, reply_to: target.reply_to });
      send.disabled = false;
      if (r.ok) { text.value = ''; renderReceipts(out, r.data.receipts); setTimeout(() => route(true), 600); }
      else out.replaceChildren(h('div', { class: 'receipt bad', text: r.data.error || 'Didn’t send.' }));
    } });
    const painted = paint(seq, title('Conversation', backBtn()),
      h('div', { class: 'bubbles' }, msgs.map((m) => bubble(m, m.id === res.data.focus))),
      out,
      target ? h('div', { class: 'composer' }, text, send) : null);
    if (painted && !quiet) {
      const f = $('m' + res.data.focus);
      if (f) f.scrollIntoView({ block: 'center' });
    }
  }

  function backBtn() {
    return h('button', { class: 'ghost', type: 'button', text: 'Back', onclick: () => history.back() });
  }

  async function showAgent(seq, agent, quiet) {
    if (!fleet.length) await loadFleet();
    const card = fleet.find((c) => c.agent === agent);
    const name = card ? card.label : agent;
    const head = [title(name, backBtn()),
      card ? h('div', { class: 'card' + (card.door && card.door.wakes ? ' wakes' : '') },
        h('div', { class: 'heard', text: card.host ? 'On ' + card.host : '' }), doorLine(card)) : null,
      h('button', { class: 'primary big', type: 'button', text: 'Write to ' + name,
        onclick: () => { location.hash = '#compose/' + encodeURIComponent(agent); } })];
    if (!quiet) paint(seq, ...head, h('p', { class: 'loading', text: 'Loading letters…' }));
    const res = await api('GET', 'agent/' + encodeURIComponent(agent));
    if (!res.ok) return paint(seq, ...head, problem(res));
    const list = res.data.letters || [];
    paint(seq, ...head, h('h3', { class: 'small', text: 'Letters between you' }),
      list.length ? h('div', { class: 'letters' }, list.map((m) => letterRow(m, { unread: true })))
        : h('p', { class: 'empty', text: 'No letters between you yet.' }));
  }

  function renderReceipts(box, receipts) {
    box.replaceChildren(...(receipts || []).map((r) => {
      const line = PB.receiptLine(r, labelOf(r.to));
      return h('div', { class: 'receipt ' + line.cls, text: line.text });
    }));
  }

  async function showCompose(seq, preset) {
    if (!fleet.length) await loadFleet();
    const chosen = new Set(preset ? [preset] : []);
    const text = h('textarea', { id: 'compose-text', rows: '5', placeholder: 'Your letter…', 'aria-label': 'Your letter' });
    const subject = h('input', { type: 'text', maxlength: '200', placeholder: 'Leave blank and the first words are used', 'aria-label': 'Subject' });
    const out = h('div', { class: 'receipts', 'aria-live': 'polite' });
    const send = h('button', { class: 'primary big', type: 'button', text: 'Send' });
    const sync = () => { send.disabled = !chosen.size || !text.value.trim(); };
    const chips = fleet.map((c) => {
      const b = h('button', { class: 'chip', type: 'button', text: c.label, 'aria-pressed': String(chosen.has(c.agent)) });
      b.addEventListener('click', () => {
        if (chosen.has(c.agent)) chosen.delete(c.agent); else chosen.add(c.agent);
        b.setAttribute('aria-pressed', String(chosen.has(c.agent))); sync();
      });
      return b;
    });
    text.addEventListener('input', sync);
    send.addEventListener('click', async () => {
      send.disabled = true;
      const order = fleet.map((c) => c.agent).filter((a) => chosen.has(a));
      const r = await api('POST', 'send', { to: order, text: text.value, subject: subject.value || undefined });
      if (r.ok) { renderReceipts(out, r.data.receipts); if (r.data.ok) { text.value = ''; subject.value = ''; } }
      else out.replaceChildren(h('div', { class: 'receipt bad', text: r.data.error || 'Didn’t send.' }));
      sync();
    });
    sync();
    paint(seq, title('Write a letter', backBtn()),
      h('label', { class: 'lbl', text: 'To' }),
      chips.length ? h('div', { class: 'chips' }, chips) : h('p', { class: 'empty', text: 'No fleet list yet.' }),
      h('label', { class: 'lbl', for: 'compose-text', text: 'Message' }), text,
      h('details', { class: 'sub' }, h('summary', { text: 'Add a subject (optional)' }), subject),
      send, out);
    if (!preset) return;
    text.focus();
  }

  async function showTraffic(seq, quiet) {
    const q = h('input', { type: 'search', placeholder: 'Search every letter…', 'aria-label': 'Search', enterkeyhint: 'search' });
    const results = h('div');
    const go = async () => {
      const term = q.value.trim();
      const res = term ? await api('GET', 'search?q=' + encodeURIComponent(term) + '&limit=60')
        : await api('GET', 'traffic?limit=60');
      if (seq !== renderSeq) return;
      if (!res.ok) return results.replaceChildren(problem(res));
      const list = res.data.letters || [];
      results.replaceChildren(...[
        term ? h('p', { class: 'small', text: res.data.total + ' found' + (res.data.total > list.length ? ', showing the newest ' + list.length : '') }) : null,
        list.length ? h('div', { class: 'letters' }, list.map((m) => letterRow(m, { traffic: true })))
          : h('p', { class: 'empty', text: term ? 'Nothing matches.' : 'No traffic.' })].filter(Boolean));
    };
    q.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
    if (quiet) {
      const live = view.querySelector('input[type=search]');
      if (live && live.value.trim()) return;           // don't clobber a search in progress
    }
    paint(seq, title('Traffic'), h('div', { class: 'row' }, q, h('button', { type: 'button', text: 'Search', onclick: go })), results);
    go();
  }

  async function showHold(seq, quiet) {
    if (!quiet) paint(seq, title('Hold'), h('p', { class: 'loading', text: 'Checking holds…' }));
    const res = await api('GET', 'holds');
    if (!res.ok) return paint(seq, title('Hold'), problem(res));
    const rows = (res.data.agents || []).map((a) => {
      const btn = h('button', { class: a.paused ? 'primary' : '', type: 'button', text: a.paused ? 'Play' : 'Pause' });
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        const r = await api('POST', a.paused ? 'play' : 'pause', { agent: a.agent });
        if (!r.ok) { btn.disabled = false; btn.textContent = 'Try again'; return; }
        route(true);
      });
      return h('div', { class: 'hold' + (a.paused ? ' paused' : '') },
        h('div', { class: 'who' }, h('b', { text: a.label }),
          h('span', { text: a.paused ? 'Paused since ' + PB.clock(a.paused_at) + ' · ' + a.held + ' held' : 'Running' + (a.held ? ' · ' + a.held + ' held' : '') })),
        btn);
    });
    paint(seq, title('Hold'),
      h('p', { class: 'small', text: 'Pause holds an agent’s outgoing letters on the bus. Play sends them, in order.' }),
      rows.length ? h('div', { class: 'holds' }, rows) : h('p', { class: 'empty', text: 'No fleet list yet.' }));
  }

  // ---- router -----------------------------------------------------------------
  function route(quiet) {
    const seq = ++renderSeq;
    if (!token) { showSetup(); return; }
    const [name, arg] = (location.hash.slice(1) || 'fleet').split('/');
    const tab = { fleet: 'fleet', inbox: 'inbox', traffic: 'traffic', hold: 'hold' }[name] || '';
    document.querySelectorAll('#tabs a').forEach((a) => {
      if (a.dataset.tab === tab) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
    const a = arg ? decodeURIComponent(arg) : '';
    if (!quiet) window.scrollTo(0, 0);
    switch (name) {
      case 'inbox': return showInbox(seq, quiet);
      case 'traffic': return showTraffic(seq, quiet);
      case 'hold': return showHold(seq, quiet);
      case 'thread': return /^\d+$/.test(a) ? showThread(seq, a, quiet) : showFleet(seq);
      case 'agent': return showAgent(seq, a, quiet);
      case 'compose': return quiet ? undefined : showCompose(seq, a);
      default: return showFleet(seq, quiet);
    }
  }

  // ---- setup / settings ---------------------------------------------------------
  function showSetup(msg) {
    $('setup').hidden = false;
    $('setup-msg').textContent = msg || '';
    $('token').focus();
  }
  function forgetToken(msg) {
    token = ''; store.del('token');
    showSetup(msg);
  }
  $('btn-connect').addEventListener('click', async () => {
    const t = $('token').value.trim();
    if (!t) return;
    token = t;
    const res = await api('GET', 'health');
    if (res.status === 401) return;                    // api() already asked again
    if (res.status === 403) { token = ''; return showSetup('This phone isn’t signed in to Tailscale as the owner.'); }
    store.set('token', t);
    $('token').value = '';
    $('setup').hidden = true;
    checkHealth(); route();
  });
  $('token').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('btn-connect').click(); });
  $('btn-write').addEventListener('click', () => { location.hash = '#compose'; });
  $('btn-menu').addEventListener('click', () => { $('menu').hidden = false; });
  $('btn-menu-close').addEventListener('click', () => { $('menu').hidden = true; });
  $('btn-forget').addEventListener('click', () => { $('menu').hidden = true; forgetToken('Token forgotten on this phone.'); });
  function applyDim(on) {
    document.body.classList.toggle('dim', on);
    $('btn-dim').textContent = 'Dim: ' + (on ? 'on' : 'off');
    store.set('dim', on);
  }
  $('btn-dim').addEventListener('click', () => applyDim(!document.body.classList.contains('dim')));
  applyDim(store.get('dim', false));

  // ---- polling: only while the page is open and visible --------------------
  window.addEventListener('hashchange', () => route());
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && token) { checkHealth(); route(true); }
  });
  setInterval(() => {
    if (document.visibilityState === 'visible' && token) { checkHealth(); route(true); }
  }, 30000);

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/app/sw.js', { scope: '/app/' }).catch(() => { /* offline shell is a bonus */ });
  }
  if (store.get('lastGood', null) && !navigator.onLine) lamp('bad');
  checkHealth();
  route();
})();
