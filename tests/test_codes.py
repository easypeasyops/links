import pytest

from shortly.codes import ALPHABET, DEFAULT_LENGTH, generate_code


def test_alphabet_is_base62() -> None:
    assert len(ALPHABET) == 62
    assert len(set(ALPHABET)) == 62


def test_default_length() -> None:
    assert len(generate_code()) == DEFAULT_LENGTH


@pytest.mark.parametrize("length", [1, 7, 12, 64])
def test_respects_length(length: int) -> None:
    assert len(generate_code(length)) == length


def test_only_alphabet_characters() -> None:
    code = generate_code(1_000)
    assert set(code) <= set(ALPHABET)


@pytest.mark.parametrize("length", [0, -1, -100])
def test_rejects_non_positive_length(length: int) -> None:
    with pytest.raises(ValueError, match="length must be >= 1"):
        generate_code(length)


def test_no_duplicates_in_sample() -> None:
    # Smoke test, not a randomness proof: a constant or tiny-period generator fails here,
    # but a predictable PRNG (e.g. `random`) would still pass.
    # At 10k draws from 62**7, P(any collision) ≈ n²/2N ≈ 1.4e-5 — flake risk is negligible.
    n = 10_000
    assert len({generate_code() for _ in range(n)}) == n
