"""The platform's ApplicationSet, adjusted for CI's GitOps drill (scripts/k8s-e2e.sh, step 12).

    kubectl create --dry-run=client -o json -f <platform>/apps/applicationset.yaml \\
      | python3 scripts/lib/e2e-appset.py <lock> <revision> <platform repo> <image repo> <tag>

Reads the ApplicationSet as JSON on stdin, prints it changed in exactly these ways:
  - the generator reads only shortly's prod env file, at the locked platform commit;
  - the values repo is read at <revision> (the commit under test) instead of the env file's;
  - the platform's deploy/kind/e2e-values.yaml is layered after the app's values files (CI
    clocks, as deploy.sh), then the local build and a lower HPA ceiling as valuesObject;
  - applicationsSync: create-only, so an edit to the generated Application sticks.
Standard library only: the CI runner's python has no YAML module.
"""

import json
import sys

VALUES_REVISION = "targetRevision: {{ .values.revision | quote }}"
VALUE_FILES_END = "        {{- end }}\n"


def adjust(appset: dict, lock: str, revision: str, platform_repo: str, repo: str, tag: str) -> dict:
    spec = appset["spec"]
    git = spec["generators"][0]["git"]
    git["revision"], git["files"] = lock, [{"path": "apps/shortly/envs/prod.yaml"}]
    spec["syncPolicy"]["applicationsSync"] = "create-only"

    patch = spec["templatePatch"]
    for anchor in (VALUES_REVISION, VALUE_FILES_END):
        if patch.count(anchor) != 1:
            sys.exit(f"e2e-appset: the platform's templatePatch changed, no single {anchor!r}")
    values_object = {
        "image": {"repository": repo, "tag": tag, "digest": ""},
        # The HPA drill (step 6) left prod at 4 pods, and the HPA keeps them for 5 minutes. A
        # canary needs room for one more pod, which the CI node then lacks; a lower ceiling
        # takes effect at once and doesn't touch the pod template.
        "autoscaling": {"maxReplicas": 2},
    }
    patch = patch.replace(VALUES_REVISION, f"targetRevision: {json.dumps(revision)}")
    patch = patch.replace(
        VALUE_FILES_END,
        VALUE_FILES_END
        + '          - "$platform/deploy/kind/e2e-values.yaml"\n'
        + f"        valuesObject: {json.dumps(values_object)}\n",
    )
    patch = patch.rstrip("\n") + (
        f"\n    - repoURL: {json.dumps(platform_repo)}\n"
        f"      targetRevision: {json.dumps(lock)}\n"
        "      ref: platform\n"
    )
    spec["templatePatch"] = patch
    return appset


if __name__ == "__main__":
    print(json.dumps(adjust(json.load(sys.stdin), *sys.argv[1:6])))
