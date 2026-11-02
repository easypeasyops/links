# links

The `links` domain's apps, deployed on [easypeasyops/platform](https://github.com/easypeasyops/platform).
Each app lives in its own folder under `apps/`, with its code, tests, deploy values, scripts
and docs (platform plan 3.5):

| App | What it is | Docs |
| --- | --- | --- |
| [shortly](apps/shortly/) | A URL shortener: `POST /links`, `GET /{code}` | [README](apps/shortly/README.md) · [Design](apps/shortly/docs/DESIGN.md) · [Runbook](apps/shortly/docs/RUNBOOK.md) |

At the repo root: CI and releases (`.github/workflows/`), `platform.lock` (the platform commit
every app here is tested and deployed against) and `CLAUDE.md`.

`deploy` at the root is a link to `apps/shortly/deploy`, only while the platform's
`apps/shortly/envs/*.yaml` still name the old paths; it goes once they name the new ones.
