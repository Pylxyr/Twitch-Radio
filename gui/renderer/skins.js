'use strict';
// Colour styles ("skins") for the dashboard, like the skins of a desktop music player. A style sets
// the accent and tints the surfaces; light and dark mode still apply on top of it. The base palettes
// live in styles.css; a style only overrides the variables listed in skinVariables() and is applied
// by app.js as inline custom properties on <html>, so no colour is duplicated in the stylesheet.
//
// This one file is loaded by the page (<script src="skins.js">, as the global `Skins`) and by the
// main process (require), which needs the same background colours for the window itself.
(function (root, factory) {
  const skins = factory();
  if (typeof module === 'object' && module.exports) module.exports = skins;
  else root.Skins = skins;
})(typeof self !== 'undefined' ? self : this, function () {
  const SYMBOLS = { dark: '#c9c9d9', light: '#33334a' }; // Windows caption buttons
  const DEFAULT_SURFACES = {
    dark: { bg: '#0b0b10', panel: '#13131b', accent: '#9146ff' },
    light: { bg: '#f3f3f8', panel: '#ffffff', accent: '#8a3ffc' },
  };

  // h/s: tint of the surfaces (hue 0-360, saturation 0-1). dark/light: the accent, a lighter or darker
  // variant for hover and text on tinted backgrounds. `light: null` means "use the default light look".
  const SKINS = {
    default: { label: 'Twitch purple' },
    midnight: {
      label: 'Midnight', h: 222, s: 0.3,
      dark: { accent: '#4f8cff', hi: '#7aa8ff', text: '#c6d8ff' },
      light: { accent: '#2f6fed', hi: '#1f56c9', text: '#1e40af' },
    },
    ocean: {
      label: 'Ocean', h: 190, s: 0.28,
      dark: { accent: '#0f9fae', hi: '#3fd2de', text: '#b4f1f6' },
      light: { accent: '#0e8f9b', hi: '#0b7280', text: '#0b5560' },
    },
    forest: {
      label: 'Forest', h: 150, s: 0.22,
      dark: { accent: '#2a9d5f', hi: '#52c987', text: '#bdf0d3' },
      light: { accent: '#1f8a52', hi: '#176f41', text: '#14532d' },
    },
    sunset: {
      label: 'Sunset', h: 20, s: 0.25,
      dark: { accent: '#d95a14', hi: '#ff8a4c', text: '#ffd2b8' },
      light: { accent: '#c74f0c', hi: '#a63f08', text: '#8a3408' },
    },
    rose: {
      label: 'Rose', h: 335, s: 0.25,
      dark: { accent: '#d63d73', hi: '#f06b99', text: '#fbc2d6' },
      light: { accent: '#c92b64', hi: '#ad2255', text: '#8a1a44' },
    },
    graphite: {
      label: 'Graphite', h: 220, s: 0.05,
      dark: { accent: '#6b7a90', hi: '#8795ab', text: '#d5dbe6' },
      light: { accent: '#55657c', hi: '#435166', text: '#2f3a4b' },
    },
    amoled: {
      label: 'AMOLED black', h: 260, s: 0.06, black: true,
      dark: { accent: '#9146ff', hi: '#ae7bff', text: '#d9c4ff' },
      light: null,
    },
  };
  const SKIN_NAMES = Object.keys(SKINS);
  const isSkin = (name) => typeof name === 'string' && Object.prototype.hasOwnProperty.call(SKINS, name);

  function hslToRgb(h, s, l) {
    const k = (n) => (n + h / 30) % 12;
    const a = s * Math.min(l, 1 - l);
    const f = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
    return [f(0), f(8), f(4)].map((v) => Math.round(v * 255));
  }
  const toHex = (rgb) => `#${rgb.map((v) => v.toString(16).padStart(2, '0')).join('')}`;
  const surface = (h, s, lightness) => toHex(hslToRgb(h, s, lightness / 100));
  const channels = (hex) => [1, 3, 5].map((i) => Number.parseInt(hex.slice(i, i + 2), 16)).join(',');
  const rgba = (hex, alpha) => `rgba(${channels(hex)},${alpha})`;

  /** The custom properties a style overrides for 'dark' or 'light' ({} when the base palette applies). */
  function skinVariables(name, mode) {
    const skin = SKINS[isSkin(name) ? name : 'default'];
    const accent = skin && skin[mode];
    if (!accent || skin.h === undefined) return {};
    const dark = mode === 'dark';
    const { h } = skin;
    const s = dark ? Math.min(0.6, skin.s * 1.4) : skin.s * 0.8;
    // lightness of: bg, panel, panel2, line, hover, hover line, log background, toast
    const L = dark
      ? skin.black ? [0, 4, 7, 13, 10, 18, 2, 8] : [6, 10, 13.5, 20, 17, 25, 7, 14]
      : [96, 100, 94, 88, 91, 82, 99, 100];
    const [bg, panel, panel2, line, hover, hoverLine, logBg, toast] = L.map((l) => surface(h, s, l));
    return {
      '--bg': bg, '--panel': panel, '--panel2': panel2, '--line2': line,
      '--btn-hover': hover, '--btn-hover-line': hoverLine,
      '--accent': accent.accent, '--accent-hi': accent.hi,
      '--accent-soft': rgba(accent.accent, dark ? 0.16 : 0.12),
      '--accent-text': accent.text, '--code': accent.text,
      '--accent-line': rgba(accent.accent, dark ? 0.45 : 0.4),
      '--accent-faint': rgba(accent.accent, dark ? 0.09 : 0.07),
      '--mark-bg': rgba(accent.accent, dark ? 0.45 : 0.28),
      '--savebar': rgba(panel, 0.96), '--toast-bg': toast, '--log-bg': logBg,
    };
  }

  /** Window background and caption-button colours; must equal --bg of the page (see theme.test.js). */
  function windowColors(name, mode) {
    const vars = skinVariables(name, mode);
    return { background: vars['--bg'] || DEFAULT_SURFACES[mode].bg, symbols: SYMBOLS[mode] };
  }

  /** Three colours to preview a style in a picker. */
  function preview(name, mode) {
    const vars = skinVariables(name, mode);
    const base = DEFAULT_SURFACES[mode];
    return { bg: vars['--bg'] || base.bg, panel: vars['--panel'] || base.panel, accent: vars['--accent'] || base.accent };
  }

  return { SKINS, SKIN_NAMES, isSkin, skinVariables, windowColors, preview };
});
