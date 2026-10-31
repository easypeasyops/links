"""Environments and their namespaces (platform plan, Phase 2: a namespace per app and env).

The platform's scripts/lib/namespaces.sh (easypeasyops/platform, which tests the mapping) is
the one place that says which namespace an environment runs in; this repo's scripts ask it
(through scripts/lib/platform.sh) instead of writing `-n prod`.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_scripts_name_no_namespace_directly() -> None:
    """`kubectl -n prod` in a script would miss the switch to <app>-<env> namespaces."""
    literal = re.compile(r"(-n|--namespace)[ =](\"|')?(staging|prod)\b")
    for path in sorted((ROOT / "scripts").glob("*.sh")):
        hits = [line for line in path.read_text().splitlines() if literal.search(line)]
        assert not hits, (path.name, hits)
