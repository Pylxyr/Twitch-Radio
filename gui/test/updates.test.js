'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { compareVersions, shouldAutoCheck, checkForAppUpdate, DAY_MS } = require('../updates');

test('compareVersions orders releases and pre-releases', () => {
  assert.equal(compareVersions('v1.1.0', '1.0.9'), 1);
  assert.equal(compareVersions('1.0.0', 'v1.0.0'), 0);
  assert.equal(compareVersions('1.10.0', '1.9.0'), 1);
  assert.equal(compareVersions('1.1.0-rc1', '1.1.0'), -1);
  assert.equal(compareVersions('1.1.0-rc1', '1.1.0-rc2'), -1);
  assert.equal(compareVersions('1.1.0-rc.2', '1.1.0-rc.10'), -1);
  assert.equal(compareVersions('nonsense', '1.0.0'), null);
});

test('shouldAutoCheck allows one check per day and honours the pref', () => {
  const now = 10 * DAY_MS;
  assert.equal(shouldAutoCheck({ checkAppUpdates: false, lastUpdateCheck: 0 }, now), false);
  assert.equal(shouldAutoCheck({ checkAppUpdates: true, lastUpdateCheck: 0 }, now), true);
  assert.equal(shouldAutoCheck({ checkAppUpdates: true, lastUpdateCheck: now - 1000 }, now), false);
  assert.equal(shouldAutoCheck({ checkAppUpdates: true, lastUpdateCheck: now - DAY_MS }, now), true);
  assert.equal(shouldAutoCheck({ checkAppUpdates: true, lastUpdateCheck: now + 5000 }, now), true);
});

const reply = (status, body) => async () => ({ status, ok: status >= 200 && status < 300, json: async () => body });

test('checkForAppUpdate reports a newer release with links on the repo only', async () => {
  const body = {
    tag_name: 'v1.2.0',
    html_url: 'https://github.com/Pylxyr/Twitch-Radio/releases/tag/v1.2.0',
    assets: [
      { name: 'SHA256SUMS.txt', browser_download_url: 'https://github.com/Pylxyr/Twitch-Radio/releases/download/v1.2.0/SHA256SUMS.txt' },
      { name: 'Twitch.Radio.Setup.1.2.0.exe', browser_download_url: 'https://github.com/Pylxyr/Twitch-Radio/releases/download/v1.2.0/Twitch.Radio.Setup.1.2.0.exe' },
    ],
  };
  const result = await checkForAppUpdate({ current: '1.0.0', fetchImpl: reply(200, body), platform: 'win32' });
  assert.equal(result.ok, true);
  assert.equal(result.updateAvailable, true);
  assert.equal(result.latest, '1.2.0');
  assert.match(result.installerUrl, /Setup/);
});

test('checkForAppUpdate links the AppImage on Linux and the installer on Windows', async () => {
  const base = 'https://github.com/Pylxyr/Twitch-Radio/releases/download/v1.2.0/';
  const body = {
    tag_name: 'v1.2.0',
    html_url: 'https://github.com/Pylxyr/Twitch-Radio/releases/tag/v1.2.0',
    assets: [
      { name: 'Twitch.Radio.Setup.1.2.0.exe', browser_download_url: `${base}Twitch.Radio.Setup.1.2.0.exe` },
      { name: 'Twitch-Radio-1.2.0-linux-x64.tar.gz', browser_download_url: `${base}Twitch-Radio-1.2.0-linux-x64.tar.gz` },
      { name: 'Twitch-Radio-1.2.0-linux-x64.AppImage', browser_download_url: `${base}Twitch-Radio-1.2.0-linux-x64.AppImage` },
    ],
  };
  const linux = await checkForAppUpdate({ current: '1.0.0', fetchImpl: reply(200, body), platform: 'linux' });
  assert.match(linux.installerUrl, /linux-x64\.AppImage$/);
  const win = await checkForAppUpdate({ current: '1.0.0', fetchImpl: reply(200, body), platform: 'win32' });
  assert.match(win.installerUrl, /Setup\.1\.2\.0\.exe$/);
  const mac = await checkForAppUpdate({ current: '1.0.0', fetchImpl: reply(200, body), platform: 'darwin' });
  assert.equal(mac.installerUrl, null);
  assert.match(mac.releaseUrl, /releases\/tag/);
});

test('checkForAppUpdate ignores foreign URLs and handles errors', async () => {
  const evil = { tag_name: 'v9.0.0', html_url: 'https://evil.example/x', assets: [{ name: 'Twitch Radio Setup 9.0.0.exe', browser_download_url: 'https://evil.example/a.exe' }] };
  const result = await checkForAppUpdate({ current: '1.0.0', fetchImpl: reply(200, evil), platform: 'win32' });
  assert.equal(result.installerUrl, null);
  assert.match(result.releaseUrl, /^https:\/\/github\.com\/Pylxyr\/Twitch-Radio\//);
  const upToDate = await checkForAppUpdate({ current: '1.2.0', fetchImpl: reply(200, { tag_name: 'v1.2.0' }) });
  assert.equal(upToDate.updateAvailable, false);
  assert.equal((await checkForAppUpdate({ current: '1.0.0', fetchImpl: reply(404, {}) })).ok, false);
  assert.equal((await checkForAppUpdate({ current: '1.0.0', fetchImpl: async () => { throw new Error('offline'); } })).error, 'offline');
});
