'use strict';
/**
 * Twitch Radio desktop shell.
 *
 * Owns three things: the window/tray, the bot process (a hidden child that
 * speaks JSON lines on stdin/stdout - protocol documented in
 * twitch_radio/headless.py), and the config files the Settings tab edits.
 * All UI lives in renderer/; it only sees the functions in preload.js.
 */
const { app, BrowserWindow, Tray, Menu, ipcMain, shell, dialog, nativeImage } = require('electron');
const path = require('path');
const fs = require('fs');
const readline = require('readline');
const { spawn, execFile } = require('child_process');
const { checkForAppUpdate, shouldAutoCheck } = require('./updates');

const IS_WIN = process.platform === 'win32';
const PROJECT_ROOT = path.resolve(__dirname, '..');
const APP_DATA = app.getPath('appData');
const HOME = process.env.TWITCH_RADIO_HOME || (app.isPackaged ? path.join(APP_DATA, 'TwitchRadio') : PROJECT_ROOT);
// GUI-only files (window prefs, Chromium cache) live beside, not inside, the bot's data.
app.setPath('userData', path.join(APP_DATA, 'TwitchRadio', 'gui'));

const STOP_GRACE_MS = 20000;
const LOG_LIMIT = 5000;
const LOG_FLUSH_MS = 100;

// ---------------------------------------------------------------- prefs ---
const DEFAULT_PREFS = {
  askOnClose: true, // when the bot is running and the window is closed
  closeAction: 'tray', // remembered answer when askOnClose is off: 'tray' | 'quit'
  autoRestart: true, // restart after a crash (never after a config error)
  startBotOnLaunch: false,
  launchAtLogin: false,
  checkAppUpdates: true, // at most once a day
  checkYtdlpOnStartup: true,
  lastUpdateCheck: 0, // set by the app itself, not by the page
};
const PAGE_PREFS = new Set(Object.keys(DEFAULT_PREFS).filter((key) => key !== 'lastUpdateCheck'));
const prefsFile = () => path.join(app.getPath('userData'), 'gui-settings.json');
let prefs = { ...DEFAULT_PREFS };
function loadPrefs() {
  try {
    prefs = { ...DEFAULT_PREFS, ...JSON.parse(fs.readFileSync(prefsFile(), 'utf8')) };
  } catch {
    prefs = { ...DEFAULT_PREFS };
  }
}
function savePrefs() {
  fs.mkdirSync(path.dirname(prefsFile()), { recursive: true });
  atomicWrite(prefsFile(), JSON.stringify(prefs, null, 2));
}
function atomicWrite(file, text) {
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, text, 'utf8');
  fs.renameSync(tmp, file);
}

// ----------------------------------------------------------------- logs ---
let logSeq = 0;
const logBuffer = [];
let pendingLogs = [];
let flushTimer = null;

function pushLog(entry) {
  const item = {
    id: ++logSeq,
    ts: entry.ts || Date.now() / 1000,
    level: String(entry.level || 'INFO').toUpperCase(),
    logger: entry.logger || 'gui',
    msg: String(entry.msg ?? ''),
    cont: !!entry.cont,
  };
  logBuffer.push(item);
  if (logBuffer.length > LOG_LIMIT) logBuffer.splice(0, logBuffer.length - LOG_LIMIT);
  pendingLogs.push(item);
  if (!flushTimer) {
    flushTimer = setTimeout(() => {
      flushTimer = null;
      const batch = pendingLogs;
      pendingLogs = [];
      send('logs:batch', batch);
    }, LOG_FLUSH_MS);
  }
}
const guiLog = (level, msg) => pushLog({ level, logger: 'gui', msg });

