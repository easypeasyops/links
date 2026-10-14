# shortly

[![CI](https://github.com/statsguy/shortly/actions/workflows/ci.yml/badge.svg)](https://github.com/statsguy/shortly/actions/workflows/ci.yml)

A URL shortener built as a hands-on DevOps practice project: uv, strict typing, tests, CI/CD, containers, and observability.

## Development

```bash
uv sync                     # create .venv from uv.lock
uv run pre-commit install   # local quality gate
uv run pytest
```
