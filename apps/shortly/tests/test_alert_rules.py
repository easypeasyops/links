"""Guards for the Prometheus rules in deploy/prometheus/.

An alert that never fires looks exactly like a healthy system, so each way a rule can
silently break has a test:

- the generated SLO rules are stale vs deploy/prometheus/slos.yaml;
- a rule is invalid, or doesn't fire / stay quiet as intended (promtool unit tests);
- a selector matches nothing the app exports (a renamed metric, route, label or bucket);
- a runbook link points at a section that doesn't exist;
- a dashboard query selects nothing the app exports.
The platform's own alerts (Argo CD) and Alertmanager's routing are tested in
easypeasyops/platform (tests/test_alerting.py).
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from shortly.api import create_app
from shortly.observability import Metrics
from shortly.service import Shortener
from shortly.store import InMemoryStore, StoreBusyError
from tests.conftest import FakeClock, scripted_codes

ROOT = Path(__file__).resolve().parent.parent
PROM = ROOT / "deploy/prometheus"
RULES = PROM / "rules"  # deploy/service/ ships the same text (tests/test_service_values.py)
RULE_FILES = sorted(RULES.glob("*.yaml"))
# Metrics that come from outside the app: Prometheus itself and kube-state-metrics.
EXTERNAL = {
    "up",
    "kube_horizontalpodautoscaler_status_current_replicas",
    "kube_horizontalpodautoscaler_spec_max_replicas",
    "kube_replicaset_status_ready_replicas",
    "kube_cronjob_status_last_successful_time",
    "kube_cronjob_created",
    "ALERTS",
}

# name{matchers}; quoted values may contain braces themselves, as in route="/{code}".
SELECTOR = re.compile(r'([a-zA-Z_][a-zA-Z0-9_:]*)\{((?:[^"}]|"(?:[^"\\]|\\.)*")*)\}')
MATCHER = re.compile(r'(\w+)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"')


def _rules() -> list[dict[str, Any]]:
    return [
        rule
        for path in RULE_FILES
        for group in yaml.safe_load(path.read_text())["groups"]
        for rule in group["rules"]
    ]


Samples = list[tuple[str, dict[str, str]]]
# Labels Prometheus attaches when it scrapes; the app itself never exports them.
TARGET_LABELS = {"namespace", "job", "instance", "pod", "service", "endpoint", "container"}


def _matches(labels: dict[str, str], matchers: str) -> bool:
    found = MATCHER.findall(matchers)
    assert len(found) == matchers.count('"') // 2, f"unparsed matchers: {matchers}"
    for name, op, value in found:
        if name in TARGET_LABELS:
            continue
        actual = labels.get(name, "")
        if op == "=" and actual != value:
            return False
        if op == "!=" and actual == value:
            return False
        if op == "=~" and not re.fullmatch(value, actual):  # PromQL regexes are anchored
            return False
        if op == "!~" and re.fullmatch(value, actual):
            return False
    return True


def _exercised_metrics(clock: FakeClock) -> Samples:
    """Drive every outcome the rules look for through real apps sharing one registry."""

    class BusyStore(InMemoryStore):
        def get(self, code: str) -> None:
            raise StoreBusyError("pool exhausted")

    class DownStore(InMemoryStore):
        def ping(self) -> bool:
            return False

    metrics = Metrics()

    def client(shortener: Shortener) -> TestClient:
        app = create_app(shortener, base_url="https://sho.rt", metrics=metrics)
        return TestClient(app, follow_redirects=False)

    ok = client(Shortener(InMemoryStore(), clock=clock))
    code = ok.post("/links", json={"url": "https://example.com/"}).json()["code"]
    assert ok.get(f"/{code}").status_code == 307
    assert ok.get("/nope").status_code == 404
    assert ok.get("/readyz").status_code == 200
    assert client(Shortener(BusyStore(), clock=clock)).get("/x").status_code == 503
    assert client(Shortener(DownStore(), clock=clock)).get("/readyz").status_code == 503
    full = client(
        Shortener(InMemoryStore(), code_gen=scripted_codes("a", "a"), clock=clock, max_attempts=1)
    )
    full.post("/links", json={"url": "https://a.example/"})
    assert full.post("/links", json={"url": "https://b.example/"}).status_code == 503
    return [(s.name, s.labels) for m in metrics.registry.collect() for s in m.samples]


def test_generated_slo_rules_are_current() -> None:
    spec = importlib.util.spec_from_file_location("gen", ROOT / "scripts/gen_slo_rules.py")
    assert spec is not None
    assert spec.loader is not None
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    assert (RULES / "slo.yaml").read_text() == gen.render(), (
        "deploy/prometheus/rules/slo.yaml is stale: run `uv run python scripts/gen_slo_rules.py`"
    )


def _check_selectors(where: str, expr: str, samples: Samples, recorded: set[str]) -> int:
    checked = 0
    for name, matchers in SELECTOR.findall(expr):
        if ":" in name:  # a recording rule: must be defined in the rule files
            assert name in recorded, f"{where}: {name} is not a recorded series"
        elif name not in EXTERNAL:
            checked += 1
            assert any(n == name and _matches(labels, matchers) for n, labels in samples), (
                f"{where}: {name}{{{matchers}}} matches nothing the app exports"
            )
    return checked


def test_every_rule_selector_matches_something_the_app_exports(clock: FakeClock) -> None:
    samples, rules = _exercised_metrics(clock), _rules()
    recorded = {r["record"] for r in rules if "record" in r}
    checked = sum(
        _check_selectors(r.get("alert") or r["record"], r["expr"], samples, recorded) for r in rules
    )
    assert checked > 20  # the parser really found the selectors


def test_every_dashboard_query_matches_something_the_app_exports(clock: FakeClock) -> None:
    samples = _exercised_metrics(clock)
    recorded = {r["record"] for r in _rules() if "record" in r}
    checked = 0
    # shortly's own dashboards; the SLO dashboard is the platform chart's, over recorded series.
    values = yaml.safe_load((ROOT / "deploy/service/values.yaml").read_text())
    for name, text in values["monitoring"]["dashboards"].items():
        for panel in json.loads(text)["panels"]:
            for target in panel.get("targets", []):
                where = f"{name}: {panel['title']}"
                checked += _check_selectors(where, target["expr"], samples, recorded)
    assert checked >= 2  # pool in use, load shed


def test_runbook_links_point_at_real_sections() -> None:
    def slug(heading: str) -> str:  # GitHub's anchor rule
        return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")

    runbook = (ROOT / "docs/RUNBOOK.md").read_text()
    anchors = {slug(h) for h in re.findall(r"^#{2,4} (.+)$", runbook, re.MULTILINE)}
    links = {r["annotations"]["runbook_url"] for r in _rules() if "alert" in r}
    for link in links:
        assert link.startswith("https://github.com/easypeasyops/links/blob/main/apps/shortly/docs/RUNBOOK.md#")
        assert link.split("#", 1)[1] in anchors, f"no RUNBOOK section for {link}"


def _docker() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.mark.integration
def test_promtool_unit_tests_pass() -> None:
    if not _docker():
        if os.environ.get("CI"):
            pytest.fail("Docker is required for the promtool tests in CI")
        pytest.skip("Docker not available")
    # The same Prometheus image as compose.yaml, so the rules are tested where they run.
    image = re.search(r"image: (prom/prometheus:\S+)", (ROOT / "compose.yaml").read_text())
    assert image is not None
    result = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{ROOT / 'deploy'}:/d:ro", "--entrypoint", "promtool",
         image[1], "test", "rules",
         *[f"/d/prometheus/tests/{p.name}" for p in sorted((PROM / "tests").glob("*.yaml"))]],
        capture_output=True,
        text=True,
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr


def test_slo_generator_serves_any_app(tmp_path: Path) -> None:
    """Platform plan 1.4: the spec, the output and the runbook link are the app's inputs."""
    spec = tmp_path / "slos.yaml"
    spec.write_text(
        "window: 30d\n"
        "runbook_url: https://example.test/pager/RUNBOOK.md#slo\n"
        "slos:\n"
        "  - name: ack-availability\n"
        "    description: POST /ack answers without a server error\n"
        "    objective: 0.999\n"
        '    sli: { type: availability, selector: \'method="POST", route="/ack"\' }\n'
    )
    out = tmp_path / "chart/rules/slo.yaml"
    script = [sys.executable, str(ROOT / "scripts/gen_slo_rules.py"), "--spec", str(spec)]
    subprocess.run([*script, "--out", str(out)], check=True, capture_output=True)
    rules = yaml.safe_load(out.read_text())
    alerts = [r for g in rules["groups"] for r in g["rules"] if "alert" in r]
    assert alerts
    assert {a["annotations"]["runbook_url"] for a in alerts} == {
        "https://example.test/pager/RUNBOOK.md#slo"
    }
    assert f"from {spec.resolve()}" in out.read_text().splitlines()[0]
    check = subprocess.run([*script, "--out", str(out), "--check"], capture_output=True)
    assert check.returncode == 0