// ----------------------------------------------------------- bot process ---
function coreCommand() {
  if (app.isPackaged) {
    return { cmd: path.join(process.resourcesPath, 'core', IS_WIN ? 'TwitchRadioCore.exe' : 'TwitchRadioCore'), args: [] };
  }
  let python = process.env.TWITCH_RADIO_PYTHON;
  if (!python) {
    const venv = path.join(PROJECT_ROOT, '.venv', IS_WIN ? 'Scripts/python.exe' : 'bin/python');
    python = fs.existsSync(venv) ? venv : IS_WIN ? 'python' : 'python3';
  }
  return { cmd: python, args: [path.join(PROJECT_ROOT, 'bot.py')] };
}
function coreEnv() {
  const env = { ...process.env, TWITCH_RADIO_HOME: HOME, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' };
  const bin = app.isPackaged ? path.join(process.resourcesPath, 'bin') : path.join(PROJECT_ROOT, 'packaging', 'bin');
  if (fs.existsSync(bin)) env.TWITCH_RADIO_BIN = bin;
  // The installer ships no JavaScript runtime for yt-dlp: with ELECTRON_RUN_AS_NODE=1 this executable
  // is Node (the core sets that variable itself, see twitch_radio/config.py). Needs Electron's runAsNode
  // fuse left on, which scripts/check_host_runtime.py verifies against the built app in CI.
  if (app.isPackaged) env.TWITCH_RADIO_HOST_JS_EXE = process.execPath;
  return env;
}

/** One-shot run of the core (preflight, env read/write, yt-dlp update checks). */
function runCore(extraArgs, input, timeout = 30000) {
  return new Promise((resolve) => {
    const { cmd, args } = coreCommand();
    const child = execFile(
      cmd,
      [...args, ...extraArgs],
      { env: coreEnv(), windowsHide: true, timeout, maxBuffer: 4 * 1024 * 1024, encoding: 'utf8', cwd: HOME_CWD() },
      (error, stdout, stderr) => resolve({ code: error ? (typeof error.code === 'number' ? error.code : 1) : 0, stdout, stderr, error }),
    );
    if (input !== undefined) child.stdin.end(input);
  });
}
function HOME_CWD() {
  try {
    fs.mkdirSync(HOME, { recursive: true });
    return HOME;
  } catch {
    return PROJECT_ROOT;
  }
}
function lastJsonLine(text) {
  const lines = String(text || '').trim().split(/\r?\n/).reverse();
  for (const line of lines) {
    try {
      return JSON.parse(line);
    } catch {
      /* keep looking: warnings may precede the report */
    }
  }
  return null;
}

const core = {
  child: null,
  phase: 'stopped', // stopped | starting | running | stopping | error
  fatal: null,
  exitCode: null,
  userStopped: false,
  stopWaiters: [],
  stopTimer: null,
  pendingAcks: new Map(),
  ackId: 0,
  lastState: null,
  watching: false, // whether the core has been told the dashboard is on screen
  crashTimes: [],
  restartTimer: null,
  restartAt: null,
  startedAt: null,
};

function status() {
  return {
    phase: core.phase,
    pid: core.child ? core.child.pid : null,
    fatal: core.fatal,
    exitCode: core.exitCode,
    restartAt: core.restartAt,
    startedAt: core.startedAt,
    home: HOME,
  };
}
function setPhase(phase) {
  if (core.phase === phase) return;
  core.phase = phase;
  broadcastStatus();
}
function broadcastStatus() {
  send('bot:status', status());
  refreshTray();
}

function startBot() {
  if (core.child) return status();
  clearTimeout(core.restartTimer);
  core.restartAt = null;
  core.fatal = null;
  core.exitCode = null;
  core.userStopped = false;
  core.lastState = null;
  core.watching = false;
  core.startedAt = Date.now();
  const { cmd, args } = coreCommand();
  guiLog('INFO', `Starting the bot (${path.basename(cmd)}).`);
  let child;
  try {
    child = spawn(cmd, [...args, '--headless'], {
      env: coreEnv(),
      cwd: HOME_CWD(),
      windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'],
    });
  } catch (error) {
    return failStart(`Couldn't launch the bot: ${error.message}`);
  }
  core.child = child;
  setPhase('starting');

  readline.createInterface({ input: child.stdout, crlfDelay: Infinity }).on('line', onCoreLine);
  readline.createInterface({ input: child.stderr, crlfDelay: Infinity }).on('line', (line) => {
    if (!line.trim()) return;
    pushLog({ level: /Traceback|Error|Exception/.test(line) ? 'ERROR' : 'WARNING', logger: 'stderr', msg: line });
  });
  child.stdin.on('error', () => {
    /* the core already exited; the 'exit' handler reports it */
  });
  child.once('error', (error) => {
    core.child = null;
    failStart(`Couldn't launch the bot: ${error.message}`);
  });
  child.once('exit', (code, signal) => onCoreExit(code, signal));
  updateWatching(true);
  return status();
}

function failStart(message) {
  core.fatal = { message, hint: 'Reinstall the app, or check that antivirus did not quarantine TwitchRadioCore.exe.', code: 3 };
  guiLog('ERROR', message);
  setPhase('error');
  return status();
}

function onCoreLine(line) {
  if (!line.trim()) return;
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    pushLog({ level: 'INFO', logger: 'core', msg: line });
    return;
  }
  switch (msg.t) {
    case 'log':
      pushLog(msg);
      break;
    case 'phase':
      if (msg.phase === 'running') setPhase('running');
      else if (msg.phase === 'stopping') setPhase('stopping');
      else if (msg.phase === 'starting' && core.phase === 'stopped') setPhase('starting');
      break;
    case 'state':
      core.lastState = msg;
      send('bot:state', msg);
      break;
    case 'fatal':
      core.fatal = { message: msg.message, hint: msg.hint, code: msg.code };
      break;
    case 'ack': {
      const waiter = core.pendingAcks.get(msg.id);
      if (waiter) {
        core.pendingAcks.delete(msg.id);
        waiter(msg);
      }
      break;
    }
    default:
      break; // hello and anything newer
  }
}

