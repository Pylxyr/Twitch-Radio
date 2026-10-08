'use strict';
const js = require('@eslint/js');
const globals = require('globals');

const rules = {
  'no-var': 'error',
  'prefer-const': 'error',
  eqeqeq: ['error', 'always', { null: 'ignore' }],
  'no-unused-vars': ['error', { args: 'after-used', argsIgnorePattern: '^_', caughtErrors: 'none' }],
};

module.exports = [
  { ignores: ['node_modules/**', 'dist/**'] },
  js.configs.recommended,
  {
    // Main process, preload, scripts and tests: Node.
    files: ['**/*.js'],
    ignores: ['renderer/**'],
    languageOptions: { ecmaVersion: 2023, sourceType: 'commonjs', globals: globals.node },
    rules,
  },
  {
    // The page: a browser context with no Node. Everything it can do goes through window.api.
    // `Skins` is the global that <script src="skins.js"> defines before app.js runs (see index.html).
    files: ['renderer/**/*.js'],
    languageOptions: { ecmaVersion: 2023, sourceType: 'script', globals: { ...globals.browser, Skins: 'readonly' } },
    rules,
  },
  {
    // skins.js is shared: a plain script in the page (root.Skins) and require()d by the main process.
    files: ['renderer/skins.js'],
    languageOptions: { sourceType: 'commonjs' },
  },
];
