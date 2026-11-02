"""shortly's values for the platform's service chart (deploy/service/) carry its alert rules.

The chart takes rules as text in values, but promtool (tests/test_alert_rules.py) and the
local Prometheus (compose.yaml) need rule files: deploy/prometheus/rules/ holds them, and the
values must carry exactly the same text, or what CI tests isn't what Argo CD deploys.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
RULES = ROOT / "deploy/prometheus/rules"
SERVICE = ROOT / "deploy/service"


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text())
    return data


@pytest.fixture(scope="module")
def service() -> dict[str, Any]:
    return load(SERVICE / "values.yaml")


def test_cause_alerts_are_the_rule_file(service: dict[str, Any]) -> None:
    assert service["monitoring"]["rules"] == {"shortly": (RULES / "shortly.yaml").read_text()}


def test_slo_rules_are_the_generated_rule_file() -> None:
    # rules/slo.yaml is kept current by tests/test_alert_rules.py; the values file holds the
    # same generated text (the platform's gen_slo_rules.py --values), so it is current too.
    generated = load(SERVICE / "slo-rules.values.yaml")
    assert generated == {"monitoring": {"rules": {"slo": (RULES / "slo.yaml").read_text()}}}


def test_slo_dashboard_links_the_slo_runbook(service: dict[str, Any]) -> None:
    spec = load(ROOT / "deploy/prometheus/slos.yaml")
    assert service["monitoring"]["sloDashboard"] == {"runbookUrl": spec["runbook_url"]}
