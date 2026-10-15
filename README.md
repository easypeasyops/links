# shortly

[![CI](https://github.com/statsguy/shortly/actions/workflows/ci.yml/badge.svg)](https://github.com/statsguy/shortly/actions/workflows/ci.yml)

A URL shortener built as a hands-on DevOps practice project: uv, strict typing, tests, CI/CD, containers, and observability.

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

## Migrations

```bash
uv run alembic revision --autogenerate -m "describe change"   # after editing src/shortly/db.py
uv run alembic upgrade head
uv run alembic upgrade head --sql                              # print SQL for review, don't run it
```

`tests/test_postgres.py` fails if `db.py` and the migrations drift apart.
