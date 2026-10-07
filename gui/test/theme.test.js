'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { WINDOW_COLORS, resolveTheme } = require('../theme');

// Git checks files out with CRLF line endings on Windows (see .gitattributes); the patterns below use \n.
const css = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'styles.css'), 'utf8').replace(/\r\n/g, '\n');

/** The `--name: value` pairs inside a block that starts at `opener`. */
function variablesIn(opener) {
  const start = css.indexOf(opener);
  assert.ok(start >= 0, `${opener} not found`);
  let depth = 0;
  let end = start;
  for (let i = css.indexOf('{', start); i < css.length; i++) {
    if (css[i] === '{') depth++;
    if (css[i] === '}' && --depth === 0) {
      end = i;
      break;
    }
  }
  const vars = new Map();
  for (const m of css.slice(start, end).matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)) vars.set(m[1], m[2].trim());
  return vars;
}
const dark = variablesIn(':root {');
const light = variablesIn('@media (prefers-color-scheme: light)');

test('every colour variable of the dark palette has a light counterpart, and nothing extra', () => {
  const sizes = new Set(['--radius', '--font', '--mono']); // not colours
  const darkColours = [...dark.keys()].filter((k) => !sizes.has(k));
  assert.ok(darkColours.length > 40, 'the dark palette looks too small');
  assert.deepEqual([...light.keys()].sort(), darkColours.sort());
});

test('the light palette is really light, the dark one really dark', () => {
  const luminance = (hex) => {
    const [r, g, b] = [1, 3, 5].map((i) => Number.parseInt(hex.slice(i, i + 2), 16) / 255);
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  assert.ok(luminance(dark.get('--bg')) < 0.1 && luminance(dark.get('--text')) > 0.7);
  assert.ok(luminance(light.get('--bg')) > 0.85 && luminance(light.get('--text')) < 0.2);
  assert.ok(luminance(light.get('--panel')) > luminance(light.get('--bg')) - 0.001, 'cards are at least as light as the page');
});

test('no colour literal is left outside the two palettes (so neither theme can be broken by a stray one)', () => {
  const stripped = css.replace(/:root \{[^}]*\}/, '').replace(/@media \(prefers-color-scheme: light\) \{[\s\S]*?\n\}\n/, '');
  const literals = [...stripped.matchAll(/#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)/g)].map((m) => m[0]);
  // Allowed: white text on the accent colour, the switch knob, the dark text on the "done" check,
  // and the green pulse glow, which reads on both backgrounds.
  const allowed = new Set(['#fff', '#06251a', 'rgba(61,220,151,.6)', 'rgba(61,220,151,0)', 'rgba(0,0,0,.4)', 'rgba(0,0,0,.35)']);
  const stray = literals.filter((l) => !allowed.has(l.replace(/\s+/g, '')) && !/^rgba\(0,0,0,/.test(l.replace(/\s+/g, ''))); // plain black shadows are fine on both
  assert.deepEqual(stray, [], `colours outside the palettes: ${stray.join(', ')}`);
  for (const m of stripped.matchAll(/var\((--[a-z0-9-]+)\)/g)) assert.ok(dark.has(m[1]), `${m[1]} is used but not defined`);
});

test('the window chrome colours match the page background in each theme', () => {
  assert.equal(WINDOW_COLORS.dark.background.toLowerCase(), dark.get('--bg').toLowerCase());
  assert.equal(WINDOW_COLORS.light.background.toLowerCase(), light.get('--bg').toLowerCase());
});

test('resolveTheme: an explicit choice wins, "system" follows the OS', () => {
  assert.equal(resolveTheme('light', true), 'light');
  assert.equal(resolveTheme('dark', false), 'dark');
  assert.equal(resolveTheme('system', true), 'dark');
  assert.equal(resolveTheme('system', false), 'light');
  assert.equal(resolveTheme('garbage', false), 'light');
});
