Drag everything here (except this README) onto the root of your Twitch-Radio repo, replace when asked.
On github.com you can also upload them: Add file > Upload files (keep the folder structure).

  .github/workflows/cut-release.yml   NEW  the one-click release button
  .github/workflows/release.yml       now also callable by cut-release.yml (tag pushes + dry run unchanged)
  scripts/bump-version.py             new `--next patch|minor|major`
  tests/test_bump_version.py          NEW  tests for it
  BUILD.md                            release instructions updated

To release: Actions > Cut release > Run workflow > choose patch/minor/major > Run workflow.
First release: leave it on "patch"; with no tags yet it releases 1.0.0 as it is.
