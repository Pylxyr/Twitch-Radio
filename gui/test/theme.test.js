'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { WINDOW_COLORS, windowColorsFor, resolveTheme } = require('../theme');
const Skins = require('../renderer/skins');

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

/** WCAG contrast ratio between two #rrggbb colours. */
function contrast(a, b) {
  const relative = (hex) => {
    const [r, g, bl] = [1, 3, 5].map((i) => {
      const c = Number.parseInt(hex.slice(i, i + 2), 16) / 255;
      return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * bl;
  };
  const [hi, lo] = [relative(a), relative(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}
const luminanceOf = (hex) => {
  const [r, g, b] = [1, 3, 5].map((i) => Number.parseInt(hex.slice(i, i + 2), 16) / 255);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};

test('colour styles: the default changes nothing, the others only override known colour variables', () => {
  assert.deepEqual(Skins.skinVariables('default', 'dark'), {});
  assert.deepEqual(Skins.skinVariables('default', 'light'), {});
  assert.deepEqual(Skins.skinVariables('no-such-style', 'dark'), {}, 'an unknown name falls back to the default');
  assert.ok(Skins.SKIN_NAMES.length >= 6, 'a handful of styles to choose from');
  for (const name of Skins.SKIN_NAMES) {
    for (const mode of ['dark', 'light']) {
      for (const key of Object.keys(Skins.skinVariables(name, mode))) assert.ok(dark.has(key), `${name}/${mode}: ${key} is not a palette variable`);
    }
  }
});

test('every colour style stays really dark in dark mode and really light in light mode', () => {
  for (const name of Skins.SKIN_NAMES) {
    const d = Skins.skinVariables(name, 'dark');
    const l = Skins.skinVariables(name, 'light');
    if (Object.keys(d).length) assert.ok(luminanceOf(d['--bg']) < 0.1 && luminanceOf(d['--panel']) < 0.15, `${name} dark is too bright`);
    if (Object.keys(l).length) assert.ok(luminanceOf(l['--bg']) > 0.85 && luminanceOf(l['--panel']) > 0.9, `${name} light is too dark`);
    for (const vars of [d, l]) {
      if (!Object.keys(vars).length) continue;
      assert.ok(contrast('#ffffff', vars['--btn-accent']) >= 4.5, `${name}: white text on a primary button is hard to read`);
      assert.ok(contrast('#ffffff', vars['--btn-accent-hover']) >= 4.5, `${name}: white text on a hovered primary button is hard to read`);
      assert.ok(contrast(vars['--accent-text'], vars['--panel']) >= 4.5, `${name}: accent-coloured text on a card is hard to read`);
    }
  }
});

test('the window chrome matches the page background in every colour style', () => {
  for (const name of Skins.SKIN_NAMES) {
    for (const [mode, base] of [['dark', dark], ['light', light]]) {
      const expected = (Skins.skinVariables(name, mode)['--bg'] || base.get('--bg')).toLowerCase();
      assert.equal(windowColorsFor(name, mode).background.toLowerCase(), expected, `${name}/${mode}`);
    }
  }
  assert.deepEqual(windowColorsFor('default', 'dark'), WINDOW_COLORS.dark);
});

test('the base palettes: white labels on buttons and badges read, and hover never lowers the contrast', () => {
  for (const [mode, vars] of [['dark', dark], ['light', light]]) {
    for (const key of ['--btn-accent', '--btn-accent-hover', '--err-fill']) {
      assert.ok(contrast('#ffffff', vars.get(key)) >= 4.5, `${mode}: white on ${key} is hard to read`);
    }
    assert.ok(contrast('#ffffff', vars.get('--btn-accent-hover')) >= contrast('#ffffff', vars.get('--btn-accent')), `${mode}: hover lowers contrast`);
  }
});
