"""The single required check (docs/DESIGN.md, Job `k8s`; RUNBOOK: CI failures).

Ruleset main-rules requires one check, `ci`, from the gate job. A job the gate doesn't
`need` can fail without blocking a merge, so the gate must need every job that does work.
"""

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT.parents[1] / ".github/workflows/ci.yml"  # the repo root: shortly is apps/shortly/
# Jobs that only report others' results.
REPORTERS = {"gate"}


def _jobs() -> dict[str, Any]:
    jobs: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    return jobs


def test_the_gate_needs_every_job_that_does_work() -> None:
    jobs = _jobs()
    assert set(jobs["gate"]["needs"]) == set(jobs) - REPORTERS


def test_the_gate_reports_even_when_a_job_failed_or_was_skipped() -> None:
    gate = _jobs()["gate"]
    assert gate["name"] == "ci"  # the context the ruleset requires
    assert gate["if"] == "always()"  # without it, a failed need skips the gate: no check
    assert 'all(.[]; .result == "success")' in gate["steps"][-1]["run"]


def test_ci_preloads_the_k6_image_the_cluster_runs() -> None:
    """CI loads k6 into kind ahead of time (a Docker Hub pull from the node timed out once
    and aborted a healthy canary); every k6 the cluster runs must be that same image: the
    platform chart's (k6_image in scripts/lib/platform.sh, tested in test_platform_lock.py)."""
    assert 'K6_IMAGE="${K6_IMAGE:-$(k6_image)}"' in (ROOT / "scripts/hpa-drill.sh").read_text()
    steps = [s for s in _jobs()["k8s"]["steps"] if s.get("name") == "Preload k6 into kind"]
    assert len(steps) == 1
    assert "k6_image" in steps[0]["run"]
    assert "kind load docker-image" in steps[0]["run"]
