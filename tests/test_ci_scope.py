"""scripts/ci-scope.sh: which pull requests skip the CI work (docs/DESIGN.md, CI)."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _scope(tmp_path: Path, changed: list[str], event: str = "pull_request") -> str:
    repo = tmp_path / "repo"
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "base"], check=True)
    for name in changed:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text("x\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "change"], check=True)
    out = tmp_path / "out"
    subprocess.run(
        [str(ROOT / "scripts/ci-scope.sh")],
        cwd=repo,
        env={**os.environ, "GITHUB_OUTPUT": str(out), "GITHUB_EVENT_NAME": event},
        check=True,
        capture_output=True,
    )
    return out.read_text().strip()


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        (["docs/DESIGN.md"], "full=false"),
        (["README.md", "docs/DESIGN.md"], "full=false"),
        (["docs/RUNBOOK.md"], "full=true"),  # tests and the e2e read the release digest
        (["docs/DESIGN.md", "src/shortly/app.py"], "full=true"),
        ([".github/workflows/ci.yml"], "full=true"),
    ],
)
def test_only_markdown_other_than_the_runbook_skips(
    tmp_path: Path, changed: list[str], expected: str
) -> None:
    assert _scope(tmp_path, changed) == expected


def test_pushes_to_main_always_run_everything(tmp_path: Path) -> None:
    assert _scope(tmp_path, ["docs/DESIGN.md"], event="push") == "full=true"
