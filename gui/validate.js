'use strict';
// Input checks for what the page sends to the main process. The page is untrusted (see preload.js),
// so everything it passes is re-validated here, in plain functions that have their own tests.

// Same numbers as twitch_radio/lookahead.py (tests/test_radio_lookahead.py checks they agree).
const LOOKAHEAD_MIN = 1;
const LOOKAHEAD_MAX = 15;
const LOOKAHEAD_DEFAULT = 5;
const LOOKAHEAD_DEFAULT_ENABLED = true;

const MAX_QUERY_LENGTH = 300;
const THEMES = ['system', 'light', 'dark'];
// Same names as twitch_radio/config.py (MODES): the Twitch song-request bot, or a plain music player.
const MODES = ['twitch', 'player'];
const VOLUME_DEFAULT = 0.8;

/** A song search or link typed in the dashboard: one tidy line, or null when there is nothing usable. */
function cleanQuery(value) {
  if (typeof value !== 'string') return null;
  const query = value.replace(/\s+/g, ' ').trim();
  if (!query || query.length > MAX_QUERY_LENGTH) return null;
  return query;
}

/** The radio-lookahead setting from anything (a saved file, the page): always usable, always in range. */
function normalizeLookahead(value) {
  const raw = value && typeof value === 'object' ? value : {};
  const n = typeof raw.count === 'number' || typeof raw.count === 'string' ? Number.parseInt(raw.count, 10) : NaN;
  return {
    enabled: typeof raw.enabled === 'boolean' ? raw.enabled : LOOKAHEAD_DEFAULT_ENABLED,
    count: Number.isFinite(n) ? Math.max(LOOKAHEAD_MIN, Math.min(LOOKAHEAD_MAX, n)) : LOOKAHEAD_DEFAULT,
  };
}

const isTheme = (value) => THEMES.includes(value);
const isMode = (value) => MODES.includes(value);

/** The music player's volume from anything: a number from 0 to 1, the default when it is not one. */
function normalizeVolume(value) {
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : VOLUME_DEFAULT;
}

module.exports = {
  LOOKAHEAD_MIN,
  LOOKAHEAD_MAX,
  LOOKAHEAD_DEFAULT,
  LOOKAHEAD_DEFAULT_ENABLED,
  MAX_QUERY_LENGTH,
  THEMES,
  MODES,
  VOLUME_DEFAULT,
  isMode,
  normalizeVolume,
  cleanQuery,
  normalizeLookahead,
  isTheme,
};
