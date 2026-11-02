"""Guard against interpreter drift between what we test and what we ship.

Real incident in this repo: Dependabot bumped the Dockerfile to Python 3.14 while
.python-version (and therefore CI's test runs) stayed on 3.13. The smoke test passed,
so nothing went red, but the unit/integration suite was testing a different interpreter
than production. This test makes that mismatch a CI failure, so an interpreter upgrade
has to be a deliberate change to both files together.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _dockerfile_pythons() -> set[str]:
    text = (ROOT / "Dockerfile").read_text()
    return set(re.findall(r"^FROM python:(\d+\.\d+)", text, flags=re.MULTILINE))


def test_dockerfile_stages_share_one_python() -> None:
    versions = _dockerfile_pythons()
    assert len(versions) == 1, f"builder and runtime use different Pythons: {versions}"


def test_dockerfile_matches_python_version_file() -> None:
    pinned = (ROOT / ".python-version").read_text().strip()
    assert _dockerfile_pythons() == {pinned}, (
        f"Dockerfile uses {_dockerfile_pythons()} but .python-version is {pinned}; "
        "upgrade both together (and requires-python / ruff target-version)"
    )


def test_tests_run_on_the_pinned_python() -> None:
    pinned = (ROOT / ".python-version").read_text().strip()
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert running == pinned, f"tests are running on {running}, project pins {pinned}"
