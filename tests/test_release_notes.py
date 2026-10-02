import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "release_notes", Path(__file__).resolve().parent.parent / "scripts" / "release_notes.py"
)
assert _SPEC and _SPEC.loader
release_notes = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(release_notes)


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Add block and unblock commands", ("Added", "Add block and unblock commands")),
        ("Fix settings page crash", ("Fixed", "Fix settings page crash")),
        ("Remove the old service file", ("Removed", "Remove the old service file")),
        ("Delete the old service file", ("Removed", "Delete the old service file")),
        (
            "Update comment for Twitch bot configuration",
            ("Changed", "Update comment for Twitch bot configuration"),
        ),
        ("Bump actions/checkout from 6 to 7", ("Dependencies", "Bump actions/checkout from 6 to 7")),
        ("feat(player): prefetch next track", ("Added", "Prefetch next track")),
        ("fix: stalled decoder", ("Fixed", "Stalled decoder")),
        ("chore: tidy imports", ("Changed", "Tidy imports")),
        ("tweak volume", ("Changed", "Tweak volume")),
    ],
)
def test_classify(subject: str, expected: tuple[str, str]) -> None:
    assert release_notes.classify(subject) == expected


@pytest.mark.parametrize(
    "subject",
    ["Release 1.2.0", "release v1.2.0", "style: ruff format", "Merge pull request #4 from a/b", "", "  "],
)
def test_noise_is_dropped(subject: str) -> None:
    assert release_notes.classify(subject) is None


def test_sections_are_grouped_ordered_and_deduplicated() -> None:
    commits = [
        ("c5", "Release 1.2.0"),
        ("c4", "Fix crash on empty queue"),
        ("c3", "Add !radio command"),
        ("c2", "Add !radio command"),
        ("c1", "Tweak volume"),
    ]
    sections = release_notes.build_sections(commits)
    assert list(sections) == ["Added", "Fixed", "Changed"]
    assert sections["Added"] == ["- Add !radio command (c3)"]


def test_render_with_previous_release() -> None:
    text = release_notes.render("v1.2.0", "v1.1.0", "me/repo", [("a1", "Fix crash"), ("a2", "Add feature")])
    assert text.startswith("## What's changed since v1.1.0")
    assert text.index("### Added") < text.index("### Fixed")
    assert "https://github.com/me/repo/compare/v1.1.0...v1.2.0" in text


def test_render_first_release_and_empty_release() -> None:
    assert "First release." in release_notes.render("v1.0.0", None, "me/repo", [])
    empty = release_notes.render("v1.0.1", "v1.0.0", "me/repo", [("a1", "Release 1.0.1")])
    assert "no user-visible changes" in empty


@pytest.mark.parametrize(
    "subject",
    [
        "Add files via upload",
        "Update bot.py",
        "Create twitch_radio/newthing.py",
        "Delete deploy/twitch-sr-bot.service",
        "Rename a.py to b.py",
        "update",
        "Initial commit",
        "WIP",
    ],
)
def test_generic_messages_are_recognised(subject: str) -> None:
    assert release_notes.is_generic(subject)


@pytest.mark.parametrize(
    "subject",
    ["Update comment for Twitch bot configuration", "Add !shuffle command", "Fix crash on empty queue"],
)
def test_real_messages_are_not_generic(subject: str) -> None:
    assert not release_notes.is_generic(subject)


def test_generic_commits_are_left_out_of_the_message_sections() -> None:
    sections = release_notes.build_sections([("a1", "Add files via upload"), ("a2", "Fix real bug")])
    assert sections == {"Fixed": ["- Fix real bug (a2)"]}


DIFF = """\
diff --git a/twitch_radio/components/song_requests.py b/twitch_radio/components/song_requests.py
--- a/twitch_radio/components/song_requests.py
+++ b/twitch_radio/components/song_requests.py
@@ -1,0 +2,3 @@
+    @commands.command(name="shuffle", aliases=["mix"])
+    async def shuffle_queue(self, ctx):
+        pass
-    @commands.command(name="oldcmd")
-    async def oldcmd(self, ctx):
diff --git a/twitch_radio/config.py b/twitch_radio/config.py
--- a/twitch_radio/config.py
+++ b/twitch_radio/config.py
+MAX_MINUTES = _int_env("MAX_QUEUE_MINUTES", 60)
-AUDIO = _int_env("AUDIO_OLD_NAME", 1)
diff --git a/twitch_radio/tunables.py b/twitch_radio/tunables.py
--- a/twitch_radio/tunables.py
+++ b/twitch_radio/tunables.py
+    "history_size": (1, 50),
diff --git a/twitch_radio/player.py b/twitch_radio/player.py
--- a/twitch_radio/player.py
+++ b/twitch_radio/player.py
+def prefetch(queue_len: int, extra: int) -> bool:
-def prefetch(queue_len: int) -> bool:
+def brand_new() -> None:
+def _private_helper() -> None:
+class Fresh:
-def gone_function():
"""


