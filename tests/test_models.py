from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from shortly.models import ShortLink
from tests.conftest import T0


def make(**overrides: object) -> ShortLink:
    fields: dict[str, object] = {
        "code": "abc1234",
        "target": "https://example.com/",
        "created_at": T0,
    }
    return ShortLink.model_validate(fields | overrides)


def test_valid_link() -> None:
    link = make()
    assert str(link.target) == "https://example.com/"
    assert link.expires_at is None


@pytest.mark.parametrize("target", ["not a url", "ftp://example.com", "javascript:alert(1)", ""])
def test_rejects_non_http_targets(target: str) -> None:
    with pytest.raises(ValidationError):
        make(target=target)


def test_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError):
        make(created_at=datetime(2026, 1, 1))  # noqa: DTZ001 — the naive value is the point


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(seconds=-1)])
def test_rejects_expiry_not_after_creation(delta: timedelta) -> None:
    with pytest.raises(ValidationError, match="expires_at must be after created_at"):
        make(expires_at=T0 + delta)


def test_is_frozen() -> None:
    link = make()
    with pytest.raises(ValidationError):
        link.code = "other"  # type: ignore[misc]


def test_expiry_boundary() -> None:
    link = make(expires_at=T0 + timedelta(hours=1))
    assert not link.is_expired(T0 + timedelta(minutes=59))
    assert link.is_expired(T0 + timedelta(hours=1))  # expiry instant is already expired


def test_never_expires_without_expiry() -> None:
    assert not make().is_expired(T0 + timedelta(days=36_500))
