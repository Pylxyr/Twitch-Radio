'use strict';
// The parts of the look that are not CSS: the window's own background and, on Windows, the colours of
// the caption buttons drawn over the title bar. They must match --bg in renderer/styles.css, which
// gui/test/theme.test.js checks. The page itself follows prefers-color-scheme, which Electron's
// nativeTheme.themeSource drives (see applyTheme in main.js).
const skins = require('./renderer/skins');

// The default style's colours; the other colour styles (renderer/skins.js) tint them.
const WINDOW_COLORS = {
  dark: skins.windowColors('default', 'dark'),
  light: skins.windowColors('default', 'light'),
};

/** Window colours for a colour style in 'dark' or 'light'. */
function windowColorsFor(skin, mode) {
  return skins.windowColors(skin, mode);
}

/** 'dark' or 'light' for a preference ('system' | 'light' | 'dark') given what the OS currently uses. */
function resolveTheme(preference, systemIsDark) {
  if (preference === 'light' || preference === 'dark') return preference;
  return systemIsDark ? 'dark' : 'light';
}

module.exports = { WINDOW_COLORS, windowColorsFor, resolveTheme };
