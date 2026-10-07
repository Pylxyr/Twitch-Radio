'use strict';
// The parts of the look that are not CSS: the window's own background and, on Windows, the colours of
// the caption buttons drawn over the title bar. They must match --bg in renderer/styles.css, which
// gui/test/theme.test.js checks. The page itself follows prefers-color-scheme, which Electron's
// nativeTheme.themeSource drives (see applyTheme in main.js).
const WINDOW_COLORS = {
  dark: { background: '#0b0b10', symbols: '#c9c9d9' },
  light: { background: '#f3f3f8', symbols: '#33334a' },
};

/** 'dark' or 'light' for a preference ('system' | 'light' | 'dark') given what the OS currently uses. */
function resolveTheme(preference, systemIsDark) {
  if (preference === 'light' || preference === 'dark') return preference;
  return systemIsDark ? 'dark' : 'light';
}

module.exports = { WINDOW_COLORS, resolveTheme };
