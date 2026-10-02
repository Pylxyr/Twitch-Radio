'use strict';
/**
 * App self-update check: asks the GitHub Releases API for the latest release
 * and compares it with the running version. It only reports and links; nothing
 * is downloaded or installed here.
 */
const UPDATE_REPO = 'Pylxyr/Twitch-Radio';
const DAY_MS = 24 * 60 * 60 * 1000;

function parseVersion(text) {
  const match = /^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$/.exec(String(text || '').trim());
  if (!match) return null;
  return { core: [Number(match[1]), Number(match[2]), Number(match[3])], pre: match[4] ? match[4].split('.') : null };
}

function comparePrerelease(a, b) {
  if (!a && !b) return 0;
  if (!a) return 1; // a release outranks its own pre-releases
  if (!b) return -1;
  for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
    if (a[i] === undefined) return -1;
    if (b[i] === undefined) return 1;
    const na = /^\d+$/.test(a[i]);
    const nb = /^\d+$/.test(b[i]);
    if (na && nb) {
      if (Number(a[i]) !== Number(b[i])) return Number(a[i]) < Number(b[i]) ? -1 : 1;
    } else if (na !== nb) {
      return na ? -1 : 1;
    } else if (a[i] !== b[i]) {
      return a[i] < b[i] ? -1 : 1;
    }
  }
  return 0;
}

/** -1, 0 or 1; null if either side isn't a version. */
function compareVersions(a, b) {
  const left = parseVersion(a);
  const right = parseVersion(b);
  if (!left || !right) return null;
  for (let i = 0; i < 3; i += 1) {
    if (left.core[i] !== right.core[i]) return left.core[i] < right.core[i] ? -1 : 1;
  }
  return comparePrerelease(left.pre, right.pre);
}

/** One automatic check per day, and only if the user hasn't turned it off. */
function shouldAutoCheck(prefs, now = Date.now()) {
  if (!prefs.checkAppUpdates) return false;
  const last = Number(prefs.lastUpdateCheck) || 0;
  return last > now || now - last >= DAY_MS;
}

// GitHub turns spaces in asset names into dots, so both spellings are accepted.
const INSTALLER_ASSET = {
  win32: /^Twitch[ .]Radio[ .]Setup[ .].*\.exe$/,
  linux: /^Twitch[ .-]Radio[ .-].*linux-x64\.AppImage$/,
};

function pickLinks(release, repo, platform) {
  const prefix = `https://github.com/${repo}/`;
  const page = typeof release.html_url === 'string' && release.html_url.startsWith(prefix) ? release.html_url : `${prefix}releases/latest`;
  const assets = Array.isArray(release.assets) ? release.assets : [];
  const wanted = INSTALLER_ASSET[platform];
  const installer = wanted && assets.find((asset) => wanted.test(String(asset.name)) && String(asset.browser_download_url).startsWith(prefix));
  return { releaseUrl: page, installerUrl: installer ? installer.browser_download_url : null };
}

async function checkForAppUpdate({ current, fetchImpl = fetch, repo = UPDATE_REPO, timeoutMs = 15000, platform = process.platform }) {
  const result = { ok: false, current, latest: null, updateAvailable: false, releaseUrl: null, installerUrl: null, error: null };
  try {
    const response = await fetchImpl(`https://api.github.com/repos/${repo}/releases/latest`, {
      headers: { Accept: 'application/vnd.github+json', 'User-Agent': 'TwitchRadio-update-check' },
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (response.status === 404) throw new Error('No release has been published yet.');
    if (!response.ok) throw new Error(`GitHub answered ${response.status}.`);
    const release = await response.json();
    const tag = String(release.tag_name || '');
    const order = compareVersions(tag, current);
    if (order === null) throw new Error(`Couldn't read the version "${tag}".`);
    Object.assign(result, { ok: true, latest: tag.replace(/^v/, ''), updateAvailable: order > 0 }, pickLinks(release, repo, platform));
  } catch (error) {
    result.error = error && error.message ? error.message : String(error);
  }
  return result;
}

module.exports = { UPDATE_REPO, DAY_MS, parseVersion, compareVersions, shouldAutoCheck, checkForAppUpdate };
