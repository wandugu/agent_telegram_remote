"""Migration barrier shared by MCP tools and long-running services (stdlib only)."""

from __future__ import annotations

import functools
import inspect
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from agent_telegram.i18n import tr
from agent_telegram.paths import gate_path

_SOURCE_MESSAGE = 'agent_telegram is migrating (GATE); please try again later'
POLL_INTERVAL_S = 5.0


class MigrationActiveError(RuntimeError):
    """The migration barrier refused a tool before its implementation ran."""


def active() -> bool:
    """Read the barrier without creating any directories or state files."""
    return gate_path().exists()


def guarded(function: Callable[..., Any]) -> Callable[..., Any]:
    """Check the barrier at invocation while preserving the original tool schema."""
    if inspect.iscoroutinefunction(function):
        @functools.wraps(function)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            if active():
                raise MigrationActiveError(tr('agent_telegram is migrating (GATE); please try again later'))
            return await function(*args, **kwargs)
    else:
        @functools.wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if active():
                raise MigrationActiveError(tr('agent_telegram is migrating (GATE); please try again later'))
            return function(*args, **kwargs)

    # MCP/Pydantic resolve forward annotations in the wrapper's module. Resolve
    # them against the implementation first, retaining Context injection,
    # Annotated metadata, defaults, and the exact flat input schema.
    wrapper.__signature__ = inspect.signature(function, eval_str=True)
    wrapper.__annotations__ = inspect.get_annotations(function, eval_str=True)
    return wrapper


def tool_registration(register: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a server's registration decorator so every registered tool is gated."""
    @functools.wraps(register)
    def tool(*args: Any, **kwargs: Any) -> Any:
        decorate = register(*args, **kwargs)

        def register_guarded(function: Callable[..., Any]) -> Any:
            return decorate(guarded(function))

        return register_guarded

    return tool


def wait_until_open(*, log: Callable[[str], Any] | None = None,
                    stop: threading.Event | None = None) -> bool:
    """Wait before service startup, logging once and polling every five seconds.

    The broker may supply its test/shutdown event; False means it was stopped
    while waiting and must exit without starting its normal work.
    """
    announced = False
    while active():
        if stop is not None and stop.is_set():
            return False
        if not announced:
            (log or logging.getLogger(__name__).warning)(tr('agent_telegram is migrating (GATE); please try again later'))
            announced = True
        if stop is None:
            time.sleep(POLL_INTERVAL_S)
        elif stop.wait(POLL_INTERVAL_S):
            return False
    return True


def __getattr__(name: str) -> str:
    """Resolve legacy message exports at access time, including for the pending bridge."""
    if name == 'MESSAGE':
        return tr('agent_telegram is migrating (GATE); please try again later')
    raise AttributeError(name)
