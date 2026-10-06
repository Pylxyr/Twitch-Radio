'use strict';
/* Twitch Radio - renderer. Plain JS, no build step. Talks to the main process
   only through window.api (see preload.js). */
(() => {
  const api = window.api;
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const OAUTH_BOT = 'http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot&force_verify=true';
  const OAUTH_OWNER = 'http://localhost:4343/oauth?scopes=channel:bot&force_verify=true';
  const OAUTH_REDIRECT = 'http://localhost:4343/oauth/callback';
  const LOG_CAP = 5000;
  const ROW = 22;

  let status = { phase: 'stopped' };
  let snap = null;
  let snapAt = 0;
  let pre = null;
  let appInfo = {};
  let prefs = {};

  // ------------------------------------------------------------ helpers ---
  function h(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === false || value == null) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? '' : value);
    }
    for (const child of children.flat()) if (child != null) node.append(child);
    return node;
  }
  const pad = (n, w = 2) => String(n).padStart(w, '0');
  function fmtDur(seconds) {
    seconds = Math.max(0, Math.floor(seconds || 0));
    const hh = Math.floor(seconds / 3600);
    const mm = Math.floor((seconds % 3600) / 60);
    const ss = seconds % 60;
    return hh ? `${hh}:${pad(mm)}:${pad(ss)}` : `${mm}:${pad(ss)}`;
  }
  function fmtUptime(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    const d = Math.floor(seconds / 86400);
    const hh = Math.floor((seconds % 86400) / 3600);
    const mm = Math.floor((seconds % 3600) / 60);
    if (d) return `${d}d ${hh}h`;
    if (hh) return `${hh}h ${pad(mm)}m`;
    return `${mm}m ${pad(seconds % 60)}s`;
  }
  function toast(message, kind = '') {
    const node = h('div', { class: `toast ${kind}`, text: message });
    $('#toasts').append(node);
    setTimeout(() => node.remove(), kind === 'err' ? 7000 : 3500);
  }
  async function copy(text, what = 'Copied') {
    try {
      await navigator.clipboard.writeText(text);
      toast(`${what} to clipboard.`, 'ok');
    } catch {
      toast("Couldn't access the clipboard.", 'err');
    }
  }
  const running = () => status.phase === 'running' || status.phase === 'starting';

  // --------------------------------------------------------------- tabs ---
  let currentTab = 'dashboard';
  function switchTab(name) {
    currentTab = name;
    $$('.nav').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
    $$('.tab').forEach((t) => t.classList.toggle('active', t.id === `tab-${name}`));
    if (name === 'logs') {
      unseenErrors = 0;
      renderBadge();
      renderLogs(true);
    }
    if (name === 'settings') loadSettings();
    if (name === 'dashboard') renderDashboard();
  }
  $$('.nav').forEach((b) => b.addEventListener('click', () => switchTab(b.dataset.tab)));
  document.addEventListener('click', (event) => {
    const goto = event.target.closest('[data-goto]');
    if (goto) switchTab(goto.dataset.goto);
    const cp = event.target.closest('[data-copy]');
    if (cp) copy($('#' + cp.dataset.copy).textContent, 'Copied');
  });

  // --------------------------------------------------- status & controls ---
  const PHASE_LABEL = { stopped: 'Stopped', starting: 'Starting…', running: 'Running', stopping: 'Stopping…', error: 'Error' };
  function renderStatus() {
    const phase = status.phase;
    let label = PHASE_LABEL[phase] || phase;
    if (status.restartAt) label = `Restarting in ${Math.max(0, Math.ceil((status.restartAt - Date.now()) / 1000))}s`;
    $('#statusPill').className = `pill ${phase}`;
    $('#statusText').textContent = label;
    $('#sideStatus').textContent = label;
    const live = phase === 'starting' || phase === 'running';
    $('#btnStart').hidden = live || phase === 'stopping';
    $('#btnStop').hidden = !(live || phase === 'stopping');
    $('#btnStop').disabled = phase === 'stopping';
    $('#btnStop').textContent = phase === 'stopping' ? 'Stopping…' : 'Stop';
    $('#btnRestart').hidden = !live;
    $('#btnStart').textContent = phase === 'error' ? 'Try again' : 'Start';

    const fatal = status.fatal;
    $('#errorBanner').hidden = !(phase === 'error' && fatal);
    if (fatal) {
      $('#errTitle').textContent = fatal.message || 'The bot stopped unexpectedly.';
      $('#errHint').textContent = fatal.hint || '';
    }
    const controls = live;
    ['#btnSkip', '#btnPause', '#btnClear'].forEach((id) => ($(id).disabled = !controls));
  }
  $('#btnStart').addEventListener('click', async () => {
    status = await api.start();
    renderStatus();
  });
  $('#btnStop').addEventListener('click', async () => {
    await api.stop();
  });
  $('#btnRestart').addEventListener('click', async () => {
    await api.restart();
  });
  $('#btnRestartNow').addEventListener('click', async () => {
    $('#settingsRestart').hidden = true;
    await api.restart();
  });
  async function command(name, okMessage) {
    const result = await api.command(name);
    if (!result.ok) toast(result.error || (name === 'skip' ? 'Nothing to skip.' : 'That did nothing.'), 'err');
    else if (okMessage) toast(okMessage, 'ok');
  }
  $('#btnSkip').addEventListener('click', () => command('skip', 'Skipping…'));
  $('#btnPause').addEventListener('click', () => command(snap && snap.player.paused ? 'resume' : 'pause'));
  $('#btnClear').addEventListener('click', () => {
    if (confirm('Remove every request from the queue?')) command('clear_queue', 'Queue cleared.');
  });
  $('#btnOpenWeb').addEventListener('click', () => api.openExternal($('#urlSettings').textContent));

  // ----------------------------------------------------------- dashboard ---
  function tokensNow() {
    const fromSnap = snap && snap.chat && snap.chat.tokens && Object.keys(snap.chat.tokens).length ? snap.chat.tokens : null;
    return fromSnap || (pre && pre.tokens) || {};
  }
  const solverReady = () => !!(pre && pre.js_solver && pre.js_solver.ready);
  function healthItems() {
    const live = running();
    const s = snap;
    const tok = tokensNow();
    const items = [];
    const add = (name, state, value) => items.push({ name, state, value });

    if (!live) add('Twitch chat', 'idle', 'Offline');
    else if (s && s.chat.ready && s.chat.subscribed) add('Twitch chat', 'ok', 'Connected');
    else if (s && s.chat.ready) add('Twitch chat', 'warn', 'Waiting for sign-in');
    else add('Twitch chat', 'warn', 'Connecting…');

    const signIn = (name, ok) => {
      if (tok.readable === false) add(name, 'err', 'Token file unreadable');
      else add(name, ok ? 'ok' : pre && pre.config_ok ? 'warn' : 'idle', ok ? 'Authorized' : 'Not authorized');
    };
    signIn('Bot account sign-in', !!tok.bot);
    signIn('Broadcaster sign-in', !!tok.owner);

    if (!live) add('Audio encoder', 'idle', 'Idle');
    else if (s && s.player.encoder_running) add('Audio encoder', s.player.encoder_starts > 1 ? 'warn' : 'ok', s.player.encoder_starts > 1 ? `Restarted ${s.player.encoder_starts - 1}×` : 'Running');
    else add('Audio encoder', 'idle', 'Starts with the first song');

    if (!live) add('Web server', 'idle', 'Offline');
    else if (s && s.http.up) add('Web server', 'ok', `${new URL(s.http.base).host} (this PC only)`);
    else add('Web server', 'warn', 'Starting…');

    const w = s && s.workers;
    if (!live || !w) add('yt-dlp workers', 'idle', 'Offline');
    else add('yt-dlp workers', 'ok', w.alive ? `${w.alive} running (up to ${w.size})` : 'Start on demand');

    if (!pre) add('ffmpeg', 'idle', 'Checking…');
    else add('ffmpeg', pre.ffmpeg && pre.ffmpeg.path ? 'ok' : 'err', pre.ffmpeg && pre.ffmpeg.path ? 'Found' : 'Not found');
    if (!pre) add('JS runtime', 'idle', 'Checking…');
    else {
      const js = pre.js_runtime || {};
      add(js.name ? `JS runtime (${js.name})` : 'JS runtime', js.path ? 'ok' : 'warn', js.path ? 'Found' : 'Not found');
    }
    if (pre) add('YouTube JS solver', solverReady() ? 'ok' : 'idle', solverReady() ? 'Ready' : 'Not needed so far');
    if (live && s) {
      const lag = (s.health && s.health.loop_lag_ms) || 0;
      add('Event-loop delay', lag < 60 ? 'ok' : lag < 250 ? 'warn' : 'err', `${Math.round(lag)} ms`);
    }
    return items;
  }

  function setupSteps() {
    const tok = tokensNow();
    const cfg = !!(pre && pre.config_ok);
    const live = running();
    const authBtns = (url) => [
      h('button', { class: 'btn primary sm', disabled: !live, title: live ? '' : 'Start the bot first', onclick: () => api.openExternal(url), text: 'Authorize' }),
      h('button', { class: 'btn ghost sm', onclick: () => copy(url, 'Link copied'), text: 'Copy link' }),
    ];
    return [
      {
        done: cfg,
        title: 'Enter your Twitch app credentials',
        desc: cfg ? '' : 'Client ID, secret and the two account IDs. In your Twitch app, set the OAuth Redirect URL to ' + OAUTH_REDIRECT,
        actions: [
          h('button', { class: 'btn primary sm', onclick: () => switchTab('settings'), text: 'Open Settings' }),
          h('button', { class: 'btn ghost sm', onclick: () => copy(OAUTH_REDIRECT, 'Redirect URL copied'), text: 'Copy redirect URL' }),
        ],
      },
      {
        done: !!tok.bot,
        title: 'Authorize the bot account',
        desc: live ? 'Opens Twitch in your browser. Sign in as the BOT account.' : 'Press Start first - the sign-in page is served by the running bot.',
        actions: authBtns(OAUTH_BOT),
      },
      {
        done: !!tok.owner,
        title: 'Authorize your broadcaster account',
        desc: 'Do this in a private window, or after signing out of the bot account on twitch.tv.',
        actions: authBtns(OAUTH_OWNER),
      },
    ];
  }

  function renderSetup() {
    const card = $('#setupCard');
    if (!pre) {
      card.hidden = true;
      return;
    }
    const steps = setupSteps();
    const done = steps.filter((s) => s.done).length;
    card.hidden = done === steps.length;
    if (card.hidden) return;
    $('#setupProgress').textContent = `${done} of ${steps.length} done`;
    const list = $('#setupSteps');
    list.replaceChildren(
      ...steps.map((step) =>
        h('li', { class: step.done ? 'done' : '' },
          h('span', { class: 'check', text: step.done ? '✓' : '' }),
          h('div', { class: 'step-body' }, h('div', { class: 'step-title', text: step.title }), step.desc ? h('div', { class: 'step-desc', text: step.desc }) : null),
          step.done ? null : h('div', { class: 'row' }, step.actions),
        ),
      ),
    );
  }

  let lastThumb = '';
  function renderDashboard() {
    if (currentTab !== 'dashboard') return;
    const live = running();
    const s = snap;
    $('#kpiListeners').textContent = live && s ? String(s.player.listeners) : '–';
    $('#kpiQueue').textContent = live && s ? String(s.queue_size) : '–';
    $('#kpiQueueSub').textContent = live && s ? (s.player.paused ? 'Playback paused' : s.radio_autoplay === false ? 'Auto-radio is off' : s.radio_autoplay ? 'Auto-radio is on' : '\u00a0') : '\u00a0';
    $('#kpiPlayed').textContent = live && s ? String(s.counters.tracks_played) : '–';
    $('#kpiPlayedSub').textContent = live && s ? `${s.counters.skips} skipped · ${s.counters.tracks_failed} failed` : '\u00a0';
    $('#kpiUptimeSub').textContent = live && status.startedAt ? `Since ${new Date(status.startedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}` : 'Not running';

    // now playing
    const np = live && s ? s.now : null;
    $('#nowTitle').textContent = np ? np.title : live ? 'Waiting for a request…' : 'Nothing playing';
    $('#nowSub').textContent = np ? np.uploader || '' : live ? 'Type !sr <song or link> in your chat.' : 'Start the bot, then use !sr in chat.';
    const tags = $('#nowTags');
    tags.replaceChildren();
    if (np) {
      tags.append(h('span', { class: `tag ${np.radio ? '' : 'purple'}`, text: np.radio ? 'Auto radio' : `Requested by ${np.requester}` }));
      if (s.player.paused) tags.append(h('span', { class: 'tag warn', text: 'Paused' }));
    }
    const thumb = np && np.thumbnail ? np.thumbnail : '';
    if (thumb !== lastThumb) {
      lastThumb = thumb;
      const img = $('#nowImg');
      if (thumb) {
        img.src = thumb;
        img.hidden = false;
        img.onerror = () => (img.hidden = true);
      } else {
        img.hidden = true;
        img.removeAttribute('src');
      }
    }
    $('#btnPause').textContent = s && s.player.paused ? 'Resume' : 'Pause';
    $('#btnSkip').disabled = !np;
    $('#btnClear').disabled = !(live && s && s.queue_size > 0);
    $('#btnPause').disabled = !live;
    tickProgress();

    // queue
    const list = $('#queueList');
    const items = live && s ? s.queue : [];
    list.replaceChildren(
      ...items.map((item) => h('li', {}, h('span', { class: 'q-title', text: item.title }), h('span', { class: 'q-by', text: item.radio ? 'auto radio' : item.requester }))),
    );
    if (live && s && s.queue_size > items.length) list.append(h('li', {}, h('span', { class: 'q-title muted', text: `…and ${s.queue_size - items.length} more` })));
    $('#queueEmpty').hidden = items.length > 0;

    // health
    $('#healthList').replaceChildren(
      ...healthItems().map((i) => h('li', { class: i.state }, h('span', { class: 'hdot' }), h('span', { class: 'hname', text: i.name }), h('span', { class: 'hval', text: i.value }))),
    );

    // counters
    const c = live && s ? s.counters : null;
    const tiles = [
      ['Requests queued', c && c.requests_queued],
      ['Songs played', c && c.tracks_played],
      ['Skipped', c && c.skips],
      ['Failed to load', c && c.tracks_failed],
      ['Links resolved', c && c.resolve_success],
      ['Resolve failures', c && c.resolve_failure],
    ];
    $('#counters').replaceChildren(...tiles.map(([label, value]) => h('div', { class: 'counter' }, h('b', { text: value == null ? '–' : String(value) }), h('span', { text: label }))));

    // OBS urls
    if (s && s.http) {
      $('#urlStream').textContent = s.http.stream;
      $('#urlOverlay').textContent = s.http.overlay;
      $('#urlSettings').textContent = s.http.settings;
    }

    renderSetup();
  }

  function tickProgress() {
    const live = running();
    const np = live && snap ? snap.now : null;
    if (live && status.startedAt && snap) {
      const base = snap.uptime + (Date.now() - snapAt) / 1000;
      $('#kpiUptime').textContent = fmtUptime(base);
    } else $('#kpiUptime').textContent = '–';
    if (!np) {
      $('#nowBar').style.width = '0%';
      $('#nowElapsed').textContent = '0:00';
      $('#nowTotal').textContent = '0:00';
      return;
    }
    let elapsed = np.elapsed + (snap.player.paused ? 0 : (Date.now() - snapAt) / 1000);
    if (np.duration > 0) elapsed = Math.min(elapsed, np.duration);
    $('#nowElapsed').textContent = fmtDur(elapsed);
    $('#nowTotal').textContent = np.duration > 0 ? fmtDur(np.duration) : 'live';
    $('#nowBar').style.width = np.duration > 0 ? `${Math.min(100, (elapsed / np.duration) * 100).toFixed(1)}%` : '0%';
  }
  setInterval(() => {
    if (document.hidden) return;
    if (currentTab === 'dashboard') tickProgress();
    if (status.restartAt) renderStatus();
  }, 1000);

  // ---------------------------------------------------------------- logs ---
  const logs = { all: [], view: [], levels: new Set(['DEBUG', 'INFO', 'WARNING', 'ERROR']), query: '', follow: true };
  let unseenErrors = 0;
  let logRaf = 0;
  const levelKey = (level) => (level === 'CRITICAL' ? 'ERROR' : level);
  function renderBadge() {
    const badge = $('#logBadge');
    badge.hidden = unseenErrors === 0 || currentTab === 'logs';
    badge.textContent = unseenErrors > 99 ? '99+' : String(unseenErrors);
  }
  function addLogs(batch) {
    for (const entry of batch) {
      logs.all.push(entry);
      if (!entry.cont && levelKey(entry.level) === 'ERROR' && currentTab !== 'logs') unseenErrors++;
    }
    if (logs.all.length > LOG_CAP) logs.all.splice(0, logs.all.length - LOG_CAP);
    renderBadge();
    if (currentTab === 'logs') scheduleLogs();
  }
  function scheduleLogs() {
    if (logRaf) return;
    logRaf = requestAnimationFrame(() => {
      logRaf = 0;
      renderLogs(true);
    });
  }
  function filterLogs() {
    const q = logs.query.trim().toLowerCase();
    logs.view = logs.all.filter((e) => logs.levels.has(levelKey(e.level)) && (!q || e.msg.toLowerCase().includes(q) || e.logger.toLowerCase().includes(q)));
  }
  function stamp(ts) {
    const d = new Date(ts * 1000);
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
  }
  function highlight(text, query) {
    if (!query) return [text];
    const out = [];
    const lower = text.toLowerCase();
    const q = query.toLowerCase();
    let from = 0;
    for (let at = lower.indexOf(q); at !== -1; at = lower.indexOf(q, from)) {
      out.push(text.slice(from, at), h('mark', { text: text.slice(at, at + q.length) }));
      from = at + q.length;
    }
    out.push(text.slice(from));
    return out;
  }
  function renderLogs(refilter) {
    if (refilter) filterLogs();
    const viewport = $('#logViewport');
    const total = logs.view.length;
    $('#logSizer').style.height = `${total * ROW}px`;
    $('#logEmpty').hidden = total > 0;
    $('#logCount').textContent = `${total.toLocaleString()} of ${logs.all.length.toLocaleString()} lines`;
    if (logs.follow && refilter) viewport.scrollTop = viewport.scrollHeight;
    paintRows();
    $('#btnJump').hidden = logs.follow;
    $('#btnFollow').classList.toggle('on', logs.follow);
  }
  function paintRows() {
    const viewport = $('#logViewport');
    const first = Math.max(0, Math.floor(viewport.scrollTop / ROW) - 6);
    const count = Math.ceil(viewport.clientHeight / ROW) + 12;
    const slice = logs.view.slice(first, first + count);
    const query = logs.query.trim();
    const rows = $('#logRows');
    rows.style.transform = `translateY(${first * ROW}px)`;
    rows.replaceChildren(
      ...slice.map((e) =>
        h('div', { class: `ll ${levelKey(e.level)}${e.cont ? ' cont' : ''}` },
          h('span', { class: 't', text: stamp(e.ts) }),
          h('span', { class: 'lv', text: levelKey(e.level) === 'WARNING' ? 'WARN' : levelKey(e.level) }),
          h('span', { class: 'lg', text: (e.logger || '').split('.').pop() }),
          h('span', { class: 'm' }, highlight(e.msg, query)),
        ),
      ),
    );
  }
  $('#logViewport').addEventListener('scroll', () => {
    const v = $('#logViewport');
    logs.follow = v.scrollHeight - v.scrollTop - v.clientHeight < 30;
    $('#btnJump').hidden = logs.follow;
    $('#btnFollow').classList.toggle('on', logs.follow);
    paintRows();
  });
  $('#btnFollow').addEventListener('click', () => {
    logs.follow = !logs.follow;
    renderLogs(true);
  });
  $('#btnJump').addEventListener('click', () => {
    logs.follow = true;
    renderLogs(true);
  });
  $('#levelChips').addEventListener('click', (event) => {
    const chip = event.target.closest('.chip');
    if (!chip) return;
    const level = chip.dataset.level;
    if (logs.levels.has(level)) logs.levels.delete(level);
    else logs.levels.add(level);
    chip.classList.toggle('on', logs.levels.has(level));
    renderLogs(true);
  });
  let searchTimer = 0;
  $('#logSearch').addEventListener('input', (event) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      logs.query = event.target.value;
      logs.follow = false;
      renderLogs(true);
    }, 120);
  });
  $('#btnLogClear').addEventListener('click', async () => {
    await api.clearLogs();
    logs.all = [];
    unseenErrors = 0;
    renderBadge();
    renderLogs(true);
  });
  $('#btnLogCopy').addEventListener('click', () => {
    if (!logs.view.length) return toast('Nothing to copy.', 'err');
    copy(logs.view.map((e) => `${stamp(e.ts)} [${levelKey(e.level)}] ${e.logger}: ${e.msg}`).join('\n'), `${logs.view.length} lines copied`);
  });
  $('#btnLogExport').addEventListener('click', async () => {
    const result = await api.exportLogs();
    if (result.ok) toast('Logs exported.', 'ok');
  });
  $('#btnLogFolder').addEventListener('click', () => api.openPath('logs'));
  window.addEventListener('resize', () => {
    if (currentTab === 'logs') paintRows();
    else renderDashboard();
  });

  // ------------------------------------------------------------ settings ---
  const SECTIONS = [
    {
      title: 'Twitch',
      desc: 'Create a free app at the Twitch developer console, then paste its credentials. Account IDs are the numeric user IDs, not the names.',
      links: [
        { label: 'Open Twitch developer console', url: 'https://dev.twitch.tv/console/apps' },
        { label: 'Copy OAuth redirect URL', copy: OAUTH_REDIRECT },
      ],
      fields: [
        { key: 'TWITCH_CLIENT_ID', label: 'Client ID', type: 'text' },
        { key: 'TWITCH_CLIENT_SECRET', label: 'Client secret', type: 'secret' },
        { key: 'TWITCH_BOT_ID', label: 'Bot account ID', type: 'text', pattern: /^\d+$/, hint: 'The account that posts in chat.' },
        { key: 'TWITCH_OWNER_ID', label: 'Broadcaster ID', type: 'text', pattern: /^\d+$/, hint: 'Your channel.' },
      ],
    },
    {
      title: 'Audio & playback',
      fields: [
        { key: 'AUDIO_BITRATE_KBPS', label: 'Audio bitrate (kbps)', type: 'number', min: 64, max: 256, placeholder: '160', hint: 'Opus quality of the stream sent to OBS.' },
        { key: 'PAUSE_QUEUE_WHEN_NO_LISTENERS', label: 'Pause when nobody is listening', type: 'bool', def: false, hint: 'Holds the queue while OBS has no audio source connected.' },
      ],
    },
    {
      title: 'Network',
      desc: 'The audio stream, OBS overlay and web settings page are served on this computer only (127.0.0.1); nothing is reachable from your network.',
      fields: [
        { key: 'TWITCH_NOWPLAYING_PORT', label: 'Port', type: 'number', min: 1024, max: 65535, placeholder: '8098' },
      ],
    },
    {
      title: 'YouTube (yt-dlp)',
      fields: [
        { special: 'cookies' },
        { key: 'YTDLP_CONCURRENCY', label: 'Parallel lookups', type: 'number', min: 1, max: 8, placeholder: '2', hint: 'The most lookups that may run at once. Each runs in its own process (roughly 50-100 MB) that is started when needed and exits when idle.' },
        { key: 'YTDLP_WORKER_IDLE_SECONDS', label: 'Lookup process idle exit (s)', type: 'number', min: 15, max: 3600, placeholder: '120', hint: 'How long an unused lookup process stays loaded before it frees its memory.' },
        { key: 'YTDLP_EXTRACT_TIMEOUT_SECONDS', label: 'Lookup timeout (s)', type: 'number', min: 10, max: 120, placeholder: '45' },
        { key: 'YTDLP_CACHE_TTL_SECONDS', label: 'Result cache (s)', type: 'number', min: 0, max: 3600, placeholder: '900', hint: '0 turns caching and next-song prefetching off.' },
      ],
    },
    {
      title: 'Logging',
      fields: [
        { key: 'LOG_LEVEL', label: 'Log level', type: 'select', options: ['DEBUG', 'INFO', 'WARNING', 'ERROR'], def: 'INFO' },
        { key: 'LOG_TO_FILE', label: 'Also write a log file', type: 'bool', def: true },
      ],
    },
  ];
  const form = { values: {}, orig: {}, secretSet: false, secretEditing: false, envPath: '' };
  const fieldDefs = SECTIONS.flatMap((s) => s.fields).filter((f) => f.key);
  const storeValue = (f, raw) => (f.type === 'bool' ? (raw === '' ? String(f.def) : String(/^(1|true|yes|on)$/i.test(raw))) : f.type === 'select' ? raw.toUpperCase() || f.def : raw);

  function isDirty() {
    return fieldDefs.some((f) => form.values[f.key] !== form.orig[f.key]);
  }
  function refreshSaveBar() {
    $('#saveBar').hidden = !isDirty();
    $('#saveError').textContent = '';
  }
  function control(f) {
    const set = (v) => {
      form.values[f.key] = v;
      refreshSaveBar();
    };
    const value = form.values[f.key] ?? '';
    if (f.type === 'secret') return secretControl(f, value, set);
    if (f.type === 'bool') {
      const box = h('input', { type: 'checkbox', id: `f-${f.key}` });
      box.checked = value === 'true';
      box.addEventListener('change', () => set(String(box.checked)));
      return h('label', { class: 'switch' }, box, h('span'));
    }
    if (f.type === 'select') {
      const sel = h('select', { class: 'input', id: `f-${f.key}` }, f.options.map((o) => h('option', { value: o, text: o })));
      sel.value = value || f.def;
      sel.addEventListener('change', () => set(sel.value));
      return sel;
    }
    const input = h('input', { class: `input${f.type === 'number' ? ' num' : ''}`, id: `f-${f.key}`, type: f.type === 'number' ? 'number' : 'text', placeholder: f.placeholder || '', autocomplete: 'off', spellcheck: 'false', min: f.min, max: f.max });
    input.value = value;
    input.addEventListener('input', () => set(input.value.trim()));
    return input;
  }
  // The stored secret never reaches the page: it shows "set" and a new value is sent only when typed.
  function secretControl(f, value, set) {
    const input = h('input', { class: 'input', id: `f-${f.key}`, type: 'password', placeholder: form.secretSet ? 'Enter a new secret' : '', autocomplete: 'off', spellcheck: 'false' });
    input.value = value;
    input.addEventListener('input', () => set(input.value.trim()));
    const toggle = h('button', { class: 'btn ghost sm', type: 'button', text: 'Show', onclick: () => {
      input.type = input.type === 'password' ? 'text' : 'password';
      toggle.textContent = input.type === 'password' ? 'Show' : 'Hide';
    } });
    const holder = h('div', { class: 'secret' });
    const draw = () => {
      if (form.secretSet && !form.secretEditing) {
        holder.replaceChildren(
          h('span', { class: 'tag', text: 'set' }),
          h('button', { class: 'btn ghost sm', type: 'button', text: 'Replace', onclick: () => {
            form.secretEditing = true;
            draw();
            input.focus();
          } }),
        );
        return;
      }
      holder.replaceChildren(input, toggle, form.secretSet ? h('button', { class: 'btn ghost sm', type: 'button', text: 'Cancel', onclick: () => {
        form.secretEditing = false;
        input.value = '';
        set('');
        draw();
      } }) : null);
    };
    draw();
    return holder;
  }
  function fieldRow(f) {
    if (f.special === 'cookies') return cookiesRow();
    return h('div', { class: 'field', 'data-key': f.key },
      h('label', { class: 'name', for: `f-${f.key}`, text: f.label }),
      h('div', { class: 'ctl' }, control(f), f.hint ? h('div', { class: 'hint', text: f.hint }) : null, h('div', { class: 'bad-msg', hidden: true })),
    );
  }
  function cookiesRow() {
    const key = 'YTDLP_COOKIES_FILE';
    const label = h('span', { class: 'muted', text: form.values[key] || 'Not set' });
    const root = h('div', { class: 'field' },
      h('label', { class: 'name', text: 'YouTube cookies' }),
      h('div', { class: 'ctl' },
        h('div', { class: 'row' }, h('button', { class: 'btn ghost sm', type: 'button', text: 'Import cookies.txt…', onclick: async () => {
          const rel = await api.importCookies();
          if (rel) {
            form.values[key] = rel;
            label.textContent = rel;
            refreshSaveBar();
          }
        } }), h('button', { class: 'btn ghost sm', type: 'button', text: 'Clear', onclick: () => {
          form.values[key] = '';
          label.textContent = 'Not set';
          refreshSaveBar();
        } }), label),
        h('div', { class: 'hint', text: 'Only needed for age-restricted or members-only videos. Export it from a browser extension in Netscape format.' })),
    );
    return root;
  }
  function section(s) {
    const node = h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', { text: s.title })));
    if (s.desc) node.append(h('div', { class: 'sec-desc', text: s.desc }));
    if (s.links) node.append(h('div', { class: 'links', style: 'margin-bottom:10px' }, s.links.map((l) => h('button', { class: 'btn ghost sm', type: 'button', text: l.label, onclick: () => (l.url ? api.openExternal(l.url) : copy(l.copy, 'Copied')) }))));
    s.fields.forEach((f) => node.append(fieldRow(f)));
    return node;
  }

  const live = { data: null, values: {}, orig: {} };
  function liveCard() {
    const d = live.data;
    const card = h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', { text: 'Live limits' }), h('span', { class: 'muted small', text: 'Apply instantly - no restart' })));
    const defs = [
      ['max_pending_per_chatter', 'Max pending per viewer', 'How many songs one viewer can have queued.'],
      ['request_cooldown_seconds', 'Request cooldown (s)', '0 disables the cooldown.'],
      ['queue_cap', 'Queue cap', 'Total requests before !sr turns people away.'],
      ['max_request_duration_seconds', 'Max track length (s)', 'Longer songs are refused.'],
    ];
    defs.forEach(([key, label, hint]) => {
      const [lo, hi] = d.bounds[key];
      const input = h('input', { class: 'input num', type: 'number', min: lo, max: hi, id: `l-${key}` });
      input.value = live.values[key];
      input.addEventListener('input', () => (live.values[key] = input.value));
      card.append(h('div', { class: 'field' }, h('label', { class: 'name', for: `l-${key}`, text: label }), h('div', { class: 'ctl' }, input, h('div', { class: 'hint', text: `${hint} (${lo}–${hi})` }))));
    });
    const box = h('input', { type: 'checkbox', id: 'l-radio' });
    box.checked = live.values.radio;
    box.addEventListener('change', () => (live.values.radio = box.checked));
    card.append(h('div', { class: 'field' }, h('label', { class: 'name', for: 'l-radio', text: 'Auto-radio' }), h('div', { class: 'ctl' }, h('label', { class: 'switch' }, box, h('span')), h('div', { class: 'hint', text: 'Queue a similar song automatically when the queue runs dry.' }))));
    card.append(h('div', { class: 'row', style: 'justify-content:flex-end;margin-top:6px' }, h('button', { class: 'btn primary', type: 'button', text: 'Apply limits', onclick: async () => {
      try {
        live.data = await api.saveLive({ tunables: Object.fromEntries(Object.keys(d.bounds).map((k) => [k, live.values[k]])), radio_autoplay_enabled: live.values.radio });
        Object.assign(live.values, live.data.tunables, { radio: live.data.radio_autoplay_enabled });
        defs.forEach(([key]) => ($(`#l-${key}`).value = live.values[key]));
        toast(running() ? 'Applied. The running bot picks this up right away.' : 'Saved. Applies on next start.', 'ok');
      } catch (error) {
        toast(`Couldn't save: ${error.message}`, 'err');
      }
    } })));
    return card;
  }

  function appCard() {
    const toggles = [
      ['askOnClose', 'Ask before closing while the radio is running', 'Otherwise the window follows your last choice.'],
      ['autoRestart', 'Restart automatically if the bot crashes', 'Up to 5 times in 10 minutes. Not for settings errors.'],
      ['startBotOnLaunch', 'Start the bot when this app opens', ''],
      ['launchAtLogin', 'Open Twitch Radio when I sign in to Windows', 'Starts minimized to the tray.'],
    ].filter(([key]) => key !== 'launchAtLogin' || appInfo.platform !== 'linux'); // Electron can't set login items on Linux
    const card = h('div', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', { text: 'This app' })));
    toggles.forEach(([key, label, hint]) => {
      const box = h('input', { type: 'checkbox', id: `p-${key}` });
      box.checked = !!prefs[key];
      box.addEventListener('change', async () => {
        prefs = await api.setPrefs({ [key]: box.checked });
      });
      card.append(h('div', { class: 'field' }, h('label', { class: 'name', for: `p-${key}`, text: label }), h('div', { class: 'ctl' }, h('label', { class: 'switch' }, box, h('span')), hint ? h('div', { class: 'hint', text: hint }) : null)));
    });
    card.append(h('div', { class: 'field' }, h('label', { class: 'name', text: 'Files' }), h('div', { class: 'ctl' }, h('div', { class: 'links' },
      h('button', { class: 'btn ghost sm', type: 'button', text: 'Open data folder', onclick: () => api.openPath('data') }),
      h('button', { class: 'btn ghost sm', type: 'button', text: 'Open log folder', onclick: () => api.openPath('logs') }),
      h('button', { class: 'btn ghost sm', type: 'button', text: 'Show .env file', onclick: () => api.openPath('env') })),
      h('div', { class: 'hint', text: `Settings and data live in ${appInfo.home || ''}. Uninstalling keeps them.` }))));
    return card;
  }

  // ------------------------------------------------------------- updates ---
  const upd = { app: null, ytdlp: null, busy: '' };
  const ytdlpView = () => {
    const info = upd.ytdlp || {};
    const base = (pre && pre.ytdlp) || {};
    return { version: info.current || base.version, source: info.source || base.source, bundled: info.bundled || base.bundled, previous: info.previous !== undefined ? info.previous : base.previous, latest: info.latest || null, available: !!info.update_available, error: info.error || null, overrideError: info.override_error || base.override_error || null };
  };
  function updateRow(label, status, buttons, hint) {
    return h('div', { class: 'field' }, h('label', { class: 'name', text: label }),
      h('div', { class: 'ctl' }, h('div', { class: 'row' }, status, buttons), hint ? h('div', { class: 'hint', text: hint }) : null));
  }
  function prefToggle(key, label) {
    const box = h('input', { type: 'checkbox', id: `p-${key}` });
    box.checked = !!prefs[key];
    box.addEventListener('change', async () => {
      prefs = await api.setPrefs({ [key]: box.checked });
    });
    return h('div', { class: 'field' }, h('label', { class: 'name', for: `p-${key}`, text: label }), h('div', { class: 'ctl' }, h('label', { class: 'switch' }, box, h('span'))));
  }
  function fillUpdates(card) {
    const busy = !!upd.busy;
    const a = upd.app;
    const appStatus = !a ? `v${appInfo.version}` : !a.ok ? `v${appInfo.version} - ${a.error}` : a.updateAvailable ? `v${appInfo.version} - v${a.latest} is available` : `v${appInfo.version} - up to date`;
    const appButtons = [h('button', { class: 'btn ghost sm', type: 'button', disabled: busy, text: upd.busy === 'app' ? 'Checking…' : 'Check for app updates', onclick: checkApp })];
    if (a && a.ok && a.updateAvailable) appButtons.push(h('button', { class: 'btn primary sm', type: 'button', text: 'Download installer', onclick: () => api.openExternal(a.installerUrl || a.releaseUrl) }));
    const y = ytdlpView();
    const ytStatus = y.version ? `${y.version}${y.source === 'override' ? ' (updated)' : ' (bundled)'} - latest: ${y.latest || 'not checked'}${y.error ? ` (${y.error})` : ''}` : 'unknown';
    const ytButtons = [h('button', { class: 'btn ghost sm', type: 'button', disabled: busy, text: upd.busy === 'check' ? 'Checking…' : 'Check for updates', onclick: checkYtdlp })];
    ytButtons.push(h('button', { class: 'btn primary sm', type: 'button', disabled: busy || !y.available, text: upd.busy === 'install' ? 'Updating…' : 'Update', onclick: () => changeYtdlp('install') }));
    if (y.previous) ytButtons.push(h('button', { class: 'btn ghost sm', type: 'button', disabled: busy, text: `Roll back to ${y.previous}`, onclick: () => changeYtdlp('rollback') }));
    const solver = pre && pre.js_solver;
    card.replaceChildren(
      h('div', { class: 'card-head' }, h('h2', { text: 'Updates' })),
      updateRow('This app', h('span', { class: 'muted', text: appStatus }), appButtons, 'Only checks and links to the installer; nothing is installed for you.'),
      prefToggle('checkAppUpdates', 'Check for app updates automatically (once a day)'),
      updateRow('yt-dlp', h('span', { class: 'muted', text: ytStatus }), ytButtons, 'YouTube changes often; updating yt-dlp does not need a new installer. Updating restarts the bot if it is running.'),
      y.overrideError ? h('div', { class: 'hint err-text', text: `The downloaded yt-dlp failed to load, so the bundled one is in use (${y.overrideError}).` }) : null,
      prefToggle('checkYtdlpOnStartup', 'Check for yt-dlp updates on startup'),
      updateRow('JS solver', h('span', { class: solver && solver.ready ? 'tag purple' : 'tag', text: !solver ? 'unknown' : solver.ready ? 'ready' : 'not downloaded yet (optional)' }), [],
        solver && solver.ready ? 'Downloaded by yt-dlp from github.com/yt-dlp/ejs and kept in the data folder.' : 'Only needed if YouTube starts requiring it, for example on a datacenter IP. yt-dlp then downloads it from github.com on its own; if that is blocked, allow github.com through your firewall or antivirus.'),
    );
  }
  function refreshUpdates() {
    const card = $('#updatesCard');
    if (card) fillUpdates(card);
  }
  async function checkApp() {
    upd.busy = 'app';
    refreshUpdates();
    try {
      upd.app = await api.checkAppUpdate();
    } catch (error) {
      upd.app = { ok: false, error: error.message || String(error) };
    }
    upd.busy = '';
    refreshUpdates();
  }
  async function checkYtdlp() {
    upd.busy = 'check';
    refreshUpdates();
    try {
      upd.ytdlp = await api.ytdlpCheck();
    } catch (error) {
      upd.ytdlp = { ok: false, error: error.message || String(error) };
    }
    upd.busy = '';
    refreshUpdates();
  }
  async function changeYtdlp(kind) {
    upd.busy = kind;
    refreshUpdates();
    try {
      const result = kind === 'install' ? await api.ytdlpInstall() : await api.ytdlpRollback();
      if (result.ok) toast(kind === 'install' ? 'yt-dlp updated.' : 'Rolled back yt-dlp.', 'ok');
      else toast(result.error || "Couldn't change yt-dlp.", 'err');
    } catch (error) {
      toast(`Couldn't change yt-dlp: ${error.message || error}`, 'err');
    }
    upd.busy = '';
    await refreshPreflight();
    upd.ytdlp = await api.ytdlpCheck().catch(() => upd.ytdlp);
    refreshUpdates();
  }
  function updatesCard() {
    const card = h('div', { class: 'card', id: 'updatesCard' });
    fillUpdates(card);
    return card;
  }

  async function loadSettings() {
    try {
      const [env, liveData] = await Promise.all([api.getEnv(), api.getLive()]);
      const dirty = isDirty();
      if (!dirty) {
        form.secretSet = !!env.secretSet;
        form.secretEditing = false;
        form.envPath = env.envPath;
        for (const f of fieldDefs) form.values[f.key] = storeValue(f, env.values[f.key] || '');
        form.values.YTDLP_COOKIES_FILE = env.values.YTDLP_COOKIES_FILE || '';
        form.orig = { ...form.values };
      }
      live.data = liveData;
      Object.assign(live.values, liveData.tunables, { radio: liveData.radio_autoplay_enabled });
      $('#settingsForm').replaceChildren(...SECTIONS.map(section), liveCard(), appCard(), updatesCard());
      refreshSaveBar();
    } catch (error) {
      toast(`Couldn't load settings: ${error.message}`, 'err');
    }
  }
  function validate() {
    let ok = true;
    $$('.field.bad').forEach((n) => {
      n.classList.remove('bad');
      $('.bad-msg', n).hidden = true;
    });
    for (const f of fieldDefs) {
      const v = form.values[f.key] ?? '';
      let msg = '';
      if (f.type === 'number' && v !== '') {
        const n = Number(v);
        if (!Number.isInteger(n) || n < f.min || n > f.max) msg = `Enter a whole number from ${f.min} to ${f.max}.`;
      }
      if (f.pattern && v && !f.pattern.test(v)) msg = 'Digits only - this is the numeric Twitch user ID.';
      if (msg) {
        ok = false;
        const row = $(`.field[data-key="${f.key}"]`);
        if (row) {
          row.classList.add('bad');
          const m = $('.bad-msg', row);
          m.textContent = msg;
          m.hidden = false;
        }
      }
    }
    return ok;
  }
  let saving = false;
  function setSaving(on) {
    saving = on;
    $('#btnSave').disabled = on;
    $('#btnDiscard').disabled = on;
  }
  $('#btnDiscard').addEventListener('click', () => {
    if (saving) return;
    form.values = { ...form.orig };
    form.secretEditing = false;
    loadSettings();
  });
  $('#btnSave').addEventListener('click', async () => {
    if (saving) return;
    if (!validate()) {
      $('#saveError').textContent = 'Fix the highlighted fields first.';
      return;
    }
    const changed = {};
    for (const f of fieldDefs) if (form.values[f.key] !== form.orig[f.key]) changed[f.key] = form.values[f.key];
    setSaving(true);
    try {
      const result = await api.saveEnv({ values: changed });
      if (!result.ok) {
        $('#saveError').textContent = result.error || "Couldn't save.";
        return;
      }
      pre = result.preflight || pre;
      form.values.TWITCH_CLIENT_SECRET = '';
      form.orig = { ...form.values };
      toast('Settings saved.', 'ok');
      $('#settingsRestart').hidden = !result.needsRestart;
      await loadSettings();
      renderDashboard();
    } catch (error) {
      $('#saveError').textContent = `Couldn't save: ${error.message || error}`;
    } finally {
      setSaving(false);
    }
  });

  // ---------------------------------------------------------------- wiring ---
  async function refreshPreflight(force = true) {
    try {
      pre = await api.preflight(force);
    } catch {
      /* the checklist just stays in its "checking" state */
    }
    renderDashboard();
    refreshUpdates();
  }

  async function init() {
    [appInfo, prefs] = await Promise.all([api.appInfo(), api.getPrefs()]);
    $('#sideVersion').textContent = `v${appInfo.version}`;
    const current = await api.getStatus();
    status = current.status;
    snap = current.state;
    snapAt = Date.now();
    addLogs(await api.getLogs());
    unseenErrors = 0; // history from before the window opened isn't news
    renderBadge();
    renderStatus();
    renderDashboard();

    let lastPhase = status.phase;
    api.onStatus((next) => {
      status = next;
      if (next.phase === 'stopped' || next.phase === 'error') snap = null;
      renderStatus();
      renderDashboard();
      if (next.phase !== lastPhase && (next.phase === 'stopped' || next.phase === 'running' || next.phase === 'error')) refreshPreflight(true);
      lastPhase = next.phase;
    });
    api.onState((next) => {
      snap = next;
      snapAt = Date.now();
      renderDashboard();
    });
    api.onLogs(addLogs);
    // Nothing polls. The report is refreshed when something changed (start/stop, a save)
    // and when the window regains focus (say, after installing ffmpeg); the main process
    // answers from its cache when the last check is under two minutes old, so this costs
    // one short check per two minutes of active use and nothing while idle.
    refreshPreflight(false);
    window.addEventListener('focus', () => {
      if (!running()) refreshPreflight(false);
    });
    api.onAppUpdate((result) => {
      upd.app = result;
      refreshUpdates();
    });
    api.onYtdlpUpdate((result) => {
      upd.ytdlp = result;
      refreshUpdates();
    });
    api.lastAppUpdate().then((result) => {
      if (result) upd.app = result;
    });
  }
  init().catch((error) => toast(`Startup problem: ${error.message}`, 'err'));
})();
