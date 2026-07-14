"""Small sync and async retry decorators."""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Callable
from typing import Any, TypeVar

from exceptions import HTTPError

T = TypeVar("T")


def _should_retry(exc: BaseException, exceptions: tuple[type[BaseException], ...]) -> bool:
    if isinstance(exc, HTTPError):
        return exc.retryable
    return isinstance(exc, exceptions)


def retry(
    *,
    attempts: int = 3,
    delay: float = 0.5,
    backoff: float = 2.0,
    exceptions: tuple[type[BaseException], ...] = (TimeoutError, ConnectionError),
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    def decorate(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapped(*args: Any, **kwargs: Any) -> T:
            wait = delay
            for attempt in range(attempts):
                try:
                    return func(*args, **kwargs)
                except BaseException as exc:
                    if attempt + 1 == attempts or not _should_retry(exc, exceptions):
                        raise
                    time.sleep(wait)
                    wait *= backoff
            raise RuntimeError("unreachable")

        return wrapped

    return decorate


def async_retry(
    *,
    attempts: int = 3,
    delay: float = 0.5,
    backoff: float = 2.0,
    exceptions: tuple[type[BaseException], ...] = (TimeoutError, ConnectionError),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            wait = delay
            for attempt in range(attempts):
                try:
                    return await func(*args, **kwargs)
                except BaseException as exc:
                    if attempt + 1 == attempts or not _should_retry(exc, exceptions):
                        raise
                    await asyncio.sleep(wait)
                    wait *= backoff
            raise RuntimeError("unreachable")

        return wrapped

    return decorate