function onCoreExit(code, signal) {
  clearTimeout(core.stopTimer);
  core.child = null;
  core.exitCode = code;
  core.lastState = null;
  for (const waiter of core.pendingAcks.values()) waiter({ ok: false, error: 'bot stopped' });
  core.pendingAcks.clear();
  const clean = core.userStopped || code === 0;
  guiLog(clean ? 'INFO' : 'ERROR', `The bot ${clean ? 'stopped' : 'exited unexpectedly'} (${signal ? `signal ${signal}` : `code ${code}`}).`);
  if (!clean && !core.fatal) {
    core.fatal = { message: `The bot stopped unexpectedly (exit code ${code}).`, hint: 'See the Logs tab for details.', code: code ?? 3 };
  }
  setPhase(clean ? 'stopped' : 'error');
  send('bot:state', null);
  const waiters = core.stopWaiters;
  core.stopWaiters = [];
  waiters.forEach((resolve) => resolve());

  // Retry only what might fix itself (network down), never config errors, and
  // never in a tight loop.
  if (!clean && prefs.autoRestart && core.fatal && core.fatal.code !== 2 && !quitting) {
    const now = Date.now();
    core.crashTimes = core.crashTimes.filter((t) => now - t < 10 * 60 * 1000);
    core.crashTimes.push(now);
    if (core.crashTimes.length <= 5) {
      const delay = Math.min(60000, 3000 * 2 ** (core.crashTimes.length - 1));
      core.restartAt = now + delay;
      guiLog('WARNING', `Restarting automatically in ${Math.round(delay / 1000)}s (attempt ${core.crashTimes.length} of 5).`);
      core.restartTimer = setTimeout(startBot, delay);
      broadcastStatus();
    } else {
      guiLog('ERROR', 'Giving up on automatic restarts after 5 failures in 10 minutes.');
    }
  }
}

