"""Delete expired links: ``python -m shortly.cleanup``, run hourly by a CronJob.

Links with a TTL stay in the table after they expire (the API answers 410 Gone for them),
so without this the table only grows. The job deletes links that expired more than
``SHORTLY_CLEANUP_GRACE_DAYS`` ago, in bounded batches: each batch is one short statement
that commits on its own, so a run never holds a long lock, can be stopped at any point,
and can overlap another run safely (SKIP LOCKED, in the Postgres store).

Success and failure are visible from outside, with nothing to push: kube-state-metrics
reports the CronJob's last successful run (alert ShortlyCleanupNotRunning), and the run
logs one JSON line with what it did.
"""

import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from shortly.service import utc_now
from shortly.store import LinkStore

log = logging.getLogger("shortly.cleanup")


@dataclass(frozen=True)
class CleanupResult:
    deleted: int
    batches: int
    complete: bool  # False: stopped at max_batches with more left (the next run continues)


def delete_expired(
    store: LinkStore,
    *,
    grace: timedelta,
    batch_size: int,
    max_batches: int,
    clock: Callable[[], datetime] = utc_now,
    pause: Callable[[], None] = lambda: time.sleep(0.05),
) -> CleanupResult:
    """Delete links that expired before ``now - grace``, ``batch_size`` at a time.

    Stops when a batch comes back short (nothing left) or after ``max_batches``, so one
    run's work and duration are bounded even with a huge backlog. ``pause`` between
    batches leaves room for the request traffic sharing the database.
    """
    cutoff = clock() - grace
    deleted = 0
    for batch in range(1, max_batches + 1):
        n = store.delete_expired(cutoff, batch_size)
        deleted += n
        if n < batch_size:
            return CleanupResult(deleted, batch, complete=True)
        pause()
    return CleanupResult(deleted, max_batches, complete=False)


def main() -> int:
    # Imported here so the module (and its tests) don't need a database configured.
    from shortly.db import make_engine
    from shortly.observability import configure_logging
    from shortly.pg_store import PostgresStore
    from shortly.settings import Settings

    settings = Settings()
    configure_logging(settings.log_level, json_output=settings.log_json)
    # Autocommit, one connection. A batch delete walks the partial index and takes
    # milliseconds; 30 s is a guard against a pathological plan, not a budget.
    engine = make_engine(str(settings.database_url), pool_size=1, statement_timeout_ms=30_000)
    try:
        started = time.monotonic()
        result = delete_expired(
            PostgresStore(engine),
            grace=timedelta(days=settings.cleanup_grace_days),
            batch_size=settings.cleanup_batch_size,
            max_batches=settings.cleanup_max_batches,
        )
    except Exception:
        log.exception("cleanup failed")
        return 1  # the Job fails, its retries run, and the CronJob's last success goes stale
    finally:
        engine.dispose()
    log.info(
        "cleanup finished",
        extra={
            "deleted": result.deleted,
            "batches": result.batches,
            "complete": result.complete,
            "duration_ms": round((time.monotonic() - started) * 1000),
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
