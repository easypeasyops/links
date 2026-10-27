"""shortly's values for the platform's service chart (deploy/service/) match links' own chart.

Platform plan 3.3: Argo CD will render shortly from the platform's service chart with
deploy/service/*.yaml, instead of deploy/helm/shortly. Until that chart is deleted (3.4),
both exist, and a change to one must reach the other. These tests fail when they disagree,
naming the piece; the platform's golden renders prove the two charts render alike for the
same values (easypeasyops/platform charts/service/tests/shortly).
"""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CHART = ROOT / "deploy/helm/shortly"
SERVICE = ROOT / "deploy/service"


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text())
    return data


@pytest.fixture(scope="module")
def service() -> dict[str, Any]:
    return load(SERVICE / "values.yaml")


@pytest.fixture(scope="module")
def chart() -> dict[str, Any]:
    return load(CHART / "values.yaml")


# Whole sections that are shortly's alone: the service chart's defaults are generic, so
# deploy/service/values.yaml must carry each of these exactly as links' chart sets them.
SECTIONS = ["app", "config", "env", "autoscaling", "resources", "cronJobs"]


@pytest.mark.parametrize("section", SECTIONS)
def test_app_settings_match_the_chart(
    section: str, service: dict[str, Any], chart: dict[str, Any]
) -> None:
    assert service[section] == chart[section], f"deploy/service/values.yaml {section}"


def test_image_database_and_migrations_match_the_chart(
    service: dict[str, Any], chart: dict[str, Any]
) -> None:
    assert service["image"] == {"repository": chart["image"]["repository"]}
    # The secret's key is the service chart's default ("url"); the secret's name is shortly's.
    assert {"secretKey": "url", **service["database"]} == chart["database"]
    assert service["migrations"]["enabled"] is True
    assert service["migrations"]["command"] == chart["migrations"]["command"]


def test_alert_rules_are_the_charts_rule_files(service: dict[str, Any]) -> None:
    assert service["monitoring"]["rules"] == {"shortly": (CHART / "rules/shortly.yaml").read_text()}
    generated = load(SERVICE / "slo-rules.values.yaml")
    # rules/slo.yaml is kept current by tests/test_alert_rules.py; the values file holds the
    # same generated text, so it is current too.
    assert generated == {"monitoring": {"rules": {"slo": (CHART / "rules/slo.yaml").read_text()}}}


def test_canary_matches_the_chart(service: dict[str, Any], chart: dict[str, Any]) -> None:
    analysis = service["canary"]["analysis"]
    assert analysis["syntheticTraffic"] == {
        "script": (CHART / "files/canary-traffic.js").read_text()
    }
    assert analysis["latencyChecks"] == [
        {
            "name": "redirect-p99",
            "selector": 'method="GET", route="/{code}"',
            "maxP99Seconds": chart["canary"]["analysis"]["maxRedirectP99Seconds"],
        }
    ]


def test_causes_dashboard_holds_the_charts_own_metric_panels(service: dict[str, Any]) -> None:
    """The service chart ships the SLO dashboard; shortly keeps the two panels on its own
    metrics in a Causes dashboard (platform #9). They must stay the chart's panels."""

    def comparable(panel: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in panel.items() if k not in ("id", "gridPos")}

    ours = json.loads(service["monitoring"]["dashboards"]["causes.json"])["panels"]
    theirs = json.loads((CHART / "dashboards/slo.json").read_text())["panels"]
    titles = [p["title"] for p in ours]
    assert titles == ["DB pool in use", "Load shed"]
    assert [comparable(p) for p in ours] == [
        comparable(p) for t in titles for p in theirs if p.get("title") == t
    ]


def test_slo_dashboard_links_the_slo_runbook(service: dict[str, Any]) -> None:
    spec = load(ROOT / "deploy/prometheus/slos.yaml")
    assert service["monitoring"]["sloDashboard"] == {"runbookUrl": spec["runbook_url"]}