function stopBot() {
  clearTimeout(core.restartTimer);
  core.restartAt = null;
  if (!core.child) {
    if (core.phase === 'error') setPhase('stopped');
    return Promise.resolve();
  }
  const done = new Promise((resolve) => core.stopWaiters.push(resolve));
  if (!core.userStopped) {
    core.userStopped = true;
    setPhase('stopping');
    guiLog('INFO', 'Stop requested - shutting down gracefully.');
    try {
      core.child.stdin.write(JSON.stringify({ cmd: 'stop' }) + '\n');
    } catch {
      killTree();
    }
    core.stopTimer = setTimeout(() => {
      guiLog('WARNING', `The bot didn't stop within ${STOP_GRACE_MS / 1000}s - forcing it to close.`);
      killTree();
    }, STOP_GRACE_MS);
  }
  return done;
}
function killTree() {
  const child = core.child;
  if (!child) return;
  if (IS_WIN) execFile('taskkill', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true }, () => {});
  else child.kill('SIGKILL');
}
async function restartBot() {
  await stopBot();
  return startBot();
}
function sendCommand(name) {
  if (!core.child) return Promise.resolve({ ok: false, error: 'The bot is not running.' });
  return new Promise((resolve) => {
    const id = ++core.ackId;
    const timer = setTimeout(() => {
      core.pendingAcks.delete(id);
      resolve({ ok: false, error: 'No answer from the bot.' });
    }, 4000);
    core.pendingAcks.set(id, (ack) => {
      clearTimeout(timer);
      resolve(ack);
    });
    try {
      core.child.stdin.write(JSON.stringify({ cmd: name, id }) + '\n');
    } catch {
      clearTimeout(timer);
      core.pendingAcks.delete(id);
      resolve({ ok: false, error: 'The bot is not accepting commands.' });
    }
  });
}

/**
 * Tells the core whether the dashboard is on screen. Hidden, the core sends a
 * state message only when the player's state actually changes; visible, it also
 * sends a slow heartbeat. `force` re-sends the current answer (a fresh core
 * starts out assuming nobody is watching).
 */
function updateWatching(force = false) {
  const visible = !!(win && !win.isDestroyed() && win.isVisible() && !win.isMinimized());
  if (!force && visible === core.watching) return;
  core.watching = visible;
  if (!core.child) return;
  try {
    core.child.stdin.write(JSON.stringify({ cmd: 'watch', on: visible }) + '\n');
  } catch {
    /* the core is exiting; the 'exit' handler reports it */
  }
}

// ------------------------------------------------------- configuration ---
const ENV_KEYS = new Set([
  'TWITCH_CLIENT_ID', 'TWITCH_CLIENT_SECRET', 'TWITCH_BOT_ID', 'TWITCH_OWNER_ID',
  'AUDIO_BITRATE_KBPS', 'LOUDNESS_MODE', 'PAUSE_QUEUE_WHEN_NO_LISTENERS', 'TWITCH_NOWPLAYING_PORT',
  'YTDLP_COOKIES_FILE', 'YTDLP_CONCURRENCY', 'YTDLP_WORKER_IDLE_SECONDS', 'YTDLP_EXTRACT_TIMEOUT_SECONDS',
  'YTDLP_CACHE_TTL_SECONDS',
  'LOG_LEVEL', 'LOG_TO_FILE',
]);
let lastPreflight = null;
let lastPreflightAt = 0;
const PREFLIGHT_FRESH_MS = 120000;
/**
 * Runs the core's preflight (it starts a short-lived process, so nothing polls
 * it). A page that is just being reopened gets the cached report; callers that
 * know something changed pass force.
 */
async function preflight(force = true) {
  if (!force && lastPreflight && Date.now() - lastPreflightAt < PREFLIGHT_FRESH_MS) return lastPreflight;
  const result = await runCore(['--preflight']);
  const report = lastJsonLine(result.stdout);
  if (!report) {
    lastPreflight = { ok: false, config_ok: false, config_error: 'The bot core could not be started to check the setup.', detail: (result.stderr || String(result.error || '')).slice(-600) };
  } else {
    lastPreflight = report;
  }
  lastPreflightAt = Date.now();
  return lastPreflight;
}
function tunableSpec() {
  const spec = (lastPreflight && lastPreflight.tunables) || {};
  return { bounds: spec.bounds || {}, defaults: spec.defaults || {} };
}
async function liveFiles() {
  if (!lastPreflight || !lastPreflight.files || !lastPreflight.tunables) await preflight();
  const files = (lastPreflight && lastPreflight.files) || {};
  return {
    tunables: files.tunables || path.join(HOME, 'data', 'tunables.json'),
    toggles: files.toggles || path.join(HOME, 'data', 'toggles.json'),
  };
}
function readJson(file) {
  try {
    const value = JSON.parse(fs.readFileSync(file, 'utf8'));
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  } catch {
    return {};
  }
}

