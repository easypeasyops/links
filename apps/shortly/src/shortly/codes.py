"""Short-code generation.

Codes act as unguessable capabilities: anyone holding one can resolve the URL,
so they must come from a CSPRNG (``secrets``), never ``random``.
"""

import secrets
import string
from typing import Final

ALPHABET: Final = string.digits + string.ascii_letters  # base62
DEFAULT_LENGTH: Final = 7  # 62**7 ≈ 3.5e12 codes


def generate_code(length: int = DEFAULT_LENGTH) -> str:
    """Return a random base62 code of ``length`` characters."""
    if length < 1:
        raise ValueError(f"length must be >= 1, got {length}")
    return "".join(secrets.choice(ALPHABET) for _ in range(length))
