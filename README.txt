Drag everything in this folder (except README.txt / changes.patch) onto the
root of your Twitch-Radio repo and accept "replace".

  .github/workflows/ci.yml   new `autoformat` job: runs ruff format and commits the result
  twitch_radio/...           the 16 files ruff wanted reformatted

Optional: add a repo secret named AUTOFORMAT_TOKEN (a PAT or GitHub App token with
contents:write) so the bot's format commit also triggers its own CI run.
If main is branch-protected against direct pushes, allow github-actions[bot] to push
or the autoformat job's push will be rejected on main (it works fine on PR branches).
