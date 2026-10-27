"""Environments and their namespaces (platform plan, Phase 2: a namespace per app and env).

scripts/lib/namespaces.sh is the one place that says which namespace an environment runs in;
every script asks it instead of writing `-n prod`, so the switch to <app>-<env> namespaces
is one setting.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "scripts/lib/namespaces.sh"


def _mapping(prefix: str | None) -> str:
    env = {"PATH": "/usr/bin:/bin"} | ({} if prefix is None else {"NAMESPACE_PREFIX": prefix})
    cmd = f'source "{LIB}"; echo "${{ENVS[*]}}|$STAGING_NS|$PROD_NS|$(ns_of prod)"'
    return subprocess.run(
        ["bash", "-c", cmd], env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.mark.parametrize(
    ("prefix", "want"),
    [
        (None, "staging prod|staging|prod|prod"),
        ("shortly-", "staging prod|shortly-staging|shortly-prod|shortly-prod"),
    ],
)
def test_environments_map_to_namespaces(prefix: str | None, want: str) -> None:
    assert _mapping(prefix) == want


def test_scripts_name_no_namespace_directly() -> None:
    """`kubectl -n prod` in a script would miss the switch to <app>-<env> namespaces."""
    literal = re.compile(r"(-n|--namespace)[ =](\"|')?(staging|prod)\b")
    for path in sorted((ROOT / "scripts").glob("*.sh")):
        hits = [line for line in path.read_text().splitlines() if literal.search(line)]
        assert not hits, (path.name, hits)
