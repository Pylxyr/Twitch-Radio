'use strict';
// `node --check` on every JS file of the GUI (main, preload, renderer, scripts, tests).
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const root = path.resolve(__dirname, '..');
const skip = new Set(['node_modules', 'dist']);

function* walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (skip.has(entry.name)) continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(full);
    else if (entry.name.endsWith('.js')) yield full;
  }
}

let failed = 0;
let count = 0;
for (const file of walk(root)) {
  count += 1;
  const result = spawnSync(process.execPath, ['--check', file], { encoding: 'utf8' });
  if (result.status !== 0) {
    failed += 1;
    process.stderr.write(`${path.relative(root, file)}\n${result.stderr}\n`);
  }
}
console.log(`${count - failed}/${count} files pass node --check`);
process.exit(failed ? 1 : 0);
