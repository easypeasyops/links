import gc
import logging
import os
import warnings
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

from shortly.db import make_engine
from shortly.pg_store import PostgresStore
from shortly.service import Shortener
from shortly.store import InMemoryStore

T0 = datetime(2026, 1, 1, tzinfo=UTC)


class FakeTime:
    """Epoch seconds that tests move by hand (the rate limiter's clock)."""

    def __init__(self, start: float = 1_767_225_600.0) -> None:  # 2026-01-01T00:00:00Z
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeClock:
    """A clock tests can move. Avoids sleep() and freezegun-style global patching."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def scripted_codes(*codes: str) -> Callable[[], str]:
    """A code generator that returns ``codes`` in order — makes collisions deterministic."""
    it: Iterator[str] = iter(codes)
    return lambda: next(it)


@pytest.fixture
def stub_platform(tmp_path: Path) -> Path:
    """A stand-in platform checkout for scripts that load it (scripts/lib/platform.sh): the
    namespace map they source and shortly's env files. Set PLATFORM_DIR to it; CI's check job
    has no checkout."""
    namespaces = tmp_path / "platform/scripts/lib/namespaces.sh"
    namespaces.parent.mkdir(parents=True)
    namespaces.write_text('ENVS=(staging prod)\nns_of() { echo "shortly-$1"; }\n')
    root = namespaces.parents[2]
    # shortly's env files, which name the chart digest and values files deploy.sh renders.
    for env in ("staging", "prod"):
        env_file = root / f"apps/shortly/envs/{env}.yaml"
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text(
            f"app: shortly\nenv: {env}\nchart:\n  version: 0.1.0\n  digest: sha256:{'b' * 64}\n"
            "values:\n  files:\n    - deploy/service/values.yaml\n"
            f"    - deploy/envs/{env}.yaml\n"
        )
    return root


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def shortener(store: InMemoryStore, clock: FakeClock) -> Shortener:
    return Shortener(store, clock=clock)


# --- Postgres (integration) fixtures -------------------------------------------------
# One container per test session (seconds to start), migrated with the real Alembic
# migrations, then truncated between tests. Fast enough to run on every push.

POSTGRES_IMAGE = "postgres:17-alpine"  # pin the same major version you run in prod


def _docker_available() -> bool:
    import docker

    # docker-py leaks its socket when the daemon is unreachable. Collect it here, with
    # ResourceWarning silenced for just this probe, so the leak can't surface later as an
    # unrelated test failure (we run with filterwarnings=error).
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        try:
            client = docker.from_env()
            try:
                return bool(client.ping())
            finally:
                client.close()
        except Exception:
            return False
        finally:
            gc.collect()


def alembic_config(url: str) -> Config:
    cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False  # keep pytest's logging config intact
    return cfg


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    if not _docker_available():
        # Skipping locally is a convenience; skipping in CI would be a silent hole in the
        # safety net, so there it's a hard failure.
        if os.environ.get("CI"):
            pytest.fail("Docker is required for integration tests in CI")
        pytest.skip("Docker not available")
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer(POSTGRES_IMAGE, driver=None) as pg:
        url = pg.get_connection_url()
        command.upgrade(alembic_config(url), "head")
        yield url


@pytest.fixture(scope="session")
def pg_engine(pg_url: str) -> Iterator[Engine]:
    engine = make_engine(pg_url)
    yield engine
    engine.dispose()


@pytest.fixture
def pg_store(pg_engine: Engine) -> PostgresStore:
    with pg_engine.begin() as conn:
        conn.execute(text("TRUNCATE links, rate_limits"))
    return PostgresStore(pg_engine)


@pytest.fixture
def clean_rate_limits(pg_engine: Engine) -> Engine:
    with pg_engine.begin() as conn:
        conn.execute(text("TRUNCATE rate_limits"))
    return pg_engine


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    saved = root.handlers[:], root.level, logging.getLogger("uvicorn.access").disabled
    yield
    root.handlers[:], level, disabled = saved
    root.setLevel(level)
    logging.getLogger("uvicorn.access").disabled = disabled
