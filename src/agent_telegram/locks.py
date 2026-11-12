"""Bounded thread/process sidecar locks for the standalone job store."""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_MAX_LOCK_WAIT_S = 5.0
_LOCK_RETRY_S = 0.02
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(path: Path) -> threading.Lock:
    key = str(path).casefold()
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


@contextmanager
def _exclusive_lock(lock_path: Path) -> Iterator[None]:
    """Acquire a thread lock then a one-byte Windows/POSIX file lock.

    Each acquisition waits at most five seconds. The persistent sidecar and
    case-folded thread-lock key coordinate concurrent threads and processes.
    """
    thread_lock = _thread_lock(lock_path)
    if not thread_lock.acquire(timeout=_MAX_LOCK_WAIT_S):
        raise TimeoutError(f"无法取得笔记锁: {lock_path}")
    handle = None
    locked = False
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        deadline = time.monotonic() + _MAX_LOCK_WAIT_S
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"无法取得笔记锁: {lock_path}") from None
                    time.sleep(_LOCK_RETRY_S)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"无法取得笔记锁: {lock_path}") from None
                    time.sleep(_LOCK_RETRY_S)
        yield
    finally:
        if handle is not None:
            if locked:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()
        thread_lock.release()
