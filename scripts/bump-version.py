#!/usr/bin/env python3
"""Keeps the app version in one step across the three places that carry it.

python scripts/bump-version.py 1.1.0          set gui/package.json, gui/package-lock.json
                                              and twitch_radio/version.py, print next steps
python scripts/bump-version.py --check v1.1.0 fail unless all three equal the tag (used by CI)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "gui" / "package.json"
LOCK = ROOT / "gui" / "package-lock.json"
VERSION_PY = ROOT / "twitch_radio" / "version.py"
SEMVER = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")
VERSION_LINE = re.compile(r'^APP_VERSION = "([^"]*)"', re.MULTILINE)


def read_versions() -> dict[str, str | None]:
    match = VERSION_LINE.search(VERSION_PY.read_text(encoding="utf-8"))
    lock = json.loads(LOCK.read_text(encoding="utf-8")) if LOCK.exists() else {}
    return {
        "gui/package.json": json.loads(PACKAGE.read_text(encoding="utf-8")).get("version"),
        "gui/package-lock.json": lock.get("version"),
        "gui/package-lock.json (root package)": (lock.get("packages", {}).get("") or {}).get("version"),
        "twitch_radio/version.py": match.group(1) if match else None,
    }


def check(tag: str) -> int:
    expected = tag[1:] if tag.startswith("v") else tag
    if not SEMVER.match(expected):
        print(f"'{tag}' is not a vMAJOR.MINOR.PATCH[-suffix] tag.", file=sys.stderr)
        return 1
    bad = {name: found for name, found in read_versions().items() if found != expected}
    for name, found in read_versions().items():
        print(f"{'ok  ' if found == expected else 'FAIL'} {name}: {found}")
    if bad:
        print(
            f"\nThe tag says {expected} but the files above disagree. Run: python scripts/bump-version.py {expected}",
            file=sys.stderr,
        )
        return 1
    return 0


def write_json_version(path: Path, version: str) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = version
    if "packages" in data and "" in data["packages"]:
        data["packages"][""]["version"] = version
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def bump(version: str) -> int:
    if not SEMVER.match(version):
        print(f"'{version}' is not MAJOR.MINOR.PATCH (optionally -rc1 etc.).", file=sys.stderr)
        return 1
    write_json_version(PACKAGE, version)
    if LOCK.exists():
        write_json_version(LOCK, version)
    text = VERSION_PY.read_text(encoding="utf-8")
    if not VERSION_LINE.search(text):
        print("APP_VERSION not found in twitch_radio/version.py", file=sys.stderr)
        return 1
    VERSION_PY.write_text(VERSION_LINE.sub(f'APP_VERSION = "{version}"', text, count=1), encoding="utf-8")
    print(
        f"Version set to {version} in gui/package.json, gui/package-lock.json and twitch_radio/version.py.\n"
    )
    print("Next steps:")
    print(f'  git add -A && git commit -m "Release {version}"')
    print(f"  git tag v{version}")
    print(f"  git push origin main v{version}")
    print(
        "The release workflow starts when the tag arrives; versions that contain '-' publish as pre-releases."
    )
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--check":
        return check(argv[2])
    if len(argv) == 2 and not argv[1].startswith("-"):
        return bump(argv[1].removeprefix("v"))
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
