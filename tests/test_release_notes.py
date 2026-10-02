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
        ("Delete deploy/twitch-sr-bot.service", ("Removed", "Delete deploy/twitch-sr-bot.service")),
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
