# Working on shortly

## Docs are part of the change

`docs/DESIGN.md` (how and why the system works) and `docs/RUNBOOK.md` (how to operate it)
must stay true. Any PR that changes behaviour they describe updates them **in the same PR**:

- API, status codes, config variables, metrics, logs, traces → DESIGN
- CI jobs, release or deploy steps, scripts, chart values, probes, rollback, admission
  policies → both
- A new failure mode, or an incident and its new guard → DESIGN (failure modes /
  issues table) and RUNBOOK (playbook)
- Version, digest or chart version in the headers and Quick reference → RUNBOOK
- Anything on the request path, CPU requests, the HPA or the DB pool → re-run
  `scripts/loadtest.sh` and update DESIGN (Capacity) with the new numbers
- SLOs or alert rules → DESIGN (SLOs and alerting) and RUNBOOK (a playbook section per
  `runbook_url`); edit `deploy/prometheus/slos.yaml` and regenerate, never
  `deploy/prometheus/rules/slo.yaml` or `deploy/service/slo-rules.values.yaml`

They are mirrored as Claude Docs (links in each file's header). When editing with Claude,
update the Claude Docs copy in the same session so the two don't drift; the repo copy is
the one reviewed with the code.

## Conventions

- `uv run pytest` (needs Docker for integration tests); CI enforces `--cov-fail-under=90`.
- Python version lives in `.python-version` and both Dockerfile `FROM` lines; change them together.
- Deploy by digest: `scripts/deploy.sh <env> ghcr.io/statsguy/shortly@sha256:…`.
- The platform (chart, Argo CD root, kind, policies, `cluster.sh`) lives in
  easypeasyops/platform; this repo runs it at the commit `platform.lock` names, from a
  checkout beside this one (`../platform`, or `PLATFORM_DIR`). A platform change lands there,
  then reaches shortly in a PR here that moves the lock.
- `deploy.sh` and `promote.sh` run the platform's deploy gate (`scripts/verify-image.sh` at
  `platform.lock`), which enforces the same provenance rules as its admission policy (the
  platform's tests check they agree).
- Migrations follow expand / deploy / contract; every revision has a working `downgrade()`.

## Pull requests opened by Claude

- **No tool attribution in PR descriptions** (Emrah, 2026-10-29): no "Generated with/by
  Claude Code" line, no claude.ai session link, no `---` separator for one. The description
  ends with its last point. (The session's GitHub connection may append its own footer when
  it writes a PR; that one is outside the session's control.)
- **One problem per PR, small enough to review in one sitting**: aim for about 200 changed
  lines (a sweet spot, not a hard limit). Tests and the docs updates the change needs ride
  in the same PR; generated files (`deploy/prometheus/rules/slo.yaml`,
  `deploy/service/slo-rules.values.yaml`, `uv.lock`) don't count, but say so in the description.
- A bigger change goes in as a stack: each PR leaves `main` working and CI green on its own
  (dormant pieces first, for example a chart nothing applies yet, then the switch that uses
  it), and its description says where it sits in the stack.
- A refactor and a behaviour change never share a PR.

- Docs-only and record-digest PRs: turn on auto-merge (squash) when opening them; they merge
  once CI is green. Docs-only PRs skip the CI work (`scripts/ci-scope.sh`).
- Promotion PRs (`scripts/promote.sh … --pr`) and code / CI PRs: never auto-merge. Merging a
  promotion is the deploy, so the human merge is the approval.