// Saves run one at a time: each is a read-modify-write of .env in a separate core process.
let saveChain = Promise.resolve();
function saveEnv(payload) {
  const run = saveChain.then(() => saveEnvNow(payload));
  saveChain = run.catch(() => {});
  return run;
}

async function saveEnvNow(payload) {
  const { values = {} } = payload || {};
  const updates = {};
  for (const [key, raw] of Object.entries(values)) {
    if (!ENV_KEYS.has(key)) continue;
    const value = String(raw ?? '').trim();
    if (key === 'TWITCH_CLIENT_SECRET' && !value) continue; // a blank never erases the stored secret
    updates[key] = value;
  }
  if (!Object.keys(updates).length) {
    return { ok: true, preflight: lastPreflight || (await preflight()), needsRestart: false };
  }
  const result = await runCore(['--env-update-stdin'], JSON.stringify(updates));
  if (result.code !== 0) return { ok: false, error: (result.stderr || '').trim() || "Couldn't save the settings." };
  guiLog('INFO', `Settings saved (${Object.keys(updates).length} value(s)).`);
  const report = await preflight();
  return { ok: true, preflight: report, needsRestart: core.phase !== 'stopped' && core.phase !== 'error' };
}

async function getEnv() {
  const result = await runCore(['--env-json']);
  const values = lastJsonLine(result.stdout) || {};
  const safe = {};
  for (const key of ENV_KEYS) safe[key] = values[key] ?? '';
  // Never hand the client secret to the page; it only needs to know it exists.
  const secretSet = !!safe.TWITCH_CLIENT_SECRET;
  delete safe.TWITCH_CLIENT_SECRET;
  return { values: safe, secretSet, envPath: path.join(HOME, '.env') };
}

async function getLive() {
  const files = await liveFiles();
  const t = readJson(files.tunables);
  const g = readJson(files.toggles);
  const { bounds, defaults } = tunableSpec();
  const tunables = {};
  for (const key of Object.keys(bounds)) {
    const n = Number.parseInt(t[key], 10);
    tunables[key] = Number.isFinite(n) ? n : defaults[key];
  }
  return { tunables, bounds, radio_autoplay_enabled: g.radio_autoplay_enabled !== false };
}
async function saveLive(payload) {
  const files = await liveFiles();
  const current = readJson(files.tunables);
  for (const [key, [lo, hi]] of Object.entries(tunableSpec().bounds)) {
    const n = Number.parseInt(payload?.tunables?.[key], 10);
    if (Number.isFinite(n)) current[key] = Math.max(lo, Math.min(hi, n));
  }
  fs.mkdirSync(path.dirname(files.tunables), { recursive: true });
  atomicWrite(files.tunables, JSON.stringify(current, null, 2));
  if (typeof payload?.radio_autoplay_enabled === 'boolean') {
    const toggles = readJson(files.toggles);
    toggles.radio_autoplay_enabled = payload.radio_autoplay_enabled;
    atomicWrite(files.toggles, JSON.stringify(toggles, null, 2));
  }
  guiLog('INFO', 'Live limits saved.');
  return getLive();
}

// -------------------------------------------------------------- updates ---
let appUpdate = null;
let ytdlpBusy = false;

async function checkAppUpdateNow() {
  appUpdate = await checkForAppUpdate({ current: app.getVersion() });
  if (!appUpdate.ok) guiLog('WARNING', `App update check failed: ${appUpdate.error}`);
  else if (appUpdate.updateAvailable) guiLog('INFO', `Twitch Radio ${appUpdate.latest} is available (you have ${app.getVersion()}). See Settings > App.`);
  else guiLog('INFO', `Twitch Radio is up to date (${app.getVersion()}).`);
  send('update:app', appUpdate);
  return appUpdate;
}

