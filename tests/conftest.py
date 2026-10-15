from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest

from shortly.service import Shortener
from shortly.store import InMemoryStore

T0 = datetime(2026, 1, 1, tzinfo=UTC)


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
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def shortener(store: InMemoryStore, clock: FakeClock) -> Shortener:
    return Shortener(store, clock=clock)
