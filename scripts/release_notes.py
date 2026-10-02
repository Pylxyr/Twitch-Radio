#!/usr/bin/env python3
"""Build the "what changed" section of a GitHub release from the commits since the previous release.

  python scripts/release_notes.py --tag v1.2.0 --repo owner/name            # previous release found automatically
  python scripts/release_notes.py --tag v1.2.0 --prev v1.1.0 --repo owner/name

Commits are grouped by their first word ("Add ..." -> Added, "Fix ..." -> Fixed, and so on; the
conventional `feat:` / `fix:` prefixes work too). Release bumps, automatic formatting commits and
merge commits are left out. Standard library only, so the release job needs no install step.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

# (section title, in display order)
SECTIONS = ("Added", "Fixed", "Changed", "Removed", "Dependencies")

# Commits that only exist because of the release/CI machinery.
_NOISE = re.compile(
    r"^(release\s+v?\d|style:\s*ruff format|merge\b|revert\s+\"?merge\b)",
    re.IGNORECASE,
)
_CONVENTIONAL = re.compile(r"^(?P<type>[a-z]+)(\([^)]*\))?!?:\s*(?P<rest>.+)$", re.IGNORECASE)
_CONVENTIONAL_SECTION = {"feat": "Added", "fix": "Fixed", "deps": "Dependencies"}
_WORD_SECTION = {
    "add": "Added",
    "adds": "Added",
    "added": "Added",
    "new": "Added",
    "implement": "Added",
    "introduce": "Added",
    "support": "Added",
    "fix": "Fixed",
    "fixes": "Fixed",
    "fixed": "Fixed",
    "repair": "Fixed",
    "resolve": "Fixed",
    "correct": "Fixed",
    "remove": "Removed",
    "removed": "Removed",
    "delete": "Removed",
    "drop": "Removed",
    "bump": "Dependencies",
}
_SEMVER_TAG = re.compile(r"^v\d+\.\d+\.\d+")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def previous_tag(tag: str) -> str | None:
    """The release before `tag`. A stable release skips pre-release tags, so v1.1.0 is
    compared with v1.0.0 rather than with v1.1.0-rc1."""
    cmd = ["describe", "--tags", "--abbrev=0", "--match", "v*.*.*"]
    if "-" not in tag:
        cmd += ["--exclude", "*-*"]
    try:
        return git(*cmd, f"{tag}^").strip() or None
    except subprocess.CalledProcessError:
        return None


def classify(subject: str) -> tuple[str, str] | None:
    """(section, cleaned subject) for a commit subject, or None to leave it out."""
    subject = subject.strip()
    if not subject or _NOISE.match(subject):
        return None
    section = ""
    match = _CONVENTIONAL.match(subject)
    if match:
        kind = match.group("type").lower()
        if kind in _CONVENTIONAL_SECTION:
            section = _CONVENTIONAL_SECTION[kind]
        subject = match.group("rest").strip()
    if not section:
        first = re.split(r"\W+", subject, maxsplit=1)[0].lower()
        section = _WORD_SECTION.get(first, "Changed")
    return section, subject[:1].upper() + subject[1:]


def build_sections(commits: list[tuple[str, str]]) -> dict[str, list[str]]:
    """Group (short hash, subject) pairs, newest first, dropping noise and repeated subjects."""
    grouped: dict[str, list[str]] = {name: [] for name in SECTIONS}
    seen: set[str] = set()
    for short, subject in commits:
        item = classify(subject)
        if item is None:
            continue
        section, text = item
        if text.lower() in seen:
            continue
        seen.add(text.lower())
        grouped[section].append(f"- {text} ({short})")
    return {name: lines for name, lines in grouped.items() if lines}


def render(tag: str, prev: str | None, repo: str, commits: list[tuple[str, str]]) -> str:
    out: list[str] = [f"## What's changed{f' since {prev}' if prev else ''}", ""]
    if prev is None:
        out += ["First release.", ""]
    else:
        sections = build_sections(commits)
        if not sections:
            out += ["Maintenance release with no user-visible changes.", ""]
        for name, lines in sections.items():
            out += [f"### {name}", *lines, ""]
        if repo:
            out += [f"**Full changelog:** https://github.com/{repo}/compare/{prev}...{tag}", ""]
    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--tag", required=True)
    parser.add_argument("--prev", help="previous release tag (default: found automatically)")
    parser.add_argument("--repo", default="", help="owner/name, for the compare link")
    args = parser.parse_args()
    if not _SEMVER_TAG.match(args.tag):
        print(f"'{args.tag}' does not look like a release tag (vMAJOR.MINOR.PATCH).", file=sys.stderr)
        return 1
    prev = args.prev or previous_tag(args.tag)
    rng = f"{prev}..{args.tag}" if prev else args.tag
    log = git("log", "--no-merges", "--pretty=%h%x1f%s", rng) if prev else ""
    commits = [tuple(line.split("\x1f", 1)) for line in log.splitlines() if "\x1f" in line]
    sys.stdout.write(render(args.tag, prev, args.repo, commits))  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    sys.exit(main())
