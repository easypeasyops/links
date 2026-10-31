"""platform.lock: the platform commit this repo is tested and deployed against (plan 3.4).

CI checks out easypeasyops/platform at that commit for the kind e2e; scripts/lib/platform.sh
reads it too. A malformed lock would fail CI only in the kind jobs, minutes in: catch it here.
"""

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = re.compile(r"^easypeasyops/platform@([0-9a-f]{40})$", re.MULTILINE)


def test_lock_names_exactly_one_full_commit() -> None:
    assert len(LOCK.findall((ROOT / "platform.lock").read_text())) == 1


def test_ci_and_scripts_read_the_lock_the_same_way() -> None:
    """The sed in CI and in scripts/lib/platform.sh is the same pattern as this test's."""
    sed = r"s/^easypeasyops\/platform@([0-9a-f]{40})$/\1/p"
    for path in ("scripts/lib/platform.sh", ".github/workflows/ci.yml"):
        assert sed in (ROOT / path).read_text(), path


def test_reusable_workflows_are_the_locked_platform() -> None:
    """CI calls the platform's reusable workflows at the lock's commit, never another."""
    lock = LOCK.findall((ROOT / "platform.lock").read_text())[0]
    uses = re.findall(
        r"uses: easypeasyops/platform/\.github/workflows/([\w.-]+)@(\S+)",
        (ROOT / ".github/workflows/ci.yml").read_text(),
    )
    assert {name for name, _ in uses} == {"reusable-python-check.yml", "reusable-image.yml"}
    assert {ref for _, ref in uses} == {lock}


def test_k6_image_is_the_platform_charts(stub_platform: Path) -> None:
    """k6_image (scripts/lib/platform.sh) reads the k6 the service chart runs: the image CI
    preloads into kind and the HPA drill uses."""
    values = stub_platform / "charts/service/values.yaml"
    values.parent.mkdir(parents=True)
    values.write_text(
        "canary:\n  analysis:\n    syntheticTraffic:\n      image: grafana/k6:9.9.9\n"
    )
    out = subprocess.run(
        ["bash", "-c", f'source "{ROOT}/scripts/lib/platform.sh" && k6_image'],
        env={**os.environ, "PLATFORM_DIR": str(stub_platform)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out == "grafana/k6:9.9.9\n"
