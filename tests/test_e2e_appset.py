"""scripts/lib/e2e-appset.py: the platform's ApplicationSet, adjusted for e2e step 12.

The drill runs only in a kind job; this checks the transform itself on the platform's
templatePatch as it is today (apps/applicationset.yaml at platform.lock), so a mistake shows
up in seconds. If the platform changes its templatePatch, the script refuses (tested below)
and this copy is updated with it.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts/lib/e2e-appset.py"
PATCH = (
    "spec:\n  sources:\n    - repoURL: oci://ghcr.io/easypeasyops/charts/service\n"
    "      targetRevision: {{ .chart.digest | quote }} # service {{ .chart.version }}\n"
    "      path: .\n      helm:\n        releaseName: {{ .app | quote }}\n        valueFiles:\n"
    "        {{- range .values.files }}\n"
    '          - {{ printf "$values/%s" . | quote }}\n'
    "        {{- end }}\n    - repoURL: {{ .values.repoURL | quote }}\n"
    "      targetRevision: {{ .values.revision | quote }}\n      ref: values\n"
)
LOCK = "e" * 40


def run(patch: str) -> subprocess.CompletedProcess[str]:
    appset = {
        "spec": {
            "generators": [
                {"git": {"revision": "main", "files": [{"path": "apps/*/envs/*.yaml"}]}}
            ],
            "syncPolicy": {"preserveResourcesOnDeletion": True},
            "templatePatch": patch,
        }
    }
    args = [LOCK, "c0ffee", "https://github.com/easypeasyops/platform.git", "shortly", "e2e"]
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=json.dumps(appset),
        capture_output=True,
        text=True,
    )


def test_prod_only_at_the_lock_and_create_only() -> None:
    spec = json.loads(run(PATCH).stdout)["spec"]
    assert spec["generators"][0]["git"] == {
        "revision": LOCK,
        "files": [{"path": "apps/shortly/envs/prod.yaml"}],
    }
    assert spec["syncPolicy"] == {
        "preserveResourcesOnDeletion": True,
        "applicationsSync": "create-only",
    }


def test_values_at_the_commit_under_test_then_ci_values() -> None:
    patch = json.loads(run(PATCH).stdout)["spec"]["templatePatch"]
    assert 'targetRevision: "c0ffee"\n      ref: values' in patch
    assert "{{ .values.revision" not in patch
    assert (
        '{{- end }}\n          - "$platform/deploy/kind/e2e-values.yaml"\n        valuesObject: '
        in patch
    )
    assert '"image": {"repository": "shortly", "tag": "e2e", "digest": ""}' in patch
    assert patch.endswith(f'targetRevision: "{LOCK}"\n      ref: platform\n')


def test_refuses_a_changed_template() -> None:
    result = run(PATCH.replace("{{- end }}", "{{ end }}"))
    assert result.returncode != 0
    assert "templatePatch changed" in result.stderr
