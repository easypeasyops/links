"""Twelve-factor config: everything comes from the environment, validated at startup.

A typo'd or missing variable fails the process on boot, not on the first request.
"""

from typing import Literal

from pydantic import PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHORTLY_", env_file=".env", extra="ignore")

    database_url: PostgresDsn
    # Public base for generated links, e.g. https://sho.rt — not derived from the request
    # Host header, which a client controls.
    base_url: str = "http://localhost:8000"
    db_pool_size: int = 5
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True  # set SHORTLY_LOG_JSON=false for human-readable local logs
    service_name: str = "shortly"  # the service.name on every trace
