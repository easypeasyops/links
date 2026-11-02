"""Domain model.

Validation happens here, at the boundary: once a ``ShortLink`` exists, the rest of
the code can trust its fields instead of re-checking strings and dicts.
"""

from datetime import datetime
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, HttpUrl, model_validator


class ShortLink(BaseModel):
    model_config = ConfigDict(frozen=True)  # immutable value object: safe to cache and share

    code: str
    target: HttpUrl
    created_at: AwareDatetime  # naive datetimes are rejected outright
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _expiry_after_creation(self) -> Self:
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        return self

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at
