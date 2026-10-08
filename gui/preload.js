'use strict';
// The only bridge between the page and the main process. The page gets named
// functions, never ipcRenderer itself, so a compromised page can't reach
// arbitrary channels.
const { contextBridge, ipcRenderer } = require('electron');

const invoke = (channel) => (...args) => ipcRenderer.invoke(channel, ...args);
const subscribe = (channel) => (callback) => {
  const handler = (_event, payload) => callback(payload);
  ipcRenderer.on(channel, handler);
  return () => ipcRenderer.removeListener(channel, handler);
};

contextBridge.exposeInMainWorld('api', {
  // bot lifecycle
  getStatus: invoke('bot:status'),
  start: invoke('bot:start'),
  stop: invoke('bot:stop'),
  restart: invoke('bot:restart'),
  command: invoke('bot:command'),
  requestSong: invoke('bot:requestSong'),
  searchSongs: invoke('bot:searchSongs'),
  getLookahead: invoke('radio:getLookahead'),
  setLookahead: invoke('radio:setLookahead'),
  onStatus: subscribe('bot:status'),
  hideToTray: invoke('window:hideToTray'),
  getMode: invoke('mode:get'),
  setMode: invoke('mode:set'),
  onMode: subscribe('app:mode'),
  onState: subscribe('bot:state'),
  // logs
  getLogs: invoke('logs:get'),
  clearLogs: invoke('logs:clear'),
  exportLogs: invoke('logs:export'),
  onLogs: subscribe('logs:batch'),
  // configuration
  preflight: invoke('config:preflight'),
  getEnv: invoke('config:getEnv'),
  saveEnv: invoke('config:saveEnv'),
  getLive: invoke('config:getLive'),
  saveLive: invoke('config:saveLive'),
  getPrefs: invoke('prefs:get'),
  setPrefs: invoke('prefs:set'),
  // updates
  checkAppUpdate: invoke('update:app'),
  lastAppUpdate: invoke('update:appLast'),
  ytdlpCheck: invoke('update:ytdlpCheck'),
  ytdlpInstall: invoke('update:ytdlpInstall'),
  ytdlpRollback: invoke('update:ytdlpRollback'),
  onAppUpdate: subscribe('update:app'),
  onYtdlpUpdate: subscribe('update:ytdlp'),
  // shell helpers
  openExternal: invoke('shell:openExternal'),
  openPath: invoke('shell:openPath'),
  importCookies: invoke('shell:importCookies'),
  appInfo: invoke('app:info'),
});
