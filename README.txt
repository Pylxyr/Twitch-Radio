Drag these onto the root of your Twitch-Radio repo and replace (or upload on github.com keeping the folders).

  scripts/release_notes.py             changelog now reads the code when commit messages are vague
  tests/test_release_notes.py          tests for it
  .github/workflows/release.yml        notes step passes --ai and the optional ANTHROPIC_API_KEY secret
  BUILD.md                             docs (also fixes a broken sentence from the previous version)

Optional AI summaries: repo Settings > Secrets and variables > Actions > New repository secret,
name ANTHROPIC_API_KEY, value = a key from console.anthropic.com. Without it everything still works.
