'use strict';
// The DOM turns a null argument of append()/replaceChildren() into the text "null" - that is how the
// word showed up in Settings. h() in app.js skips null children, but a direct call does not, so a
// `cond ? node : null` passed straight to one of them is a bug waiting to be seen. This reads app.js and
// fails on any such call.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'app.js'), 'utf8');

/** Top-level arguments of every `.append(` / `.replaceChildren(` call, found by balancing parentheses. */
function directCalls(text) {
  const calls = [];
  const re = /\.(append|replaceChildren)\(/g;
  let m;
  while ((m = re.exec(text))) {
    let depth = 1;
    let i = re.lastIndex;
    let top = '';
    for (; i < text.length && depth > 0; i++) {
      const c = text[i];
      if (c === "'" || c === '"' || c === '`') {
        const quote = c;
        for (i++; i < text.length && text[i] !== quote; i++) if (text[i] === '\\') i++;
        top += '""';
        continue;
      }
      if (c === '(' || c === '[' || c === '{') depth++;
      else if (c === ')' || c === ']' || c === '}') depth--;
      if (depth >= 1 && (depth === 1 || c === ',')) top += depth === 1 ? c : '';
      else if (depth === 0) break;
    }
    calls.push({ at: text.slice(0, m.index).split('\n').length, args: top });
  }
  return calls;
}

test('no append()/replaceChildren() call passes a possibly-null child directly', () => {
  const bad = directCalls(source).filter((c) => /[?:]\s*null\b|\bnull\s*[,)]|\|\|\s*null\b/.test(c.args));
  assert.deepEqual(bad.map((c) => `app.js line ${c.at}`), []);
});

test('the helper that replaced them leaves null and false out', () => {
  assert.match(source, /function replaceKids\(parent, \.\.\.nodes\)/);
  assert.match(source, /filter\(\(node\) => node != null && node !== false\)/);
});
