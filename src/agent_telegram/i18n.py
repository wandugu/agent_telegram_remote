"""Small, standard-library-only translation layer, evaluated at use time.

AGENT_TG_LANGUAGE optionally selects the initial process language, read
once at import (useful for test subprocesses). Explicit set_language calls,
including the bridge's configuration loading, take precedence.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from agent_telegram import distribution
from agent_telegram.locales.zh import MESSAGES

LANGUAGES = ("en", "zh")


def normalize(value: object) -> str | None:
    """Return a canonical language name, or None for an unknown value."""
    if not isinstance(value, str):
        return None
    return {"en": "en", "english": "en", "zh": "zh", "cn": "zh",
            "zh-cn": "zh", "zh_cn": "zh", "中文": "zh"}.get(value.strip().lower())  # i18n: data


_language = normalize(os.environ.get("AGENT_TG_LANGUAGE")) or distribution.DEFAULT_LANGUAGE


def current() -> str:
    return _language


def set_language(lang: str) -> None:
    global _language
    value = normalize(lang)
    if value is None:
        raise ValueError(f"Unknown language: {lang!r}")
    _language = value


@contextmanager
def use(lang: str) -> Iterator[None]:
    """Temporarily switch the process language, restoring it even on failure."""
    previous = current()
    set_language(lang)
    try:
        yield
    finally:
        set_language(previous)


def tr(text: str, /, **values: object) -> str:
    message = MESSAGES.get(text, text) if current() == "zh" else text
    return message.format(**values) if values else message
