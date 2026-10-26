"""Golden manifests for the shortly chart (docs/DESIGN.md, Gate-by-gate: job `check`).

Argo CD applies what `helm template` renders, so the rendered manifests are the chart's
behaviour. They are checked in under tests/golden/, one file per environment:
  - a change to a template, the rules, the dashboards or an env file's settings shows up
    in review as a diff of what the cluster will receive
  - a refactor (the platform plan's Phase 1) must leave them byte for byte the same

Two things are normalised, so routine PRs leave the files alone:
  - the image digest, which every promotion PR changes: rendered as a fixed placeholder
  - the chart version in the helm.sh/chart label, which release PRs bump

After an intended change:  UPDATE_GOLDEN=1 uv run pytest tests/test_chart_golden.py
"""

import difflib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CHART = ROOT / "deploy/helm/shortly"
GOLDEN = ROOT / "tests/golden"
ENVS = sorted(p.stem for p in (ROOT / "deploy/envs").glob("*.yaml"))
PLACEHOLDER_DIGEST = "sha256:" + "0" * 64
CHART_LABEL = re.compile(r"(helm\.sh/chart: shortly-)\S+")


def render(env: str, *sets: str) -> str:
    """The manifests Argo CD renders for `env`, normalised (module docstring)."""
    if shutil.which("helm") is None:
        if os.environ.get("CI"):
            pytest.fail("helm is required in CI")
        pytest.skip("helm not installed")
    out = subprocess.run(
        [
            "helm", "template", "shortly", str(CHART),
            "--namespace", env,
            "--values", str(ROOT / f"deploy/envs/{env}.yaml"),
            "--set", f"image.digest={PLACEHOLDER_DIGEST}",
            # What the cluster offers, so the monitoring objects render as they do there.
            "--kube-version", "1.37.0",
            "--api-versions", "monitoring.coreos.com/v1/ServiceMonitor",
            "--api-versions", "monitoring.coreos.com/v1/PrometheusRule",
            *(arg for s in sets for arg in ("--set", s)),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout  # fmt: skip
    return CHART_LABEL.sub(r"\g<1><version>", out)


def test_every_environment_has_a_golden_file() -> None:
    assert ENVS == ["prod", "staging"]
    assert sorted(p.stem for p in GOLDEN.glob("*.yaml")) == ENVS


@pytest.mark.parametrize("env", ENVS)
def test_rendered_manifests_match_the_golden_file(env: str) -> None:
    golden = GOLDEN / f"{env}.yaml"
    rendered = render(env)
    if os.environ.get("UPDATE_GOLDEN"):
        golden.write_text(rendered)
    expected = golden.read_text()
    if rendered != expected:
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                rendered.splitlines(keepends=True),
                f"tests/golden/{env}.yaml",
                "helm template",
            )
        )
        pytest.fail(
            f"the {env} manifests changed. If intended, regenerate and commit the diff:\n"
            f"  UPDATE_GOLDEN=1 uv run pytest tests/test_chart_golden.py\n\n{diff[:6000]}"
        )


def test_normalisation_hides_only_the_digest_and_chart_version() -> None:
    rendered = render("prod")
    assert PLACEHOLDER_DIGEST in rendered
    assert "sha256:" not in rendered.replace(PLACEHOLDER_DIGEST, "")
    assert "helm.sh/chart: shortly-<version>" in rendered


def _app_env(manifests: str) -> dict[str, str]:
    """The app container's env in the prod Rollout, as name -> value."""
    (rollout,) = (d for d in yaml.safe_load_all(manifests) if d and d["kind"] == "Rollout")
    (app,) = rollout["spec"]["template"]["spec"]["containers"]
    return {e["name"]: e.get("value", "<secret>") for e in app["env"]}


def test_env_entries_with_when_render_only_when_switched_on() -> None:
    """The drill-only and tracing-only variables (values.yaml `env`, `when`)."""
    default = _app_env(render("prod"))
    assert "SHORTLY_FAULT_ERROR_RATIO" not in default
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in default
    on = _app_env(render("prod", "config.faultErrorRatio=0.5", "otel.endpoint=http://otel:4318"))
    assert on["SHORTLY_FAULT_ERROR_RATIO"] == "0.5"
    assert on["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://otel:4318"
    assert on["OTEL_RESOURCE_ATTRIBUTES"] == "deployment.environment.name=prod"
    assert on["SHORTLY_DATABASE_URL"] == "<secret>"
