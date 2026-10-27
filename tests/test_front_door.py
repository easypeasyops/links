"""The kind front door (deploy/kind/gateway.yaml, deploy/kind/tools/, scripts/cluster.sh).

The users' listener and the dev tools' listener must stay apart, and the laptop ports in
scripts/cluster.sh must forward to the node ports the Gateway's Service actually pins.
"""

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
# The app's listener admits the environment-named and the per-app namespaces while the
# environments move (platform plan, Phase 2).
APP_NAMESPACES = {"staging", "prod", "shortly-staging", "shortly-prod"}
LISTENER_NAMESPACES = {"http": APP_NAMESPACES, "tools": {"argocd", "monitoring"}}


def _docs(path: Path) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def _gateway_docs() -> dict[str, dict[str, Any]]:
    return {d["kind"]: d for d in _docs(ROOT / "deploy/kind/gateway.yaml")}


def test_each_listener_admits_only_its_namespaces() -> None:
    listeners = _gateway_docs()["Gateway"]["spec"]["listeners"]
    assert {listener["name"] for listener in listeners} == set(LISTENER_NAMESPACES)
    for listener in listeners:
        (expr,) = listener["allowedRoutes"]["namespaces"]["selector"]["matchExpressions"]
        assert expr["key"] == "kubernetes.io/metadata.name"
        assert expr["operator"] == "In"
        assert set(expr["values"]) == LISTENER_NAMESPACES[listener["name"]]


def test_tool_routes_attach_to_the_tools_listener_only() -> None:
    routes = [d for f in sorted((ROOT / "deploy/kind/tools").glob("*.yaml")) for d in _docs(f)]
    assert {r["metadata"]["name"] for r in routes} == {
        "argocd",
        "grafana",
        "prometheus",
        "alertmanager",
    }
    for route in routes:
        assert route["metadata"]["namespace"] in LISTENER_NAMESPACES["tools"]
        assert route["spec"]["parentRefs"] == [
            {"name": "shortly", "namespace": "gateway", "sectionName": "tools"}
        ]
        assert route["spec"]["hostnames"] == [f"{route['metadata']['name']}.localhost"]


def test_front_door_ports_match_the_pinned_node_ports() -> None:
    listeners = {
        listener["port"]: listener["name"]
        for listener in _gateway_docs()["Gateway"]["spec"]["listeners"]
    }
    service = _gateway_docs()["EnvoyProxy"]["spec"]["provider"]["kubernetes"]["envoyService"]
    assert service["type"] == "NodePort"
    pinned = {p["nodePort"]: p["port"] for p in service["patch"]["value"]["spec"]["ports"]}
    assert set(pinned.values()) == set(listeners), "every listener needs a pinned node port"

    script = (ROOT / "scripts/cluster.sh").read_text()
    match = re.search(r"^FRONT_DOOR_PORTS=\(([^)]*)\)", script, re.M)
    assert match, "scripts/cluster.sh has no FRONT_DOOR_PORTS"
    forwarded = {int(host): int(node) for host, node in re.findall(r'"(\d+):(\d+)"', match[1])}
    assert set(forwarded.values()) == set(pinned), "a forwarder points at an unpinned port"
    # The docs and the Argo CD / Grafana settings use 8080 for users, 8090 for the tools.
    by_listener = {listeners[pinned[node]]: host for host, node in forwarded.items()}
    assert by_listener == {"http": 8080, "tools": 8090}
