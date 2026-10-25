"""The platform under Argo CD (deploy/argocd/platform/, DESIGN: GitOps with Argo CD).

The chart renders the child Applications; these checks hold for every way it is rendered:
  - every chart is pulled by digest, from a registry the platform project allows
  - every git source deploys the revision the root asks for, from this repository
  - the children stay inside the platform project's namespaces, sync in the right waves,
    and orphan (not delete) their resources if the Application goes
  - the vendored metrics-server manifest is byte for byte the one that was pinned
and (integration) each chart's version still resolves to the digest pinned for it.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CHART = ROOT / "deploy/argocd/platform"
VALUES = yaml.safe_load((CHART / "values.yaml").read_text())
PROJECT = yaml.safe_load((ROOT / "deploy/argocd/platform-project.yaml").read_text())["spec"]
REPO = "https://github.com/statsguy/shortly.git"
WAVE_1 = {"gateway", "tool-routes", "kyverno-policies"}  # they use CRDs wave 0 installs


def _render(*sets: str) -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        if os.environ.get("CI"):
            pytest.fail("helm is required in CI")
        pytest.skip("helm not installed")
    args = [arg for s in ("arch=amd64", "revision=0123abc", *sets) for arg in ("--set", s)]
    out = subprocess.run(
        ["helm", "template", "platform", str(CHART), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d and d["kind"] == "Application"]


ALL_ON = ()
ALL_OFF = ("components.monitoring=false", "components.policies=false", "components.backups=false")


def _sources(app: dict[str, Any]) -> list[dict[str, Any]]:
    spec = app["spec"]
    return spec.get("sources") or [spec["source"]]


@pytest.mark.parametrize("sets", [ALL_ON, ALL_OFF], ids=["all", "minimal"])
def test_children_are_pinned_and_stay_in_the_platform_project(sets: tuple[str, ...]) -> None:
    allowed_repos = set(PROJECT["sourceRepos"])
    allowed_ns = {d["namespace"] for d in PROJECT["destinations"]}
    for app in _render(*sets):
        name = app["metadata"]["name"]
        assert app["kind"] == "Application"
        assert app["metadata"]["namespace"] == "argocd"
        assert app["spec"]["project"] == "platform", name
        assert app["spec"]["destination"]["namespace"] in allowed_ns, name
        # Orphan, don't cascade: a deleted Application must not take the platform with it.
        assert "finalizers" not in app["metadata"], name
        sync = app["spec"]["syncPolicy"]
        assert sync["automated"] == {"prune": True, "selfHeal": True}, name
        assert "ServerSideApply=true" in sync["syncOptions"], name
        for source in _sources(app):
            assert source["repoURL"] in allowed_repos, (name, source["repoURL"])
            if source["repoURL"] == REPO:
                assert source["targetRevision"] == "0123abc", name  # the root's revision
            else:  # an OCI chart: by digest, never a tag
                assert re.fullmatch(r"sha256:[0-9a-f]{64}", source["targetRevision"]), name
            for values_file in source.get("helm", {}).get("valueFiles", []):
                assert (ROOT / values_file.removeprefix("$values/")).is_file(), values_file


def test_waves_put_crd_users_after_the_charts_that_install_the_crds() -> None:
    waves = {
        a["metadata"]["name"]: a["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]
        for a in _render()
    }
    assert {name for name, wave in waves.items() if wave == "1"} == WAVE_1
    assert set(waves.values()) == {"0", "1"}


def test_components_can_be_switched_off() -> None:
    names = {a["metadata"]["name"] for a in _render(*ALL_OFF)}
    assert names == {"metrics-server", "envoy-gateway", "argo-rollouts", "gateway", "tool-routes"}
    routes = next(a for a in _render(*ALL_OFF) if a["metadata"]["name"] == "tool-routes")
    assert routes["spec"]["source"]["directory"]["include"] == "argocd.yaml"


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_rollouts_plugin_matches_the_node_architecture(arch: str) -> None:
    app = next(a for a in _render(f"arch={arch}") if a["metadata"]["name"] == "argo-rollouts")
    (plugin,) = app["spec"]["sources"][0]["helm"]["valuesObject"]["controller"][
        "trafficRouterPlugins"
    ]
    assert plugin["location"].endswith(f"-linux-{arch}")
    assert plugin["sha256"] == VALUES["gatewayAPIPlugin"]["sha256"][arch]


def test_helm_releases_handed_over_are_the_ones_argo_cd_renders() -> None:
    """cluster.sh deletes these Helm records once Argo CD owns the resources: names must match."""
    script = (ROOT / "scripts/cluster.sh").read_text()
    match = re.search(r"^HELM_RELEASES=\(([^)]*)\)", script, re.M)
    assert match
    handed_over = set(match[1].split())
    rendered = {
        f"{a['spec']['destination']['namespace']}/{a['spec']['sources'][0]['helm']['releaseName']}"
        for a in _render()
        if "sources" in a["spec"]
    }
    assert handed_over == rendered


def test_root_application_points_at_the_chart() -> None:
    root = yaml.safe_load((ROOT / "deploy/argocd/platform-root.yaml").read_text())
    assert root["spec"]["project"] == "platform"
    assert root["spec"]["source"]["repoURL"] == REPO
    assert root["spec"]["source"]["path"] == "deploy/argocd/platform"
    assert REPO in PROJECT["sourceRepos"]


def test_argo_cd_alerts_ship_only_with_monitoring() -> None:
    def kinds(*sets: str) -> list[str]:
        args = [a for x in ("arch=amd64", *sets) for a in ("--set", x)]
        out = subprocess.run(
            ["helm", "template", "platform", str(CHART), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return [d["kind"] for d in yaml.safe_load_all(out) if d]

    _render()  # skips without helm
    assert {"ServiceMonitor", "PrometheusRule"} <= set(kinds())
    # Their CRDs arrive in wave 0: Argo CD's dry run must not fail the root's whole sync.
    out = subprocess.run(
        ["helm", "template", "platform", str(CHART), "--set", "arch=amd64"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for doc in (d for d in yaml.safe_load_all(out) if d and d["kind"] != "Application"):
        options = doc["metadata"]["annotations"]["argocd.argoproj.io/sync-options"]
        assert "SkipDryRunOnMissingResource=true" in options, doc["kind"]
    assert not {"ServiceMonitor", "PrometheusRule"} & set(kinds("components.monitoring=false"))


def test_vendored_metrics_server_is_the_pinned_release() -> None:
    folder = ROOT / "deploy/platform/metrics-server"
    want, name = (folder / "SHA256SUMS").read_text().split()
    assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == want


# ------------------------------------------------------------------- real registries
def _registry_digest(repo: str, tag: str) -> str:
    """The digest a registry serves for repo:tag (anonymous pull token, HEAD manifest)."""
    host, path = repo.removeprefix("oci://").split("/", 1)
    token_url = {
        "ghcr.io": f"https://ghcr.io/token?scope=repository:{path}:pull",
        "registry-1.docker.io": "https://auth.docker.io/token?service=registry.docker.io"
        f"&scope=repository:{path}:pull",
    }[host]
    with urllib.request.urlopen(token_url, timeout=20) as resp:  # noqa: S310 (fixed hosts)
        token = json.load(resp)["token"]
    request = urllib.request.Request(
        f"https://{host}/v2/{path}/manifests/{tag}",
        method="HEAD",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.oci.image.manifest.v1+json",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as resp:  # noqa: S310
        return str(resp.headers["Docker-Content-Digest"])


@pytest.mark.integration
@pytest.mark.parametrize("name", sorted(VALUES["charts"]))
def test_chart_versions_still_resolve_to_their_pinned_digests(name: str) -> None:
    chart = VALUES["charts"][name]
    assert _registry_digest(chart["repo"], chart["version"]) == chart["digest"], (
        f"{name} {chart['version']} no longer resolves to the pinned digest: re-pushed tag?"
    )
