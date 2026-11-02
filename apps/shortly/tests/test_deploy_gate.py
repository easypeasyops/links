"""The deploy gate: scripts/deploy.sh must refuse anything whose provenance doesn't verify.

The check itself is the platform's scripts/verify-image.sh (tested in easypeasyops/platform).
Unit tests run deploy.sh against a stub verifier and stub kubectl/helm/kind/docker on PATH,
and check that helm is (or is never) called. The integration test runs the platform's real
verifier, at platform.lock's commit, on the release digest the runbook documents.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "scripts/deploy.sh"
DIGEST = "a" * 64
IMAGE = f"ghcr.io/statsguy/shortly@sha256:{DIGEST}"


@pytest.fixture
def stubs(tmp_path: Path, stub_platform: Path) -> tuple[Path, Path]:
    """A bin dir of fake tools that log their calls, and that log. The platform's verifier
    is a stub too: it passes when FAKE_VERIFY is set, and refuses otherwise."""
    bin_dir, log = tmp_path / "bin", tmp_path / "calls.log"
    bin_dir.mkdir()
    scripts = {
        "kubectl": 'if [ "$1 $2" = "config current-context" ]; then echo "$FAKE_CONTEXT"; fi',
        # `helm template --show-only templates/rollout.yaml` fails: no Rollout rendered, so
        # deploy.sh takes the plain Deployment path (the canary path is tested in kind, e2e).
        "helm": 'if [ "$1" = template ]; then exit 1; fi',
        "kind": "",
        "docker": "exit 1",  # `docker image inspect`: the image isn't local
    }
    verify = stub_platform / "scripts/verify-image.sh"
    for path, body in [(bin_dir / name, body) for name, body in scripts.items()] + [
        (
            verify,
            'if [ -n "$FAKE_VERIFY" ]; then echo "verified: built by release.yml"; '
            'else echo "REFUSED: no valid provenance for $1" >&2; exit 1; fi',
        )
    ]:
        path.write_text(f'#!/usr/bin/env bash\necho "{path.name} $*" >> "{log}"\n{body}\n')
        path.chmod(0o755)
    return bin_dir, log


def _deploy(stubs: tuple[Path, Path], ref: str, **env: str) -> tuple[int, str, str]:
    bin_dir, log = stubs
    platform = bin_dir.parent / "platform"  # the stub_platform fixture, beside bin/
    full_env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "PLATFORM_DIR": str(platform),
        **env,
    }
    result = subprocess.run(
        [str(DEPLOY), "prod", ref], capture_output=True, text=True, env=full_env, check=False
    )
    calls = log.read_text() if log.exists() else ""
    return result.returncode, result.stdout + result.stderr, calls


def test_verified_release_digest_is_deployed(stubs: tuple[Path, Path]) -> None:
    code, out, calls = _deploy(stubs, IMAGE, FAKE_CONTEXT="prod-cluster", FAKE_VERIFY="1")
    assert code == 0, out
    assert f"verify-image.sh {IMAGE}" in calls  # the platform's gate, on this exact digest
    assert "helm upgrade --install shortly" in calls
    assert f"image.digest=sha256:{DIGEST}" in calls


def test_digest_the_gate_refuses_is_not_deployed(stubs: tuple[Path, Path]) -> None:
    code, out, calls = _deploy(stubs, IMAGE, FAKE_CONTEXT="kind-shortly", FAKE_VERIFY="")
    assert code != 0
    assert "REFUSED: no valid provenance" in out
    assert "helm " not in calls  # nothing was deployed


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
    assert "verify-image.sh" not in calls
    assert "image.tag=dev" in calls


def test_break_glass_skips_verification_loudly(stubs: tuple[Path, Path]) -> None:
    code, out, calls = _deploy(
        stubs, IMAGE, FAKE_CONTEXT="prod-cluster", FAKE_VERIFY="", INSECURE_SKIP_VERIFY="1"
    )
    assert code == 0, out
    assert "WARNING: provenance verification SKIPPED" in out
    assert "verify-image.sh" not in calls
    assert "helm upgrade" in calls


# ------------------------------------------------------------------- real registry


def _documented_release() -> str:
    """The digest the runbook's Quick reference names as the current release."""
    runbook = (ROOT / "docs/RUNBOOK.md").read_text()
    match = re.search(r"^\| Current release \| v[\d.]+, `(sha256:[0-9a-f]{64})`", runbook, re.M)
    assert match, "RUNBOOK Quick reference has no 'Current release' digest"
    return f"ghcr.io/statsguy/shortly@{match[1]}"


@pytest.mark.integration
def test_documented_release_verifies(locked_verify_image: Path) -> None:
    result = subprocess.run(
        [str(locked_verify_image), _documented_release()], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "refs/tags/v" in result.stdout
