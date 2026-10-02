'use strict';
/**
 * Lightweight validation of the electron-builder config in package.json,
 * without running electron-builder. Confirms that everything the config
 * references from the repo exists. Build outputs (../dist/core, ../packaging/bin)
 * are only required with --require-built, which the release job uses.
 */
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
const build = pkg.build || {};
const requireBuilt = process.argv.includes('--require-built');
const problems = [];
const fail = (message) => problems.push(message);

const exists = (rel, base = root) => fs.existsSync(path.resolve(base, rel));
const hasGlob = (text) => /[*?[\]{}]/.test(text);

if (!/^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$/.test(pkg.version || '')) fail(`package.json version "${pkg.version}" is not semver`);
if (!/^[a-z0-9]+(\.[a-z0-9-]+)+$/i.test(build.appId || '')) fail(`build.appId "${build.appId}" looks invalid`);
for (const key of ['productName', 'copyright']) if (!build[key]) fail(`build.${key} is missing`);
if (!pkg.main || !exists(pkg.main)) fail(`"main" (${pkg.main}) does not exist`);
for (const [name, range] of Object.entries({ ...pkg.dependencies, ...pkg.devDependencies })) {
  if (!/^\d+\.\d+\.\d+/.test(range)) fail(`${name} is not pinned to an exact version (${range})`);
}

const buildResources = (build.directories && build.directories.buildResources) || 'build';
if (!exists(buildResources)) fail(`buildResources folder "${buildResources}" is missing`);
for (const icon of ['icon.ico', 'icon.png', 'tray.png', 'tray@2x.png']) {
  if (!exists(path.join(buildResources, icon))) fail(`${buildResources}/${icon} is missing (is it git-ignored?)`);
}
if (build.win && build.win.icon && !exists(build.win.icon)) fail(`win.icon ${build.win.icon} does not exist`);

for (const pattern of build.files || []) {
  if (pattern.startsWith('!')) continue;
  const base = pattern.split('/**')[0].split('*')[0];
  if (!base || !exists(base.replace(/\/$/, ''))) fail(`files entry "${pattern}" matches nothing in the repo`);
  else if (hasGlob(pattern) && base !== pattern && fs.statSync(path.join(root, base)).isDirectory() && !fs.readdirSync(path.join(root, base)).length) fail(`files entry "${pattern}" is an empty folder`);
}

for (const entry of build.extraResources || []) {
  const from = typeof entry === 'string' ? entry : entry.from;
  const builtOutput = from.includes('dist/') || from.includes('packaging/bin');
  if (!exists(from)) {
    if (!builtOutput) fail(`extraResources "${from}" does not exist`);
    else if (requireBuilt) fail(`build output "${from}" is missing - run the core build and fetch-tools first`);
  }
}

// Files the main process loads from the app folder.
for (const rel of ['renderer/index.html', 'renderer/app.js', 'renderer/styles.css', 'renderer/icon.png', 'renderer/tray.png', 'renderer/logo.png', 'preload.js']) {
  if (!exists(rel)) fail(`${rel} is missing (loaded by main.js or index.html)`);
}

if (problems.length) {
  console.error(`electron-builder config check found ${problems.length} problem(s):`);
  for (const problem of problems) console.error(`  - ${problem}`);
  process.exit(1);
}
console.log(`electron-builder config OK (${build.productName} ${pkg.version}${requireBuilt ? ', build outputs present' : ''})`);
