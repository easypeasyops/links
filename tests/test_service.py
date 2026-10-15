from datetime import timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from shortly.codes import ALPHABET
from shortly.service import (
    CodeSpaceExhaustedError,
    LinkExpiredError,
    LinkNotFoundError,
    Shortener,
)
from shortly.store import InMemoryStore
from tests.conftest import FakeClock, scripted_codes


def test_shorten_then_resolve(shortener: Shortener) -> None:
    link = shortener.shorten("https://example.com/a?b=1")
    assert shortener.resolve(link.code) == "https://example.com/a?b=1"


def test_shorten_records_clock_time(shortener: Shortener, clock: FakeClock) -> None:
    assert shortener.shorten("https://example.com/").created_at == clock.now


def test_resolve_unknown_code(shortener: Shortener) -> None:
    with pytest.raises(LinkNotFoundError):
        shortener.resolve("missing")


def test_invalid_url_is_rejected_before_storing(store: InMemoryStore, clock: FakeClock) -> None:
    calls = 0

    def counting_gen() -> str:
        nonlocal calls
        calls += 1
        return "abc1234"

    with pytest.raises(ValidationError):
        Shortener(store, code_gen=counting_gen, clock=clock).shorten("not a url")
    assert calls == 0  # failed fast: no code drawn, nothing stored


def test_retries_on_collision(store: InMemoryStore, clock: FakeClock) -> None:
    svc = Shortener(store, code_gen=scripted_codes("dup", "dup", "fresh"), clock=clock)
    assert svc.shorten("https://a.example/").code == "dup"
    assert svc.shorten("https://b.example/").code == "fresh"  # skipped the taken "dup"
    assert svc.resolve("dup") == "https://a.example/"  # first link untouched


def test_gives_up_after_max_attempts(store: InMemoryStore, clock: FakeClock) -> None:
    svc = Shortener(store, code_gen=lambda: "same", clock=clock, max_attempts=3)
    svc.shorten("https://a.example/")
    with pytest.raises(CodeSpaceExhaustedError, match="3 attempts"):
        svc.shorten("https://b.example/")


def test_rejects_non_positive_max_attempts(store: InMemoryStore) -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        Shortener(store, max_attempts=0)


def test_ttl_expiry(shortener: Shortener, clock: FakeClock) -> None:
    link = shortener.shorten("https://example.com/", ttl=timedelta(hours=1))
    clock.advance(timedelta(minutes=59))
    assert shortener.resolve(link.code) == "https://example.com/"
    clock.advance(timedelta(minutes=1))
    with pytest.raises(LinkExpiredError):
        shortener.resolve(link.code)


def test_non_positive_ttl_is_rejected(shortener: Shortener) -> None:
    with pytest.raises(ValidationError):
        shortener.shorten("https://example.com/", ttl=timedelta(0))


# --- Property-based tests: state an invariant, let Hypothesis hunt for counterexamples.

paths = st.text(alphabet=st.characters(categories=["L", "N"]), max_size=30)


@given(path=paths)
def test_round_trip_property(path: str) -> None:
    # Hypothesis calls this many times per run, so build fresh state inside the test
    # rather than using function-scoped fixtures (which would be shared across examples).
    svc = Shortener(InMemoryStore(), clock=FakeClock())
    target = f"https://example.com/{path}"
    link = svc.shorten(target)
    assert svc.resolve(link.code) == str(link.target)
    assert set(link.code) <= set(ALPHABET)


@given(n=st.integers(min_value=1, max_value=50))
def test_codes_unique_within_store(n: int) -> None:
    svc = Shortener(InMemoryStore(), clock=FakeClock())
    codes = [svc.shorten(f"https://example.com/{i}").code for i in range(n)]
    assert len(set(codes)) == n
