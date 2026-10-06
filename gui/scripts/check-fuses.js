'use strict';
/**
 * Reads the Electron fuses of a built app and fails if they are not what the build config
 * asks for. Fuses live in the executable, so this is the only way to know a packaged app
 * really has them (a typo in the config is otherwise silently ignored).
 *
 *   node scripts/check-fuses.js "../dist/app/win-unpacked/Twitch Radio.exe"
 *
 * runAsNode must stay ON: yt-dlp runs the app's executable as Node to solve YouTube's
 * challenges (see twitch_radio/config.py and scripts/check_host_runtime.py).
 */
const path = require('path');

const exe = process.argv[2];
if (!exe) {
  console.error('usage: node scripts/check-fuses.js <path to the app executable>');
  process.exit(2);
}

const expected = require('../package.json').build.electronFuses;
const names = {
  runAsNode: 'RunAsNode',
  enableCookieEncryption: 'EnableCookieEncryption',
  enableNodeOptionsEnvironmentVariable: 'EnableNodeOptionsEnvironmentVariable',
  enableNodeCliInspectArguments: 'EnableNodeCliInspectArguments',
  enableEmbeddedAsarIntegrityValidation: 'EnableEmbeddedAsarIntegrityValidation',
  onlyLoadAppFromAsar: 'OnlyLoadAppFromAsar',
  grantFileProtocolExtraPrivileges: 'GrantFileProtocolExtraPrivileges',
};

(async () => {
  const { getCurrentFuseWire, FuseV1Options } = await import('@electron/fuses');
  const wire = await getCurrentFuseWire(path.resolve(exe));
  const ENABLED = 49; // FuseState.ENABLE (the package does not export the enum); 48 is DISABLE
  const problems = [];
  for (const [key, want] of Object.entries(expected)) {
    const option = FuseV1Options[names[key]];
    if (option === undefined) {
      problems.push(`${key}: unknown fuse name in build.electronFuses`);
      continue;
    }
    const got = wire[option] === ENABLED;
    console.log(`${got === want ? 'ok  ' : 'FAIL'} ${key} = ${got} (expected ${want})`);
    if (got !== want) problems.push(`${key} is ${got}, the build config says ${want}`);
  }
  if (problems.length) {
    console.error(`\n${problems.length} fuse problem(s):\n  - ${problems.join('\n  - ')}`);
    process.exit(1);
  }
  console.log('Electron fuses match the build config.');
})().catch((error) => {
  console.error(error.message || error);
  process.exit(1);
});