/** Runs a yt-dlp update mode of the core; every attempt lands in the Logs tab. */
async function runYtdlpMode(flag, timeout) {
  const result = await runCore([flag], undefined, timeout);
  const report = lastJsonLine(result.stdout);
  if (!report) {
    const detail = (result.stderr || String(result.error || '')).trim().slice(-300);
    guiLog('ERROR', `yt-dlp ${flag} gave no answer. ${detail}`);
    return { ok: false, error: "The bot core didn't answer. See the Logs tab." };
  }
  for (const line of report.log || []) pushLog({ level: line.level, logger: 'yt-dlp', msg: line.msg });
  delete report.log;
  return report;
}

async function ytdlpCheck() {
  if (ytdlpBusy) return { ok: false, error: 'Another yt-dlp operation is running.' };
  ytdlpBusy = true;
  try {
    const report = await runYtdlpMode('--ytdlp-check', 60000);
    send('update:ytdlp', report);
    return report;
  } finally {
    ytdlpBusy = false;
  }
}

/** Install or roll back: the bot is stopped around the swap and restarted if it was running. */
async function ytdlpChange(flag) {
  if (ytdlpBusy) return { ok: false, error: 'Another yt-dlp operation is running.' };
  ytdlpBusy = true;
  const wasRunning = !!core.child;
  try {
    if (wasRunning) {
      guiLog('INFO', 'Stopping the bot to change the yt-dlp version.');
      await stopBot();
    }
    const report = await runYtdlpMode(flag, 180000);
    if (wasRunning) startBot();
    return report;
  } finally {
    ytdlpBusy = false;
  }
}

function scheduleStartupChecks() {
  setTimeout(async () => {
    if (shouldAutoCheck(prefs)) {
      prefs.lastUpdateCheck = Date.now();
      savePrefs();
      await checkAppUpdateNow();
    }
    if (prefs.checkYtdlpOnStartup) {
      const report = await ytdlpCheck();
      if (report.ok && report.update_available) guiLog('INFO', `A newer yt-dlp (${report.latest}) is available. Settings > App can install it.`);
    }
  }, 8000);
}

// --------------------------------------------------------- window / tray ---
let win = null;
let tray = null;
let quitting = false;
// Launched at login: start with only the tray icon; the window is built when it is first opened.
const startHidden = process.argv.includes('--hidden');

function send(channel, payload) {
  if (win && !win.isDestroyed() && !win.webContents.isDestroyed()) win.webContents.send(channel, payload);
}

function createWindow() {
  win = new BrowserWindow({
    width: 1220,
    height: 780,
    minWidth: 980,
    minHeight: 640,
    show: false,
    backgroundColor: '#0b0b10',
    title: 'Twitch Radio',
    icon: path.join(__dirname, 'renderer', 'icon.png'),
    ...(IS_WIN
      ? { titleBarStyle: 'hidden', titleBarOverlay: { color: '#0b0b10', symbolColor: '#c9c9d9', height: 44 } }
      : {}),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
    },
  });
  win.setMenuBarVisibility(false);
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  win.once('ready-to-show', () => {
    win.show();
    updateWatching();
  });
  for (const event of ['show', 'hide', 'minimize', 'restore']) win.on(event, () => updateWatching());
  // The page is a fixed local file: never navigate away, never open windows.
  win.webContents.on('will-navigate', (event) => event.preventDefault());
  win.webContents.setWindowOpenHandler(({ url }) => {
    openExternalSafe(url);
    return { action: 'deny' };
  });
  win.on('close', onWindowClose);
  win.on('closed', () => {
    win = null;
    updateWatching();
  });
}

