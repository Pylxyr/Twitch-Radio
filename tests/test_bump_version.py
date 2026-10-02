import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "bump_version", Path(__file__).resolve().parent.parent / "scripts" / "bump-version.py"
)
assert _SPEC and _SPEC.loader
bump_version = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bump_version)


@pytest.mark.parametrize(
    ("current", "kind", "expected"),
    [
        ("1.0.0", "patch", "1.0.1"),
        ("1.0.9", "patch", "1.0.10"),
        ("1.4.2", "minor", "1.5.0"),
        ("1.4.2", "major", "2.0.0"),
        ("1.2.0-rc1", "patch", "1.2.1"),
    ],
)
def test_next_version(current: str, kind: str, expected: str) -> None:
    assert bump_version.next_version(current, kind) == expected


@pytest.mark.parametrize(("current", "kind"), [("1.0", "patch"), ("", "patch"), ("1.0.0", "huge")])
def test_next_version_rejects_bad_input(current: str, kind: str) -> None:
    with pytest.raises(ValueError):
        bump_version.next_version(current, kind)


def test_repo_versions_agree() -> None:
    versions = {v for v in bump_version.read_versions().values()}
    assert len(versions) == 1 and None not in versions
