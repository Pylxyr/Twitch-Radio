import importlib.util
import json
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
    assert "- A thing" in text and "Summarised automatically by AI" in text
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


def test_ai_summary_without_any_service_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ANTHROPIC_API_KEY", "AI_API_KEY", "AI_BASE_URL", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
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


def test_trim_diff_drops_lockfiles_and_caps_each_file() -> None:
    big = "diff --git a/twitch_radio/big.py b/twitch_radio/big.py\n" + ("+x\n" * 20000)
    lock = "diff --git a/gui/package-lock.json b/gui/package-lock.json\n+huge\n"
    out = release_notes._trim_diff(lock + big, 50_000)
    assert "package-lock" not in out
    assert "omitted" in out and len(out) < 4_000


def test_functions_in_new_files_are_not_listed() -> None:
    diff = "+++ b/twitch_radio/new_module.py\n+def helper_one() -> None:\n+class Thing:\n+++ b/twitch_radio/old.py\n+def added_later() -> None:\n"
    logic = release_notes.detect_logic(diff, frozenset({"twitch_radio/new_module.py"}))
    assert logic["code"]["added"] == {"added_later()"}


def test_pick_provider_order() -> None:
    both = {
        "ANTHROPIC_API_KEY": "a",
        "AI_API_KEY": "b",
        "AI_BASE_URL": "https://x/v1",
        "AI_MODEL": "m",
        "GITHUB_TOKEN": "g",
    }
    assert release_notes.pick_provider(both).name == "Claude"
    del both["ANTHROPIC_API_KEY"]
    generic = release_notes.pick_provider(both)
    assert generic.name == "AI" and generic.url == "https://x/v1/chat/completions" and generic.model == "m"
    only_token = release_notes.pick_provider({"GITHUB_TOKEN": "g"})
    assert only_token.name == "GitHub Models" and only_token.url.endswith("/inference/chat/completions")
    assert only_token.budget <= 24_000  # stays inside the free tier's 8,000-token input limit
    assert release_notes.pick_provider({}) is None
    # a generic key without a model or base URL is not enough to pick that service
    assert release_notes.pick_provider({"AI_API_KEY": "k", "AI_BASE_URL": "https://x"}) is None


def test_prompt_stays_inside_the_budget_and_puts_product_code_first() -> None:
    def chunk(path: str) -> str:
        return f"diff --git a/{path} b/{path}\n" + "+line of change\n" * 120

    diff = (
        chunk("tests/test_a.py") + chunk("BUILD.md") + chunk("twitch_radio/player.py") + chunk("gui/main.js")
    )
    prompt = release_notes.build_prompt("v2", "v1", [("a", "Fix crash")], "stat", diff, 5_000)
    assert len(prompt) <= 5_000
    assert prompt.index("player.py") < prompt.index("main.js") or "main.js" in prompt
    assert "twitch_radio/player.py" in prompt
    assert (
        "not shown" in prompt and "test_a.py" in prompt.split("not shown")[1]
    )  # low priority files are only named


def test_prompt_leaves_out_generic_commit_messages() -> None:
    prompt = release_notes.build_prompt(
        "v2", "v1", [("a", "Add files via upload"), ("b", "Fix crash")], "", "", 5_000
    )
    assert "Fix crash" in prompt and "via upload" not in prompt


def test_ai_summary_reads_openai_style_replies(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ANTHROPIC_API_KEY", "AI_API_KEY", "AI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setattr(
        release_notes, "git", lambda *a: "diff --git a/twitch_radio/x.py b/twitch_radio/x.py\n+print(1)\n"
    )
    seen = {}

    def fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        return _FakeResponse({"choices": [{"message": {"content": "### Fixed\n- Something"}}]})

    monkeypatch.setattr(release_notes.urllib.request, "urlopen", fake_urlopen)
    assert release_notes.ai_summary("v1", "v2", [("a", "Fix x")]) == "### Fixed\n- Something"
    assert seen["url"].startswith("https://models.github.ai/inference") and seen["auth"] == "Bearer t"


def test_ai_summary_survives_a_quota_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setattr(release_notes, "git", lambda *a: "")

    def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise release_notes.urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(release_notes.urllib.request, "urlopen", boom)
    assert release_notes.ai_summary("v1", "v2", []) is None


def _compatible_service(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ANTHROPIC_API_KEY", "AI_MAX_OUTPUT_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AI_API_KEY", "k")
    monkeypatch.setenv("AI_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("AI_MODEL", "some/model")
    monkeypatch.setattr(release_notes, "git", lambda *a: "diff --git a/x.py b/x.py\n+print(1)\n")


def test_compatible_service_gets_room_to_think_and_it_is_adjustable(monkeypatch: pytest.MonkeyPatch) -> None:
    _compatible_service(monkeypatch)
    sent: list[dict] = []

    def fake_urlopen(request, *a, **k):  # type: ignore[no-untyped-def]
        sent.append(json.loads(request.data))
        return _FakeResponse({"choices": [{"message": {"content": "### Added\n- Something"}}]})

    monkeypatch.setattr(release_notes.urllib.request, "urlopen", fake_urlopen)
    assert release_notes.ai_summary("v1.0.0", "v1.1.0", []) == "### Added\n- Something"
    assert sent[-1]["max_tokens"] == 3000
    monkeypatch.setenv("AI_MAX_OUTPUT_TOKENS", "5000")
    release_notes.ai_summary("v1.0.0", "v1.1.0", [])
    assert sent[-1]["max_tokens"] == 5000
    for odd, expected in (("lots", 3000), ("5", 256), ("999999", 16000)):
        assert release_notes._max_output_tokens({"AI_MAX_OUTPUT_TOKENS": odd}) == expected


def test_a_reply_cut_off_before_any_text_says_what_to_change(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _compatible_service(monkeypatch)
    cut_off = {"choices": [{"message": {"content": None}, "finish_reason": "length"}]}
    monkeypatch.setattr(release_notes.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(cut_off))
    assert release_notes.ai_summary("v1.0.0", "v1.1.0", []) is None
    assert "AI_MAX_OUTPUT_TOKENS" in capsys.readouterr().err


def test_an_http_error_shows_the_services_own_explanation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io
    import urllib.error

    _compatible_service(monkeypatch)

    def refuse(request, *a, **k):  # type: ignore[no-untyped-def]
        body = io.BytesIO(b'{"error":{"message":"The model `some/model` does not exist"}}')
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, body)  # type: ignore[arg-type]

    monkeypatch.setattr(release_notes.urllib.request, "urlopen", refuse)
    assert release_notes.ai_summary("v1.0.0", "v1.1.0", []) is None
    err = capsys.readouterr().err
    assert "HTTP 404" in err and "does not exist" in err