async function onWindowClose(event) {
  if (quitting) return;
  const running = core.child && core.phase !== 'stopped';
  if (!running) {
    quitting = true; // nothing to protect: closing the window quits the app
    return;
  }
  event.preventDefault();
  let action = prefs.closeAction;
  if (prefs.askOnClose) {
    const { response, checkboxChecked } = await dialog.showMessageBox(win, {
      type: 'question',
      title: 'Twitch Radio is still running',
      message: 'The radio is live. What would you like to do?',
      detail: 'Keeping it in the tray leaves the music and chat bot running. Stopping shuts everything down cleanly.',
      buttons: ['Keep running in tray', 'Stop and quit', 'Cancel'],
      defaultId: 0,
      cancelId: 2,
      noLink: true,
      checkboxLabel: "Don't ask again",
    });
    if (response === 2) return;
    action = response === 0 ? 'tray' : 'quit';
    if (checkboxChecked) {
      prefs.askOnClose = false;
      prefs.closeAction = action;
      savePrefs();
    }
  }
  if (action === 'quit') {
    app.quit();
  } else if (win) {
    // Not win.hide(): a hidden window keeps its renderer and GPU processes
    // alive (about 160 MB) for nothing. Closing it for real frees them; the
    // page rebuilds its whole state from the main process when reopened.
    win.destroy();
  }
}

function showWindow() {
  if (!win) createWindow();
  else {
    if (win.isMinimized()) win.restore();
    win.show();
    win.focus();
  }
}

function createTray() {
  const image = nativeImage.createFromPath(path.join(__dirname, 'renderer', 'tray.png'));
  tray = new Tray(image);
  tray.on('click', showWindow);
  refreshTray();
}
function refreshTray() {
  if (!tray) return;
  const running = core.phase === 'running' || core.phase === 'starting';
  const labels = { stopped: 'Stopped', starting: 'Starting...', running: 'Running', stopping: 'Stopping...', error: 'Error' };
  tray.setToolTip(`Twitch Radio - ${labels[core.phase] || core.phase}`);
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: 'Open Twitch Radio', click: showWindow },
      { type: 'separator' },
      { label: 'Start', enabled: !core.child, click: () => startBot() },
      { label: 'Stop', enabled: running, click: () => stopBot() },
      { type: 'separator' },
      { label: 'Quit', click: () => app.quit() },
    ]),
  );
}

function openExternalSafe(url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol === 'https:' || parsed.protocol === 'http:') {
      shell.openExternal(parsed.toString());
      return true;
    }
  } catch {
    /* fall through */
  }
  return false;
}

