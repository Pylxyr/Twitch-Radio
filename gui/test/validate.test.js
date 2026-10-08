'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { cleanQuery, normalizeLookahead, isTheme, LOOKAHEAD_MIN, LOOKAHEAD_MAX, LOOKAHEAD_DEFAULT, LOOKAHEAD_DEFAULT_ENABLED, MAX_QUERY_LENGTH } = require('../validate');

test('cleanQuery tidies one line and refuses nothing-or-too-much', () => {
  assert.equal(cleanQuery('  daft   punk\none more time '), 'daft punk one more time');
  assert.equal(cleanQuery('https://www.youtube.com/watch?v=abc'), 'https://www.youtube.com/watch?v=abc');
  for (const bad of ['', '   ', '\n\t', null, undefined, 42, {}, ['x'], 'x'.repeat(MAX_QUERY_LENGTH + 1)]) assert.equal(cleanQuery(bad), null);
  assert.equal(cleanQuery('x'.repeat(MAX_QUERY_LENGTH)).length, MAX_QUERY_LENGTH);
});

test('normalizeLookahead always returns a usable, in-range setting', () => {
  assert.deepEqual(normalizeLookahead({}), { enabled: LOOKAHEAD_DEFAULT_ENABLED, count: LOOKAHEAD_DEFAULT });
  assert.deepEqual(normalizeLookahead(undefined), { enabled: true, count: 5 });
  assert.deepEqual(normalizeLookahead('junk'), { enabled: true, count: 5 });
  assert.deepEqual(normalizeLookahead({ enabled: false, count: 9 }), { enabled: false, count: 9 });
  assert.deepEqual(normalizeLookahead({ enabled: 'no', count: '7' }), { enabled: true, count: 7 });
  assert.equal(normalizeLookahead({ count: 0 }).count, LOOKAHEAD_MIN);
  assert.equal(normalizeLookahead({ count: -4 }).count, LOOKAHEAD_MIN);
  assert.equal(normalizeLookahead({ count: 99 }).count, LOOKAHEAD_MAX);
  for (const bad of [NaN, Infinity, null, [], {}, true, 'many']) assert.equal(normalizeLookahead({ count: bad }).count, LOOKAHEAD_DEFAULT, String(bad));
});

test('isTheme accepts exactly the three appearance choices', () => {
  for (const ok of ['system', 'light', 'dark']) assert.equal(isTheme(ok), true);
  for (const bad of ['Light', '', null, undefined, 1, 'auto', {}]) assert.equal(isTheme(bad), false);
});

test('the dashboard asks for the same 1 to 15 range the core enforces', () => {
  const html = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'index.html'), 'utf8');
  assert.match(html, /id="lookaheadCount"[^>]*min="1"[^>]*max="15"/);
  const app = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'app.js'), 'utf8');
  assert.match(app, /Math\.max\(1, Math\.min\(15,/);
  assert.equal(`${LOOKAHEAD_MIN}-${LOOKAHEAD_MAX}`, '1-15');
});

test('modes: only the two known names are accepted', () => {
  const { isMode, MODES } = require('../validate');
  assert.deepEqual(MODES, ['twitch', 'player']);
  assert.equal(isMode('twitch'), true);
  assert.equal(isMode('player'), true);
  for (const bad of ['', 'Twitch', 'both', null, undefined, 1, {}]) assert.equal(isMode(bad), false, String(bad));
});

test('volume: always a number from 0 to 1', () => {
  const { normalizeVolume, VOLUME_DEFAULT } = require('../validate');
  assert.equal(normalizeVolume(0.25), 0.25);
  assert.equal(normalizeVolume(0), 0);
  assert.equal(normalizeVolume(7), 1);
  assert.equal(normalizeVolume(-3), 0);
  for (const bad of ['0.5', null, undefined, NaN, Infinity, {}]) assert.equal(normalizeVolume(bad), VOLUME_DEFAULT, String(bad));
});

test('colour styles: only known names are accepted', () => {
  const { isSkin } = require('../validate');
  assert.equal(isSkin('default'), true);
  assert.equal(isSkin('midnight'), true);
  for (const bad of ['', 'Midnight', 'constructor', '__proto__', null, undefined, 3]) assert.equal(isSkin(bad), false, String(bad));
});
