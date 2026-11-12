"""Standard-library-only helpers for publishing files atomically."""

from __future__ import annotations

import os
import time

_REPLACE_TIMEOUT_S = 2.0
_INITIAL_DELAY_S = 0.01
_MAX_DELAY_S = 0.2


def replace_atomic(
    source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
) -> None:
    """Replace a file, retrying only Windows access/sharing violations for 2 s.

    Pass paths through unchanged, including any Windows verbatim-path prefix.
    The caller owns temporary-file cleanup. On failure the last exception is
    re-raised unchanged; other platforms call ``os.replace`` exactly once.
    """
    if os.name != "nt":
        return os.replace(source, destination)

    deadline = None
    delay = _INITIAL_DELAY_S
    while True:
        try:
            return os.replace(source, destination)
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (5, 32):
                raise
            now = time.monotonic()
            if deadline is None:
                deadline = now + _REPLACE_TIMEOUT_S
            remaining = deadline - now
            if remaining <= 0:
                raise
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, _MAX_DELAY_S)