// ------------------------------------------------------------------ ipc ---
function registerIpc() {
  ipcMain.handle('bot:status', () => ({ status: status(), state: core.lastState }));
  ipcMain.handle('bot:start', () => startBot());
  ipcMain.handle('bot:stop', async () => {
    await stopBot();
    return status();
  });
  ipcMain.handle('bot:restart', () => restartBot());
  ipcMain.handle('bot:command', (_e, name) => {
    if (!['skip', 'pause', 'resume', 'clear_queue'].includes(name)) return { ok: false, error: 'Unknown command.' };
    return sendCommand(name);
  });

  ipcMain.handle('logs:get', () => logBuffer);
  ipcMain.handle('logs:clear', () => {
    logBuffer.length = 0;
    pendingLogs = [];
    return true;
  });
  ipcMain.handle('logs:export', async () => {
    const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    const { canceled, filePath } = await dialog.showSaveDialog(win, {
      title: 'Export logs',
      defaultPath: path.join(app.getPath('documents'), `twitch-radio-${stamp}.log`),
      filters: [{ name: 'Log file', extensions: ['log', 'txt'] }],
    });
    if (canceled || !filePath) return { ok: false, canceled: true };
    const text = logBuffer
      .map((e) => `${new Date(e.ts * 1000).toISOString()} [${e.level}] ${e.logger}: ${e.msg}`)
      .join('\n');
    fs.writeFileSync(filePath, text + '\n', 'utf8');
    return { ok: true, filePath };
  });

  ipcMain.handle('config:preflight', (_e, force) => preflight(force !== false));
  ipcMain.handle('config:getEnv', () => getEnv());
  ipcMain.handle('config:saveEnv', (_e, payload) => saveEnv(payload));
  ipcMain.handle('config:getLive', () => getLive());
  ipcMain.handle('config:saveLive', (_e, payload) => saveLive(payload));

  ipcMain.handle('prefs:get', () => prefs);
  ipcMain.handle('prefs:set', (_e, patch) => {
    for (const key of PAGE_PREFS) {
      if (patch && key in patch && typeof patch[key] === typeof DEFAULT_PREFS[key]) prefs[key] = patch[key];
    }
    savePrefs();
    if (app.isPackaged && (IS_WIN || process.platform === 'darwin')) app.setLoginItemSettings({ openAtLogin: !!prefs.launchAtLogin, args: ['--hidden'] });
    return prefs;
  });

  ipcMain.handle('update:app', async () => {
    prefs.lastUpdateCheck = Date.now();
    savePrefs();
    return checkAppUpdateNow();
  });
  ipcMain.handle('update:appLast', () => appUpdate);
  ipcMain.handle('update:ytdlpCheck', () => ytdlpCheck());
  ipcMain.handle('update:ytdlpInstall', () => ytdlpChange('--ytdlp-install'));
  ipcMain.handle('update:ytdlpRollback', () => ytdlpChange('--ytdlp-rollback'));

  ipcMain.handle('shell:openExternal', (_e, url) => openExternalSafe(String(url)));
  ipcMain.handle('shell:openPath', async (_e, which) => {
    const targets = {
      home: HOME,
      data: path.join(HOME, 'data'),
      logs: path.join(HOME, 'logs'),
      env: path.join(HOME, '.env'),
    };
    const target = targets[which];
    if (!target) return false;
    if (which === 'env') {
      shell.showItemInFolder(target);
      return true;
    }
    fs.mkdirSync(target, { recursive: true });
    return (await shell.openPath(target)) === '';
  });
  // yt-dlp rewrites its cookie jar while it runs, so the file has to sit in a
  // folder the bot can write to. Copy the picked export into the data folder
  // and hand back the relative path the bot's config expects.
  ipcMain.handle('shell:importCookies', async () => {
    const { canceled, filePaths } = await dialog.showOpenDialog(win, {
      title: 'Choose your exported cookies.txt',
      properties: ['openFile'],
      filters: [{ name: 'Cookies (Netscape format)', extensions: ['txt'] }, { name: 'All files', extensions: ['*'] }],
    });
    if (canceled || !filePaths.length) return null;
    fs.mkdirSync(path.join(HOME, 'data'), { recursive: true });
    fs.copyFileSync(filePaths[0], path.join(HOME, 'data', 'cookies.txt'));
    guiLog('INFO', 'Imported cookies.txt into the data folder.');
    return 'data/cookies.txt';
  });
  ipcMain.handle('app:info', () => ({ version: app.getVersion(), home: HOME, packaged: app.isPackaged, electron: process.versions.electron, platform: process.platform }));
}

// ------------------------------------------------------------ lifecycle ---
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', showWindow);

  app.whenReady().then(async () => {
    loadPrefs();
    registerIpc();
    if (!startHidden) createWindow();
    createTray();
    guiLog('INFO', `Twitch Radio ${app.getVersion()} - data folder: ${HOME}`);
    if (prefs.startBotOnLaunch) startBot();
    scheduleStartupChecks();
  });

  // Quitting must not leave ffmpeg / yt-dlp workers behind: stop the bot
  // first (it saves the queue and tokens), then really quit.
  app.on('before-quit', (event) => {
    if (core.child && !core.quitHandled) {
      event.preventDefault();
      core.quitHandled = true;
      quitting = true;
      stopBot().finally(() => app.quit());
    } else {
      quitting = true;
    }
  });
  // Windows logoff / shutdown gives no time for dialogs: closing the control
  // channel makes the core run the same graceful stop on its own.
  app.on('session-end', () => {
    quitting = true;
    try {
      core.child?.stdin.end();
    } catch {
      /* already gone */
    }
  });
  app.on('window-all-closed', () => {
    // Tray app: with the bot running the process stays; otherwise onWindowClose already set quitting.
    if (quitting) app.quit();
  });
}
