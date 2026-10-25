"""The platform under Argo CD (deploy/argocd/platform/, DESIGN: GitOps with Argo CD).

The vendored metrics-server manifest must be byte for byte the release that was pinned.
"""

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_vendored_metrics_server_is_the_pinned_release() -> None:
    folder = ROOT / "deploy/platform/metrics-server"
    want, name = (folder / "SHA256SUMS").read_text().split()
    assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == want