def test_detect_logic_finds_commands_settings_and_code() -> None:
    logic = release_notes.detect_logic(DIFF)
    assert logic["commands"] == {"added": {"!shuffle"}, "removed": {"!oldcmd"}}
    assert logic["settings"] == {"added": {"MAX_QUEUE_MINUTES"}, "removed": {"AUDIO_OLD_NAME"}}
    assert logic["tunables"]["added"] == {"history_size"}
    # an edited signature is neither added nor removed; private helpers are ignored
    assert logic["code"]["added"] == {"shuffle_queue()", "brand_new()", "Fresh"}
    assert logic["code"]["removed"] == {"oldcmd()", "gone_function()"}


def test_merge_file_states_nets_out_the_commits() -> None:
    merged_status, merged_counts = release_notes.merge_file_states(
        [
            ({"a.py": "A", "b.py": "M", "tmp.py": "A"}, {"a.py": (10, 0), "b.py": (2, 1), "tmp.py": (5, 0)}),
            ({"a.py": "M", "b.py": "D", "tmp.py": "D"}, {"a.py": (3, 2), "b.py": (0, 9), "tmp.py": (0, 5)}),
        ]
    )
    assert merged_status == {"a.py": "A", "b.py": "D"}  # tmp.py was added and deleted: not reported
    assert merged_counts["a.py"] == (13, 2)


def test_area_of() -> None:
    assert release_notes.area_of("twitch_radio/player.py") == "Audio playback"
    assert release_notes.area_of("twitch_radio/admin/app.py") == "Settings web page"
    assert release_notes.area_of("gui/main.js") == "Desktop app"
    assert release_notes.area_of(".github/workflows/ci.yml") == "Build and release"
    assert release_notes.area_of("weird/thing.bin") == "Other"


def test_render_code_changes_and_lockfiles_ignored() -> None:
    logic = release_notes.detect_logic(DIFF)
    lines = release_notes.render_code_changes(
        {"twitch_radio/player.py": "M", "gui/package-lock.json": "M", "deploy/x.service": "D"},
        {"twitch_radio/player.py": (5, 1), "gui/package-lock.json": (900, 900), "deploy/x.service": (0, 7)},
        logic,
    )
    text = "\n".join(lines)
    assert "New chat commands: `!shuffle`" in text
    assert "**Audio playback:** `player.py` (+5 -1)" in text
    assert "removed `x.service` (+0 -7)" in text
    assert "package-lock" not in text


def test_render_uses_code_lines_when_messages_say_nothing() -> None:
    text = release_notes.render(
        "v1.1.0",
        "v1.0.0",
        "me/repo",
        [("a1", "Add files via upload")],
        ["- **Desktop app:** `main.js` (+1 -0)"],
    )
    assert "### Changes (detected from the code)" in text
    assert "### Also changed" not in text
    mixed = release_notes.render("v1.1.0", "v1.0.0", "me/repo", [("a1", "Fix bug")], ["- x"])
    assert "### Also changed (detected from the code)" in mixed


def test_render_prefers_ai_text() -> None:
    text = release_notes.render(
        "v1.1.0", "v1.0.0", "me/repo", [("a1", "Fix bug")], ["- x"], "### Added\n- A thing"
    )
    assert "- A thing" in text and "Summarised automatically by Claude" in text
    assert "Fix bug" not in text and "detected from the code" not in text


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        import io
        import json

        self._buf = io.BytesIO(json.dumps(payload).encode())

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, *a: object) -> bytes:
        return self._buf.read(*a)


def test_ai_summary_without_key_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert release_notes.ai_summary("v1.0.0", "v1.1.0", []) is None


def test_ai_summary_accepts_good_reply_and_rejects_bad(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(release_notes, "git", lambda *a: "diff --git a/x.py b/x.py\n+print(1)\n")
    good = {"content": [{"type": "text", "text": "### Added\n- Something"}]}
    monkeypatch.setattr(release_notes.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(good))
    assert release_notes.ai_summary("v1.0.0", "v1.1.0", [("a", "Fix x")]) == "### Added\n- Something"
    bad = {"content": [{"type": "text", "text": "Great release!"}]}
    monkeypatch.setattr(release_notes.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(bad))
    assert release_notes.ai_summary("v1.0.0", "v1.1.0", []) is None


def test_trim_diff_drops_lockfiles_and_caps_size() -> None:
    big = "diff --git a/big.py b/big.py\n" + ("+x\n" * 20000)
    lock = "diff --git a/gui/package-lock.json b/gui/package-lock.json\n+huge\n"
    out = release_notes._trim_diff(lock + big)
    assert "package-lock" not in out
    assert "truncated" in out and len(out) < 20000


def test_functions_in_new_files_are_not_listed() -> None:
    diff = "+++ b/twitch_radio/new_module.py\n+def helper_one() -> None:\n+class Thing:\n+++ b/twitch_radio/old.py\n+def added_later() -> None:\n"
    logic = release_notes.detect_logic(diff, frozenset({"twitch_radio/new_module.py"}))
    assert logic["code"]["added"] == {"added_later()"}
