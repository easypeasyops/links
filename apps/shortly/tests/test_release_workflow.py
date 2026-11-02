"""release.yml: releases per app (platform plan 3.5). Its first step reads the app and the
version from the tag (<app>/vX.Y.Z), checks them against apps/<app>/pyproject.toml, and names
the image ghcr.io/<owner>/<repo>/<app>. That step runs here as the workflow runs it."""

import os
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]  # shortly is apps/shortly/
WORKFLOW = yaml.safe_load((REPO / ".github/workflows/release.yml").read_text())
VERSION = tomllib.loads((REPO / "apps/shortly/pyproject.toml").read_text())["project"]["version"]


def _app_step(tmp_path: Path, tag: str) -> tuple[subprocess.CompletedProcess[str], str, str]:
    (step,) = [s for s in WORKFLOW["jobs"]["release"]["steps"] if s.get("id") == "app"]
    out, env_file = tmp_path / "output", tmp_path / "env"
    out.touch()
    env_file.touch()
    env = {
        **os.environ,
        "GITHUB_REF_NAME": tag,
        "GITHUB_REPOSITORY": "EasyPeasyOps/links",
        "GITHUB_OUTPUT": str(out),
        "GITHUB_ENV": str(env_file),
    }
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]], cwd=REPO, env=env, capture_output=True, text=True
    )
    return result, out.read_text(), env_file.read_text()


def test_only_app_tags_release() -> None:
    assert WORKFLOW[True]["push"]["tags"] == ["*/v[0-9]+.[0-9]+.[0-9]+"]  # yaml reads `on` as True


def test_an_app_tag_names_the_app_its_version_and_its_image(tmp_path: Path) -> None:
    result, out, env = _app_step(tmp_path, f"shortly/v{VERSION}")
    assert result.returncode == 0, result.stdout
    assert out.splitlines() == ["app=shortly", f"version={VERSION}"]
    assert env.strip() == "IMAGE=ghcr.io/easypeasyops/links/shortly"  # lowercase, per app


@pytest.mark.parametrize(
    ("tag", "error"),
    [
        ("shortly/v99.0.0", "does not match apps/shortly/pyproject.toml"),
        ("pager/v1.0.0", "no app apps/pager/"),
        ("Shortly/v1.0.0", "is not a valid app name"),
    ],
)
def test_a_tag_that_names_no_released_version_is_refused(
    tmp_path: Path, tag: str, error: str
) -> None:
    result, out, _ = _app_step(tmp_path, tag)
    assert result.returncode != 0
    assert error in result.stdout
    assert out == ""  # nothing for the later steps
