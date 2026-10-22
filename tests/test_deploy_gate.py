"""The deploy gate: scripts/deploy.sh must refuse anything whose provenance doesn't verify.

Unit tests run deploy.sh against stub kubectl/helm/kind/docker/gh on PATH and check that
helm is (or is never) called. Integration tests run scripts/verify-image.sh against the real
registry and Sigstore, on the release digest the runbook documents.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "scripts/deploy.sh"
VERIFY = ROOT / "scripts/verify-image.sh"
DIGEST = "a" * 64
IMAGE = f"ghcr.io/statsguy/shortly@sha256:{DIGEST}"


def _attestation(ref: str, digest: str = DIGEST) -> str:
    """What `gh attestation verify --format json` prints, trimmed to what we check."""
    cert = {
        "sourceRepositoryRef": ref,
        "buildSignerURI": f"https://github.com/statsguy/shortly/.github/workflows/release.yml@{ref}",
        "sourceRepositoryURI": "https://github.com/statsguy/shortly",
        "sourceRepositoryDigest": "0123456789abcdef",
    }
    statement = {"subject": [{"digest": {"sha256": digest}}]}
    return json.dumps(
        [{"verificationResult": {"signature": {"certificate": cert}, "statement": statement}}]
    )


@pytest.fixture
def stubs(tmp_path: Path) -> tuple[Path, Path]:
    """A bin dir of fake tools that log their calls, and that log."""
    bin_dir, log = tmp_path / "bin", tmp_path / "calls.log"
    bin_dir.mkdir()
    scripts = {
        "kubectl": 'if [ "$1 $2" = "config current-context" ]; then echo "$FAKE_CONTEXT"; fi',
        "helm": "",
        "kind": "",
        "docker": "exit 1",  # `docker image inspect`: the image isn't local
        "gh": 'if [ -n "$FAKE_GH_OUTPUT" ]; then echo "$FAKE_GH_OUTPUT"; '
        "else echo denied >&2; exit 1; fi",
    }
    for name, body in scripts.items():
        path = bin_dir / name
        path.write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "{log}"\n{body}\n')
        path.chmod(0o755)
    return bin_dir, log


def _deploy(stubs: tuple[Path, Path], ref: str, **env: str) -> tuple[int, str, str]:
    bin_dir, log = stubs
    full_env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", **env}
    result = subprocess.run(
        [str(DEPLOY), "prod", ref], capture_output=True, text=True, env=full_env, check=False
    )
    calls = log.read_text() if log.exists() else ""
    return result.returncode, result.stdout + result.stderr, calls


def test_verified_release_digest_is_deployed(stubs: tuple[Path, Path]) -> None:
    code, out, calls = _deploy(
        stubs, IMAGE, FAKE_CONTEXT="prod-cluster", FAKE_GH_OUTPUT=_attestation("refs/tags/v0.2.0")
    )
    assert code == 0, out
    assert "verified: built by" in out
    assert "helm upgrade --install shortly" in calls
    assert f"image.digest=sha256:{DIGEST}" in calls


def test_digest_without_valid_provenance_is_refused(stubs: tuple[Path, Path]) -> None:
    code, out, calls = _deploy(stubs, IMAGE, FAKE_CONTEXT="kind-shortly", FAKE_GH_OUTPUT="")
    assert code != 0
    assert "REFUSED: no valid provenance" in out
    assert "helm " not in calls  # nothing was deployed


def test_digest_built_from_a_branch_is_refused(stubs: tuple[Path, Path]) -> None:
    # Signed by release.yml, but from refs/heads/...: not a release.
    code, out, calls = _deploy(
        stubs, IMAGE, FAKE_CONTEXT="kind-shortly", FAKE_GH_OUTPUT=_attestation("refs/heads/main")
    )
    assert code != 0
    assert "not from a release tag" in out
    assert "helm " not in calls


def test_attestation_for_a_different_digest_is_refused(stubs: tuple[Path, Path]) -> None:
    output = _attestation("refs/tags/v0.2.0", digest="b" * 64)
    code, _, calls = _deploy(stubs, IMAGE, FAKE_CONTEXT="kind-shortly", FAKE_GH_OUTPUT=output)
    assert code != 0
    assert "helm " not in calls


def test_tag_outside_kind_is_refused(stubs: tuple[Path, Path]) -> None:
    code, out, calls = _deploy(
        stubs, "ghcr.io/statsguy/shortly:latest", FAKE_CONTEXT="prod-cluster"
    )
    assert code != 0
    assert "is a tag" in out
    assert "helm " not in calls


def test_local_tag_on_kind_is_deployed_without_verification(stubs: tuple[Path, Path]) -> None:
    code, out, calls = _deploy(stubs, "shortly:dev", FAKE_CONTEXT="kind-shortly")
    assert code == 0, out
    assert "gh " not in calls
    assert "image.tag=dev" in calls


def test_break_glass_skips_verification_loudly(stubs: tuple[Path, Path]) -> None:
    code, out, calls = _deploy(
        stubs, IMAGE, FAKE_CONTEXT="prod-cluster", FAKE_GH_OUTPUT="", INSECURE_SKIP_VERIFY="1"
    )
    assert code == 0, out
    assert "WARNING: provenance verification SKIPPED" in out
    assert "helm upgrade" in calls


# ------------------------------------------------------------------- real registry


def _real_gh() -> None:
    if shutil.which("gh") is None:
        if os.environ.get("CI"):
            pytest.fail("gh is required for the provenance tests in CI")
        pytest.skip("gh not installed")


def _documented_release() -> str:
    """The digest the runbook's Quick reference names as the current release."""
    runbook = (ROOT / "docs/RUNBOOK.md").read_text()
    match = re.search(r"^\| Current release \| v[\d.]+, `(sha256:[0-9a-f]{64})`", runbook, re.M)
    assert match, "RUNBOOK Quick reference has no 'Current release' digest"
    return f"ghcr.io/statsguy/shortly@{match[1]}"


@pytest.mark.integration
def test_documented_release_verifies() -> None:
    _real_gh()
    result = subprocess.run([str(VERIFY), _documented_release()], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "refs/tags/v" in result.stdout


@pytest.mark.integration
def test_real_release_is_refused_for_the_wrong_signer() -> None:
    _real_gh()
    env = {**os.environ, "VERIFY_SIGNER": "statsguy/shortly/.github/workflows/ci.yml"}
    result = subprocess.run(
        [str(VERIFY), _documented_release()], capture_output=True, text=True, env=env
    )
    assert result.returncode != 0
    assert "REFUSED" in result.stderr
