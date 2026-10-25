"""The platform under Argo CD (deploy/argocd/platform/, DESIGN: GitOps with Argo CD).

The chart renders the child Applications; these checks hold for every way it is rendered:
  - every git source deploys the revision the root asks for, from this repository
  - the children stay inside the platform project's namespaces, sync in the right waves,
    and orphan (not delete) their resources if the Application goes
  - the vendored metrics-server manifest is byte for byte the one that was pinned
"""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CHART = ROOT / "deploy/argocd/platform"
PROJECT = yaml.safe_load((ROOT / "deploy/argocd/platform-project.yaml").read_text())["spec"]
REPO = "https://github.com/statsguy/shortly.git"
WAVE_1 = {"kyverno-policies"}  # needs Kyverno's webhook serving, not just its CRDs


def _render(*sets: str) -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        if os.environ.get("CI"):
            pytest.fail("helm is required in CI")
        pytest.skip("helm not installed")
    args = [arg for s in ("revision=0123abc", *sets) for arg in ("--set", s)]
    out = subprocess.run(
        ["helm", "template", "platform", str(CHART), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d and d["kind"] == "Application"]


ALL_ON = ()
ALL_OFF = ("components.monitoring=false", "components.policies=false", "components.backups=false")


@pytest.mark.parametrize("sets", [ALL_ON, ALL_OFF], ids=["all", "minimal"])
def test_children_stay_in_the_platform_project(sets: tuple[str, ...]) -> None:
    allowed_repos = set(PROJECT["sourceRepos"])
    allowed_ns = {d["namespace"] for d in PROJECT["destinations"]}
    for app in _render(*sets):
        name = app["metadata"]["name"]
        assert app["metadata"]["namespace"] == "argocd"
        assert app["spec"]["project"] == "platform", name
        assert app["spec"]["destination"]["namespace"] in allowed_ns, name
        # Orphan, don't cascade: a deleted Application must not take the platform with it.
        assert "finalizers" not in app["metadata"], name
        sync = app["spec"]["syncPolicy"]
        assert sync["automated"] == {"prune": True, "selfHeal": True}, name
        assert "ServerSideApply=true" in sync["syncOptions"], name
        source = app["spec"]["source"]
        assert source["repoURL"] in allowed_repos, name
        assert source["targetRevision"] == "0123abc", name  # the root's revision


def test_only_what_needs_a_running_webhook_waits_for_wave_1() -> None:
    waves = {
        a["metadata"]["name"]: a["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]
        for a in _render()
    }
    assert {name for name, wave in waves.items() if wave == "1"} == WAVE_1
    assert set(waves.values()) == {"0", "1"}


def test_components_can_be_switched_off() -> None:
    names = {a["metadata"]["name"] for a in _render(*ALL_OFF)}
    assert names == {"metrics-server", "gateway", "tool-routes"}
    routes = next(a for a in _render(*ALL_OFF) if a["metadata"]["name"] == "tool-routes")
    assert routes["spec"]["source"]["directory"]["include"] == "argocd.yaml"


def test_root_application_points_at_the_chart() -> None:
    root = yaml.safe_load((ROOT / "deploy/argocd/platform-root.yaml").read_text())
    assert root["spec"]["project"] == "platform"
    assert root["spec"]["source"]["repoURL"] == REPO
    assert root["spec"]["source"]["path"] == "deploy/argocd/platform"
    assert REPO in PROJECT["sourceRepos"]


def test_vendored_metrics_server_is_the_pinned_release() -> None:
    folder = ROOT / "deploy/platform/metrics-server"
    want, name = (folder / "SHA256SUMS").read_text().split()
    assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == want
