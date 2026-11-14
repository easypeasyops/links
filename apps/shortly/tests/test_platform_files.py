"""deploy/platform/: shortly's env files and AppProject, in the platform's format.

The platform stopped deploying shortly (platform #80), so they live here. deploy.sh and the
kind e2e read them through scripts/lib/platform.sh; a mistake would otherwise show only in
the kind jobs, minutes in.
"""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parents[1]


def _sh(stub_platform: Path, call: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "{ROOT}/scripts/lib/platform.sh" && {call}'],
        env={**os.environ, "PLATFORM_DIR": str(stub_platform)},
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_env_file_names_a_chart_digest_and_values_files_that_exist(
    stub_platform: Path, env: str
) -> None:
    chart = _sh(stub_platform, f"chart_ref {env}")
    assert chart.returncode == 0, chart.stderr
    assert chart.stdout.startswith("oci://ghcr.io/easypeasyops/charts/service@sha256:")
    values = _sh(stub_platform, f"values_args {env}").stdout.split()
    files = [Path(path) for flag, path in zip(values[::2], values[1::2], strict=True)]
    assert values[::2] == ["--values"] * len(files)
    assert [str(path.relative_to(REPO)) for path in files] == [
        "apps/shortly/deploy/service/values.yaml",
        "apps/shortly/deploy/service/slo-rules.values.yaml",
        f"apps/shortly/deploy/envs/{env}.yaml",
    ]
    assert all(path.is_file() for path in files)


def test_an_unknown_environment_is_refused(stub_platform: Path) -> None:
    result = _sh(stub_platform, "chart_ref qa")
    assert result.returncode != 0
    assert "shortly has no environment qa" in result.stderr
