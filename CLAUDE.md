# Working on shortly

## Docs are part of the change

`docs/DESIGN.md` (how and why the system works) and `docs/RUNBOOK.md` (how to operate it)
must stay true. Any PR that changes behaviour they describe updates them **in the same PR**:

- API, status codes, config variables, metrics, logs, traces → DESIGN
- CI jobs, release or deploy steps, scripts, chart values, probes, rollback → both
- A new failure mode, or an incident and its new guard → DESIGN (failure modes /
  issues table) and RUNBOOK (playbook)
- Version, digest or chart version in the headers and Quick reference → RUNBOOK

They are mirrored as Claude Docs (links in each file's header). When editing with Claude,
update the Claude Docs copy in the same session so the two don't drift; the repo copy is
the one reviewed with the code.

## Conventions

- `uv run pytest` (needs Docker for integration tests); CI enforces `--cov-fail-under=90`.
- Python version lives in `.python-version` and both Dockerfile `FROM` lines; change them together.
- Deploy by digest: `scripts/deploy.sh <env> ghcr.io/statsguy/shortly@sha256:…`.
- Migrations follow expand / deploy / contract; every revision has a working `downgrade()`.
