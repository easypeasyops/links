"""Contract tests for ``LinkStore``.

Every adapter must pass the same suite. When the Postgres store lands, it gets added to
the ``store_impl`` params and inherits all of these for free.
"""

import pytest

from shortly.models import ShortLink
from shortly.store import CodeConflictError, InMemoryStore, LinkStore
from tests.conftest import T0


@pytest.fixture(params=[InMemoryStore], ids=["memory"])
def store_impl(request: pytest.FixtureRequest) -> LinkStore:
    factory: type[LinkStore] = request.param
    return factory()


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
