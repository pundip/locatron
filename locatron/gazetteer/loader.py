"""Load-once, cache-in-process gazetteer plumbing.

Every gazetteer in this package is small enough to sit in each worker's memory:
249 countries, 358 country-bucket variants, ~48k cities, ~18.5k AU localities,
~65k state-bucket variants. They are read once on first use and never
invalidated at runtime, because the upstream tables they come from are
read-only mirrors that only change on a rebuild.

The street gazetteer is deliberately NOT here. Per-worker Python objects are
duplicated and refcounting defeats copy-on-write, so 15M address rows go in
SQLite where workers share OS page cache. See CLAUDE.md.

`reset_gazetteers()` exists for tests, which need to swap in fixture data
without a live database.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from functools import wraps
from typing import Any

from sqlalchemy import text

from locatron.db import mysql

_REGISTRY: list[Callable[[], None]] = []


def cached_gazetteer[T](fn: Callable[[], T]) -> Callable[[], T]:
    """Memoise a zero-argument loader and register it for `reset_gazetteers()`.

    Not `functools.lru_cache`, because that hides the sentinel and makes it
    awkward to tell "loaded, legitimately empty" from "not loaded yet" — a
    distinction that matters when a gazetteer query silently returns nothing.
    """
    sentinel = object()
    box: list[Any] = [sentinel]

    @wraps(fn)
    def wrapper() -> T:
        if box[0] is sentinel:
            box[0] = fn()
        return box[0]  # type: ignore[return-value]

    def clear() -> None:
        box[0] = sentinel

    wrapper.cache_clear = clear  # type: ignore[attr-defined]
    wrapper.is_loaded = lambda: box[0] is not sentinel  # type: ignore[attr-defined]
    _REGISTRY.append(clear)
    return wrapper


def reset_gazetteers() -> None:
    """Drop every cached gazetteer. Tests use this; production never needs it."""
    for clear in _REGISTRY:
        clear()


def rows(sql: str, **params: Any) -> Iterator[dict[str, Any]]:
    """Stream a read-only query as plain dicts.

    Gazetteer loads are one-shot bulk reads, so they take their own session
    rather than borrowing a request-scoped one.
    """
    with mysql.session_scope() as s:
        for row in s.execute(text(sql), params).mappings():
            yield dict(row)


def as_int(value: Any, default: int = 0) -> int:
    """Parse an upstream varchar number.

    Upstream stores numbers as varchar with blanks instead of NULLs, and
    Cities.population has a handful of rows written as '11001.00'. int() throws
    on both, so go through float().
    """
    if value is None:
        return default
    s = str(value).strip()
    if not s:
        return default
    try:
        return int(float(s))
    except ValueError:
        return default


def as_float(value: Any) -> float | None:
    """Parse an upstream varchar coordinate, returning None for blanks.

    Blank must become None, not 0.0. A 0.0 latitude is a real place in the Gulf
    of Guinea, and averaging blanks as zero is exactly the trap that drags
    centroids off the coast of Africa.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None
