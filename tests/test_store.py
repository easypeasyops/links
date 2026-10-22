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


def _expiring(code: str, expired_ago: timedelta | None) -> ShortLink:
    if expired_ago is None:
        return link(code)  # no TTL: never deleted
    created = T0 - expired_ago - timedelta(days=1)
    return ShortLink.model_validate(
        {
            "code": code,
            "target": "https://example.com/",
            "created_at": created,
            "expires_at": T0 - expired_ago,
        }
    )


def test_delete_expired_removes_only_links_expired_before_the_cutoff(store_impl: LinkStore) -> None:
    for code, ago in [
        ("old0001", timedelta(days=30)),
        ("old0002", timedelta(days=8)),
        ("new0001", timedelta(days=1)),  # expired, but after the cutoff
        ("forever", None),
    ]:
        store_impl.add(_expiring(code, ago))
    future = ShortLink.model_validate(
        {"code": "future1", "target": "https://example.com/", "created_at": T0,
         "expires_at": T0 + timedelta(days=1)}
    )  # fmt: skip
    store_impl.add(future)
    assert store_impl.delete_expired(T0 - timedelta(days=7), limit=100) == 2
    assert store_impl.get("old0001") is None
    assert store_impl.get("old0002") is None
    for kept in ("new0001", "forever", "future1"):
        assert store_impl.get(kept) is not None, kept


def test_delete_expired_respects_the_limit_oldest_first(store_impl: LinkStore) -> None:
    for i, days in enumerate([40, 30, 20]):
        store_impl.add(_expiring(f"old000{i}", timedelta(days=days)))
    assert store_impl.delete_expired(T0, limit=2) == 2
    assert store_impl.get("old0002") is not None  # the most recently expired is left
    assert store_impl.delete_expired(T0, limit=2) == 1
    assert store_impl.delete_expired(T0, limit=2) == 0
