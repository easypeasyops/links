"""The Kyverno admission policies in deploy/policies/, evaluated by the Kyverno CLI.

The same engine version the cluster runs (scripts/cluster.sh pins the chart), so a policy
that passes here behaves the same at admission. The provenance cases go to the real registry
and Sigstore, using the release digest the runbook documents. scripts/admission-check.sh
proves the same verdicts through the live API server.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
POLICIES = ROOT / "deploy/policies"
# kyverno-cli v1.19.1: the engine of Kyverno chart 3.9.1 (scripts/cluster.sh).
KYVERNO_CLI = (
    "ghcr.io/kyverno/kyverno-cli:v1.19.1"
    "@sha256:ced7b2be0b04250cabfe695f15307f69eb715fe23234816388af4f3812915b2a"
)
# v0.2.0's per-platform (linux/amd64) manifest: a real image from the release, but the
# attestation names the index digest only.
UNATTESTED_CHILD = "ghcr.io/statsguy/shortly@sha256:" + (
    "7025c409513225aeb5e473d2e08951cfd8cbbe9d1f574a0ac042ad8e3aa0d4ec"
)


def _docker() -> None:
    if (
        shutil.which("docker")
        and subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    ):
        return
    if os.environ.get("CI"):
        pytest.fail("Docker is required for the admission policy tests in CI")
    pytest.skip("Docker not available")


def _pod(name: str, *images: str, init: tuple[str, ...] = (), ns: str = "prod") -> dict[str, Any]:
    spec: dict[str, Any] = {
        "containers": [{"name": f"c{i}", "image": im} for i, im in enumerate(images)]
    }
    if init:
        spec["initContainers"] = [{"name": f"i{i}", "image": im} for i, im in enumerate(init)]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": ns},
        "spec": spec,
    }


def _apply(tmp_path: Path, policy: str, pods: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    """Run `kyverno apply`; return {pod name: (result, message)}."""
    work = tmp_path / "kyverno"
    work.mkdir()
    (work / "policy.yaml").write_text(policy)
    (work / "pods.yaml").write_text(yaml.safe_dump_all(pods))
    work.chmod(0o755)
    for f in work.iterdir():
        f.chmod(0o644)
    cmd = ["docker", "run", "--rm", "--network", "host", "-v", f"{work}:/w:ro", "-w", "/w"]
    # Behind an HTTPS proxy (local sandboxes): pass it through, with its CA if given.
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"):
        if os.environ.get(var):
            cmd += ["-e", var]
    if ca := os.environ.get("SSL_CERT_FILE"):
        cmd += ["-v", f"{ca}:/ca.crt:ro", "-e", "SSL_CERT_FILE=/ca.crt"]
    cmd += [KYVERNO_CLI, "apply", "policy.yaml", "--resource", "pods.yaml", "--policy-report"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    report = yaml.safe_load(result.stdout) if result.stdout.strip() else None
    assert report, result.stdout + result.stderr
    assert "results" in report, result.stdout + result.stderr
    verdicts: dict[str, tuple[str, str]] = {}
    for r in report["results"]:
        name = r["resources"][0]["name"]
        # Several results for one pod: any failure or error wins over a pass.
        if verdicts.get(name, ("pass",))[0] == "pass":
            verdicts[name] = (r["result"], r.get("message", ""))
    return verdicts


def _documented_release() -> str:
    runbook = (ROOT / "docs/RUNBOOK.md").read_text()
    match = re.search(r"^\| Current release \| v[\d.]+, `(sha256:[0-9a-f]{64})`", runbook, re.M)
    assert match, "RUNBOOK Quick reference has no 'Current release' digest"
    return f"ghcr.io/statsguy/shortly@{match[1]}"


# ---------------------------------------------------------------------- static checks


def _load(name: str) -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load((POLICIES / name).read_text())
    return doc


def test_provenance_policy_matches_the_deploy_gate() -> None:
    """Admission and scripts/verify-image.sh must accept the same signer, or one of them lies."""
    policy = _load("require-release-provenance.yaml")["spec"]
    identity = policy["attestors"][0]["cosign"]["keyless"]["identities"][0]
    assert identity["issuer"] == "https://token.actions.githubusercontent.com"
    signer = re.compile(identity["subjectRegExp"])
    base = "https://github.com/statsguy/shortly/.github/workflows"
    assert signer.match(f"{base}/release.yml@refs/tags/v0.2.0")
    for wrong in (
        f"{base}/release.yml@refs/heads/main",  # a branch, not a release
        f"{base}/ci.yml@refs/tags/v0.2.0",  # CI, not the release workflow
        "https://github.com/evil/shortly/.github/workflows/release.yml@refs/tags/v0.2.0",
        f"{base}/release.yml@refs/tags/v0.2.0-evil",
    ):
        assert not signer.match(wrong), wrong
    verify = (ROOT / "scripts/verify-image.sh").read_text()
    assert "release.yml" in verify
    assert "https://slsa.dev/provenance/v1" in verify
    assert policy["attestations"][0]["intoto"]["type"] == "https://slsa.dev/provenance/v1"
    # Fail closed, refuse (not warn), and never rewrite a tag into a digest behind our back.
    assert policy["failurePolicy"] == "Fail"
    assert policy["validationActions"] == ["Deny"]
    assert policy["validationConfigurations"] == {
        "mutateDigest": False, "verifyDigest": True, "required": True
    }  # fmt: skip


def test_policies_cover_the_app_namespaces_only() -> None:
    for name in ("require-release-provenance.yaml", "allowed-images.yaml"):
        selector = _load(name)["spec"]["matchConstraints"]["namespaceSelector"]
        values = selector["matchExpressions"][0]["values"]
        assert sorted(values) == ["prod", "staging"], name


# ------------------------------------------------------------------ Kyverno CLI cases


@pytest.mark.integration
def test_allowlist_verdicts(tmp_path: Path) -> None:
    _docker()
    release = _documented_release()
    pods = [
        _pod("release", release),
        _pod("db", "postgres:17-alpine"),
        _pod("db-qualified", "docker.io/library/postgres:17-alpine"),
        _pod("k6", "grafana/k6:2.3.0"),
        # The backup Job: pg_dump in an init container, rclone (by digest) uploads.
        _pod("backup", "rclone/rclone@sha256:" + "b" * 64, init=("postgres:17-alpine",)),
        _pod("rclone-tag", "rclone/rclone:latest"),  # a tag can move: refused
        _pod("tag", "ghcr.io/statsguy/shortly:0.2.0"),
        _pod("other-image", "docker.io/attacker/miner:latest"),
        _pod("bad-init", release, init=("busybox:1.36",)),
        _pod("old-postgres", "postgres:16-alpine"),
        _pod("lookalike", "ghcr.io/statsguy/shortly-evil@sha256:" + "a" * 64),
        _pod("elsewhere", "busybox:1.36", ns="default"),
    ]
    verdicts = _apply(tmp_path, (POLICIES / "allowed-images.yaml").read_text(), pods)
    for name in ("release", "db", "db-qualified", "k6", "backup"):
        assert verdicts[name][0] == "pass", (name, verdicts[name])
    for name in ("tag", "other-image", "bad-init", "old-postgres", "lookalike", "rclone-tag"):
        assert verdicts[name][0] == "fail", (name, verdicts.get(name))
    assert "docker.io/attacker/miner:latest" in verdicts["other-image"][1]
    assert "elsewhere" not in verdicts  # not an app namespace: not evaluated


@pytest.mark.integration
def test_provenance_verdicts_against_the_real_registry(tmp_path: Path) -> None:
    _docker()
    release = _documented_release()
    pods = [
        _pod("release", release),
        _pod("release-with-sidecar", release, "postgres:17-alpine"),
        _pod("tag", "ghcr.io/statsguy/shortly:0.2.0"),
        _pod("unattested", UNATTESTED_CHILD),
        _pod("unattested-init", release, init=(UNATTESTED_CHILD,)),
    ]
    verdicts = _apply(tmp_path, (POLICIES / "require-release-provenance.yaml").read_text(), pods)
    assert verdicts["release"][0] == "pass", verdicts["release"]
    assert verdicts["release-with-sidecar"][0] == "pass", verdicts["release-with-sidecar"]
    assert verdicts["tag"] == (
        "fail",
        "image ghcr.io/statsguy/shortly:0.2.0 does not have a digest",
    )
    assert verdicts["unattested"][0] == "fail", verdicts["unattested"]
    assert verdicts["unattested-init"][0] == "fail", verdicts["unattested-init"]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (r"release\.yml@refs", r"ci\.yml@refs", "release digest with provenance"),
        ("https://actions.github.io/buildtypes/workflow/v1", "https://example.com/other",
         "not from a GitHub Actions workflow build"),
    ],
)  # fmt: skip
def test_provenance_policy_actually_checks(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    """A policy expecting a different signer or build type must refuse the real release:
    proof that the checks run, rather than passing everything."""
    _docker()
    text = (POLICIES / "require-release-provenance.yaml").read_text()
    assert old in text
    verdicts = _apply(tmp_path, text.replace(old, new), [_pod("release", _documented_release())])
    assert verdicts["release"][0] == "fail"
    assert message in verdicts["release"][1]
