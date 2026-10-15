"""Contract tests for ``LinkStore``.

Every adapter must pass the same suite. When the Postgres store lands, it gets added to
the ``store_impl`` params and inherits all of these for free.
"""

from datetime import timedelta

import pytest

from shortly.models import ShortLink
from shortly.store import CodeConflictError, InMemoryStore, LinkStore
from tests.conftest import T0


@pytest.fixture(
    params=[
        pytest.param("memory"),
        pytest.param("postgres", marks=pytest.mark.integration),
    ]
)
def store_impl(request: pytest.FixtureRequest) -> LinkStore:
    if request.param == "postgres":
        store: LinkStore = request.getfixturevalue("pg_store")  # only started if selected
        return store
    return InMemoryStore()


def link(code: str = "abc1234", target: str = "https://example.com/") -> ShortLink:
    return ShortLink.model_validate({"code": code, "target": target, "created_at": T0})


def test_get_missing_returns_none(store_impl: LinkStore) -> None:
    assert store_impl.get("nope") is None


def test_add_then_get(store_impl: LinkStore) -> None:
    original = link()
    store_impl.add(original)
    assert store_impl.get(original.code) == original


def test_duplicate_code_conflicts_and_keeps_first(store_impl: LinkStore) -> None:
    store_impl.add(link(target="https://first.example/"))
    with pytest.raises(CodeConflictError):
        store_impl.add(link(target="https://second.example/"))
    stored = store_impl.get("abc1234")
    assert stored is not None
    assert str(stored.target) == "https://first.example/"


def test_round_trips_expiry_and_timezone(store_impl: LinkStore) -> None:
    original = ShortLink.model_validate(
        {
            "code": "ttl0001",
            "target": "https://example.com/",
            "created_at": T0,
            "expires_at": T0 + timedelta(hours=1),
        }
    )
    store_impl.add(original)
    assert store_impl.get("ttl0001") == original  # tz-aware in, tz-aware out


def test_ping(store_impl: LinkStore) -> None:
    assert store_impl.ping() is True
