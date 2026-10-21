# shortly

[![CI](https://github.com/statsguy/shortly/actions/workflows/ci.yml/badge.svg)](https://github.com/statsguy/shortly/actions/workflows/ci.yml)

A URL shortener built as a hands-on DevOps practice project: uv, strict typing, tests, CI/CD, containers, and observability.

## Docs

- [Design](docs/DESIGN.md): architecture, decisions, failure modes, security
- [Runbook](docs/RUNBOOK.md): release, deploy, rollback, incident playbooks

## API

| Method | Path       | Result                                                     |
|--------|------------|------------------------------------------------------------|
| POST   | `/links`   | `{"url": "...", "ttl_seconds": 3600}` → 201 with `code`, `short_url` |
| GET    | `/{code}`  | 307 redirect · 404 unknown · 410 expired                   |
| GET    | `/healthz` | Liveness: process is up (no dependencies checked)          |
| GET    | `/readyz`  | Readiness: 503 if the database is unreachable              |

Interactive docs at `/docs` while the server runs.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync                          # create .venv from uv.lock
uv run pre-commit install        # local quality gate
cp .env.example .env             # local config (git-ignored)
docker compose up -d --wait      # Postgres on localhost:5432
uv run alembic upgrade head      # apply migrations
uv run shortly                   # http://localhost:8000
```

## Tests

```bash
uv run pytest                        # everything; starts a throwaway Postgres via testcontainers
uv run pytest -m "not integration"   # fast loop, no Docker needed
```

Without Docker, integration tests are skipped locally. In CI they fail instead of skipping.

Coverage is reported on every run but only *enforced* in CI (90% floor on the full suite).
To reproduce CI exactly: `uv run pytest --cov-fail-under=90` with Docker running.

## Container

```bash
docker compose --profile app up -d --build --wait   # Postgres → migrate (one-shot) → app
SHORTLY_IMAGE=shortly:dev scripts/smoke.sh          # the same black-box test CI runs
docker compose --profile app down -v
```

The image is multi-stage (build tools never ship), runs as UID 10001 with read-only code,
has pip removed, and handles SIGTERM gracefully. Migrations run as a separate one-shot
command (`alembic upgrade head`) from the same image — never on app startup.

## Observability

| Signal  | Where | Notes |
|---------|-------|-------|
| Logs    | stdout, one JSON object per line | `request_id` on every line; `trace_id` when tracing is on |
| Metrics | `GET /metrics` (Prometheus) | RED metrics by route *template*, plus links created, resolutions, code collisions |
| Traces  | OTLP/HTTP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set | request span → SQL spans (psycopg) |

Every response carries `x-request-id` (a valid caller-supplied ID is propagated).

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318 \
  docker compose --profile app --profile obs up -d --build --wait
# Prometheus: http://localhost:9090   Jaeger: http://localhost:16686
```

Useful queries:

```promql
sum by (route, status) (rate(http_requests_total[5m]))                       # traffic & errors
histogram_quantile(0.99, sum by (le, route) (rate(http_request_duration_seconds_bucket[5m])))  # p99
rate(code_collisions_total[1h])                                              # code space filling up?
```

## Release

```bash
# 1. bump `version` in pyproject.toml in a PR, merge it
# 2. tag the merge commit
git switch main && git pull
git tag v0.1.0 && git push origin v0.1.0
```

`.github/workflows/release.yml` then: checks the tag matches `pyproject.toml`, builds and
**scans before publishing**, pushes a multi-arch (amd64 + arm64) image to
`ghcr.io/statsguy/shortly` with an SBOM and provenance, signs a provenance attestation
(Sigstore, keyless), and creates a GitHub Release whose notes contain the image **digest**.

```bash
gh attestation verify oci://ghcr.io/statsguy/shortly@sha256:<digest> --owner statsguy
```

## Deploy (local Kubernetes)

Requires Docker, [kind](https://kind.sigs.k8s.io), kubectl, and Helm 4.

```bash
scripts/cluster.sh up                                          # kind + metrics-server + staging/prod namespaces + Postgres
scripts/deploy.sh staging ghcr.io/statsguy/shortly@sha256:<digest>
scripts/deploy.sh prod    ghcr.io/statsguy/shortly@sha256:<digest>   # same digest: build once, deploy many
helm -n prod history shortly                                   # what can I roll back to?
helm -n prod rollback shortly <revision>
```

A failed rollout rolls back automatically (`--rollback-on-failure`); `maxUnavailable: 0`
keeps old pods serving throughout. Namespaces enforce the **restricted** Pod Security
Standard. `scripts/k8s-e2e.sh` (run in CI on every PR) deploys, promotes, breaks a release
on purpose, asserts it rolled back with zero failed requests, then loads prod and checks
that the HorizontalPodAutoscaler adds pods.

Local image instead of a release: `docker build -t shortly:dev . && scripts/deploy.sh staging shortly:dev`

## Load testing and capacity

```bash
SHORTLY_IMAGE=shortly:dev docker compose --profile app up -d --wait
scripts/loadtest.sh                          # k6 step test, app pinned to 1 CPU; prints PASS/FAIL per rate
RATES="200 250 300" scripts/loadtest.sh
```

One pod sustains about 250 req/s within the latency SLO (redirect p99 < 50 ms) and is
CPU-bound; prod autoscales on CPU from 2 pods. Method, numbers and sizing:
[DESIGN.md, Capacity](docs/DESIGN.md#capacity).

## SLOs and alerts

| SLO | Objective (30 days) |
| --- | --- |
| Redirects without a server error | 99.9% |
| Redirects under 50 ms | 99% |
| Creates without a server error | 99.5% |
| Creates under 100 ms | 99% |

Declared in `deploy/prometheus/slos.yaml`; `scripts/gen_slo_rules.py` generates the
recording rules and multi-window burn-rate alerts in `deploy/prometheus/rules/`. Every alert
has promtool unit tests, and CI fails if a rule selects a metric the app doesn't export.
Locally, `docker compose --profile app --profile obs up -d` and open
http://localhost:9090/alerts. Details: [DESIGN.md, SLOs and alerting](docs/DESIGN.md#slos-and-alerting).

## Migrations

```bash
uv run alembic revision --autogenerate -m "describe change"   # after editing src/shortly/db.py
uv run alembic upgrade head
uv run alembic upgrade head --sql                              # print SQL for review, don't run it
```

`tests/test_postgres.py` fails if `db.py` and the migrations drift apart.
