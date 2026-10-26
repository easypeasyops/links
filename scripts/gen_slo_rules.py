"""Generate Prometheus SLO rules from an app's SLO spec (shortly's: deploy/prometheus/slos.yaml).

    uv run python scripts/gen_slo_rules.py           # (re)write deploy/helm/shortly/rules/slo.yaml
    uv run python scripts/gen_slo_rules.py --check   # exit 1 if the committed file is stale
    uv run python scripts/gen_slo_rules.py --spec <app>/slos.yaml --out <chart>/rules/slo.yaml

Nothing here is shortly's: the spec names the SLIs, the objectives and the runbook the alerts
link to (`runbook_url`), so any app with the platform's HTTP metrics can use it.

Alerting method: multi-window, multi-burn-rate (Google SRE Workbook, "Alerting on SLOs").
Burn rate = how fast the error budget is being spent; 1x spends exactly the budget over the
window. Each alert needs BOTH a long window (is this significant?) and a short window (is it
still happening?), so it fires fast on a real incident and stops soon after recovery.

Each alert also needs MIN_BAD_EVENTS bad events in its long window. A ratio alone can't tell
1 slow request out of 3 (a quiet hour, 33%) from 300 out of 900: on low traffic it pages on
a single event. (Workbook, "Low-traffic services and error budget alerting".)
"""

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
# shortly's spec, and the rules file its chart ships (PrometheusRule): the defaults.
SPEC = ROOT / "deploy/prometheus/slos.yaml"
OUT = ROOT / "deploy/helm/shortly/rules/slo.yaml"

WINDOWS = ["5m", "30m", "1h", "2h", "6h", "1d", "3d", "30d"]

# (severity, [(long window, short window, burn rate)], for). For a 30-day budget:
#   14.4x for 1h spends 2% of it, 6x for 6h spends 5%   -> wake someone up
#   3x for 1d spends 10%, 1x for 3d spends 10%          -> a ticket for working hours
BURN_ALERTS: list[tuple[str, list[tuple[str, str, float]], str]] = [
    ("page", [("1h", "5m", 14.4), ("6h", "30m", 6.0)], "2m"),
    ("ticket", [("1d", "2h", 3.0), ("3d", "6h", 1.0)], "15m"),
]

PACE = {"page": "burning fast", "ticket": "burning steadily"}

# Below this many bad events in the long window, no burn-rate alert fires, whatever the ratio.
# Small enough that a real incident on a quiet service still alerts (5 failed requests in an
# hour); large enough that one or two unlucky requests don't wake anyone.
MIN_BAD_EVENTS = 5
LONG_WINDOWS = sorted(
    {long for _, pairs, _ in BURN_ALERTS for long, _, _ in pairs}, key=WINDOWS.index
)

# Per-namespace, so staging and prod scraped by one Prometheus get separate budgets. Without a
# namespace label (local compose) the result is one series with no namespace.
BY = "sum by (namespace)"


def error_ratio(sli: dict[str, Any], window: str) -> str:
    sel = sli["selector"]
    if sli["type"] == "availability":
        bad = f'{BY} (rate(http_requests_total{{{sel}, status=~"5.."}}[{window}]))'
        total = f"{BY} (rate(http_requests_total{{{sel}}}[{window}]))"
        # No 5xx series at all means 0 errors, not "no data": `or 0 * total` fills those
        # namespaces with 0. (1 - good/total would hide a namespace with 100% errors: it has
        # no "good" series, so the division returns nothing. A promtool test covers it.)
        return f"({bad} or 0 * {total}) / {total}"
    if sli["type"] == "latency":
        le = repr(float(sli["threshold_seconds"]))  # prometheus_client's bucket label format
        # Every histogram series exports every bucket, so good/total is always defined.
        good = f'{BY} (rate(http_request_duration_seconds_bucket{{{sel}, le="{le}"}}[{window}]))'
        total = f"{BY} (rate(http_request_duration_seconds_count{{{sel}}}[{window}]))"
        return f"1 - ({good} / {total})"
    raise ValueError(f"unknown SLI type {sli['type']!r}")


def events(sli: dict[str, Any], window: str) -> str:
    """Valid events (the SLI's denominator) in the window, as a count."""
    metric = {
        "availability": "http_requests_total",
        "latency": "http_request_duration_seconds_count",
    }[sli["type"]]
    return f"{BY} (increase({metric}{{{sli['selector']}}}[{window}]))"


