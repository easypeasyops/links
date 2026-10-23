"""Twelve-factor config: everything comes from the environment, validated at startup.

A typo'd or missing variable fails the process on boot, not on the first request.
"""

from typing import Literal

from pydantic import Field, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHORTLY_", env_file=".env", extra="ignore")

    database_url: PostgresDsn
    # Public base for generated links, e.g. https://sho.rt — not derived from the request
    # Host header, which a client controls.
    base_url: str = "http://localhost:8000"
    # /metrics on its own port, so the public port carries no internal data and a
    # NetworkPolicy can let only Prometheus reach it. 0 = serve it on the app port instead.
    metrics_port: int = Field(default=9100, ge=0, le=65535)
    db_pool_size: int = 5
    # Seconds a request may wait for a pooled connection before it is shed with a 503.
    db_pool_timeout_seconds: float = 1.0
    # Postgres cancels a statement running longer than this, or waiting on a lock longer
    # than lock_timeout; the request gets a 503 instead of holding a connection. 0 = off.
    db_statement_timeout_ms: int = Field(default=2000, ge=0)
    db_lock_timeout_ms: int = Field(default=1000, ge=0)
    # Requests a pod works on at once; more are answered 503 + Retry-After immediately,
    # instead of queueing until every request is slow (docs/DESIGN.md, Capacity). 0 = off.
    max_concurrent_requests: int = Field(default=16, ge=0)
    # DRILLS ONLY. This share of creates and redirects fails with 500 while probes stay
    # green: a release that is ready but broken, to rehearse the canary catching it.
    fault_error_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    # Per-client limit on POST /links (shortly.ratelimit). A quiet client may create
    # `burst` links at once, then one every 60/per_minute seconds. 0 turns limiting off.
    rate_limit_creates_per_minute: float = Field(default=60.0, ge=0)
    rate_limit_burst: int = Field(default=20, ge=1)
    # Expired-link cleanup (python -m shortly.cleanup, run by a CronJob). A link expired less
    # than grace_days ago is kept, so it still answers 410 Gone rather than 404.
    cleanup_grace_days: int = Field(default=7, ge=0)
    cleanup_batch_size: int = Field(default=1000, ge=1, le=100_000)
    cleanup_max_batches: int = Field(default=100, ge=1)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True  # set SHORTLY_LOG_JSON=false for human-readable local logs
    service_name: str = "shortly"  # the service.name on every trace
