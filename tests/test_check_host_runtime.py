import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_host_runtime", Path(__file__).resolve().parent.parent / "scripts" / "check_host_runtime.py"
)
assert _SPEC and _SPEC.loader
check_host_runtime = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_host_runtime)


def _node_major() -> int:
    node = shutil.which("node")
    if not node:
        return 0
    out = subprocess.run([node, "--version"], capture_output=True, text=True, check=False).stdout.strip()
    return int(out.lstrip("v").split(".")[0]) if out.startswith("v") else 0


@pytest.mark.skipif(_node_major() < 22, reason="needs Node 22+ on PATH")
def test_a_real_node_passes() -> None:
    assert check_host_runtime.check(shutil.which("node") or "") == []


def test_an_executable_that_is_not_node_fails_every_check() -> None:
    problems = check_host_runtime.check(sys.executable)
    assert len(problems) == 3
    assert any("--version" in p for p in problems)


def test_main_reports_missing_file(capsys: pytest.CaptureFixture[str]) -> None:
    assert check_host_runtime.main(["x", "/no/such/app"]) == 1
    assert "does not exist" in capsys.readouterr().out
