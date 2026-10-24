// Pure helpers in static/app.js, run with `node --test` (no DOM needed).
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const PB = require(path.join(__dirname, '..', 'static', 'app.js'));

test('a 50 KB body collapses, cut on a word, with a marker', () => {
  const text = ('word '.repeat(10000)).trim();
  const c = PB.collapse(text);
  assert.equal(c.more, true);
  assert.ok(c.short.length <= PB.COLLAPSE_AT + 2);
  assert.ok(c.short.endsWith(' …'));
  assert.ok(!c.short.slice(0, -2).endsWith(' '));
});

test('a short body is left alone', () => {
  assert.deepEqual(PB.collapse('hello'), { short: 'hello', more: false });
});

test('one giant word still collapses', () => {
  const c = PB.collapse('x'.repeat(5000));
  assert.equal(c.more, true);
  assert.equal(c.short, 'x'.repeat(PB.COLLAPSE_AT) + ' …');
});

test('ago reads like a person', () => {
  const now = Date.parse('2026-10-23T20:00:00Z');
  assert.equal(PB.ago('2026-10-23T19:59:30Z', now), 'just now');
  assert.equal(PB.ago('2026-10-23T19:48:00Z', now), '12 min ago');
  assert.equal(PB.ago('2026-10-23T19:00:00Z', now), '1 hour ago');
  assert.equal(PB.ago('2026-10-23T15:00:00Z', now), '5 hours ago');
  assert.equal(PB.ago('2026-10-21T20:00:00Z', now), '2 days ago');
  assert.equal(PB.ago(null, now), '');
  assert.equal(PB.ago('not a date', now), '');
});

test('receipt lines come from the dispatcher reply', () => {
  assert.deepEqual(PB.receiptLine({ ok: true, to: 'CC', id: 5191, state: 'SENT' }, 'CC'),
    { cls: '', text: 'Sent to CC — #5191' });
  assert.equal(PB.receiptLine({ ok: true, to: 'CC', id: 7, state: 'HELD' }).cls, 'held');
  const bad = PB.receiptLine({ ok: false, to: 'CC4', error: 'unknown agent' }, 'Archie');
  assert.equal(bad.cls, 'bad');
  assert.match(bad.text, /Archie: unknown agent/);
});

test('a reply goes to the other side, on its newest letter', () => {
  const msgs = [{ id: 1, from: 'Guy', to: 'CC' }, { id: 2, from: 'CC', to: 'Guy' }, { id: 3, from: 'Guy', to: 'CC' }];
  assert.deepEqual(PB.replyTarget(msgs, 'Guy'), { to: 'CC', reply_to: 2 });
  assert.deepEqual(PB.replyTarget([{ id: 9, from: 'Guy', to: 'Opie' }], 'Guy'), { to: 'Opie', reply_to: 9 });
  assert.equal(PB.replyTarget([], 'Guy'), null);
});

test('state notes are plain words', () => {
  assert.deepEqual(PB.stateNote('FAILED'), ['didn’t arrive', 'bad']);
  assert.equal(PB.stateNote('DELIVERED'), null);
});