def days(pairs: list[tuple[str, str, float]], pick: Any) -> str:
    """Days until a 30-day budget is gone at the fastest (max) or slowest (min) burn rate."""
    return f"{30 / pick(rate for _, _, rate in pairs):.0f}"


def camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("-"))


def fmt(x: float) -> str:
    return f"{x:.6g}"


def build(spec: dict[str, Any]) -> dict[str, Any]:
    if spec["window"] != "30d":
        raise ValueError("burn-rate factors assume a 30d window")
    groups = []
    for slo in spec["slos"]:
        name, objective = slo["name"], float(slo["objective"])
        budget = 1 - objective

        def labels(slo_name: str = name) -> dict[str, str]:
            return {"slo": slo_name}  # a fresh dict per rule: no YAML anchors in the output

        rules: list[dict[str, Any]] = [
            {"record": "slo:objective:ratio", "expr": fmt(objective), "labels": labels()}
        ]
        rules += [
            {
                "record": f"slo:sli_error:ratio_rate{w}",
                "expr": error_ratio(slo["sli"], w),
                "labels": labels(),
            }
            for w in WINDOWS
        ]
        rules += [
            {
                "record": f"slo:sli_events:increase{w}",
                "expr": events(slo["sli"], w),
                "labels": labels(),
            }
            for w in LONG_WINDOWS
        ]
        rules.append(
            {
                "record": "slo:error_budget_remaining:ratio",
                "expr": f'1 - slo:sli_error:ratio_rate30d{{slo="{name}"}} / {fmt(budget)}',
                "labels": labels(),
            }
        )
        for severity, pairs, for_ in BURN_ALERTS:
            sel = f'{{slo="{name}"}}'
            conditions = [
                f"(slo:sli_error:ratio_rate{long}{sel} > {fmt(rate * budget)}"
                f" and slo:sli_error:ratio_rate{short}{sel} > {fmt(rate * budget)}"
                f" and slo:sli_error:ratio_rate{long}{sel} * slo:sli_events:increase{long}{sel}"
                f" >= {MIN_BAD_EVENTS})"
                for long, short, rate in pairs
            ]
            windows = " or ".join(f"{rate:g}x over {long}" for long, _, rate in pairs)
            rules.append(
                {
                    "alert": f"{camel(name)}BudgetBurn",
                    "expr": "\n".join(c if i == 0 else f"or {c}" for i, c in enumerate(conditions)),
                    "for": for_,
                    "labels": {"severity": severity, "slo": name},
                    "annotations": {
                        "summary": f"{name}: error budget {PACE[severity]} ({windows})",
                        "description": (
                            f"{slo['description']}: objective {objective:.4g} over 30 days. "
                            f"At this pace the whole budget lasts only "
                            f"{days(pairs, max)} to {days(pairs, min)} days."
                        ),
                        "runbook_url": spec["runbook_url"],
                    },
                }
            )
        groups.append({"name": f"slo-{name}", "rules": rules})
    return {"groups": groups}


class _Dumper(yaml.SafeDumper):
    """Multi-line strings (alert expressions) as readable `|` blocks."""


def _str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _str)


def _shown(path: Path) -> str:
    """A path as the header shows it: relative to the repository when it is inside."""
    path = path.resolve()
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def render(spec_path: Path = SPEC) -> str:
    spec = yaml.safe_load(spec_path.read_text())
    header = (
        f"# GENERATED by scripts/gen_slo_rules.py from {_shown(spec_path)}. Do not edit:\n"
        "# change the spec and re-run the script (a test fails if this file is stale).\n"
    )
    body = yaml.dump(build(spec), Dumper=_Dumper, sort_keys=False, width=200, allow_unicode=True)
    return header + body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the output is stale")
    parser.add_argument("--spec", type=Path, default=SPEC, help="the app's SLO spec")
    parser.add_argument("--out", type=Path, default=OUT, help="the rules file to write")
    args = parser.parse_args()
    text = render(args.spec)
    out: Path = args.out
    if args.check:
        if not out.exists() or out.read_text() != text:
            print(f"{_shown(out)} is stale: run scripts/gen_slo_rules.py", file=sys.stderr)
            return 1
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(f"wrote {_shown(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
