"""Load every in-process gazetteer up front, with per-loader timing.

Gazetteers are lazy: whichever request reaches a worker first pays the entire
load. Measured against ReferenceDB that is seconds, and `--max-requests`
recycling means a fresh worker — and so a fresh penalty — happens in normal
operation, not just at deploy. `warm()` moves the cost into worker startup,
where nobody is waiting on it. The gunicorn hook that calls it lives in
`deploy/gunicorn.conf.py`.

Order is deliberate. `load_countries()` calls `load_au()` and `load_cities()`
itself to build its sub-national exclusion set, so warming those two first is
what keeps each reported timing attributable to one loader instead of charging
their cost to whichever ran first.

The street gazetteer is not here. It belongs in SQLite, where workers share OS
page cache rather than each holding a copy. See CLAUDE.md.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import structlog

from locatron.gazetteer.au import load_au
from locatron.gazetteer.cities import load_cities
from locatron.gazetteer.countries import load_countries

log = structlog.get_logger("locatron.gazetteer")


def _au_counts(g: Any) -> dict[str, int]:
    """Rows, keys and aliases, kept apart on purpose.

    A single count invites the wrong question. `by_norm` is keyed by norm_key
    alone, so 18,567 locality rows sit under 16,228 keys — homonyms like PERTH
    (WA and TAS) and the 148 postal ranges under SYDNEY share a key by design,
    and `lookup()` filters by state afterwards. Reporting only the key count
    reads as though 2,339 rows went missing, and reporting no alias count at
    all hides 61,155 rows completely.
    """
    return {
        "rows": len(g.by_id),
        "keys": len(g.by_norm),
        "aliases": sum(len(v) for v in g.aliases.values()),
        "alias_keys": len(g.aliases),
    }


def _cities_counts(g: Any) -> dict[str, int]:
    # index_entries, not rows: a city whose local and ASCII spellings normalise
    # differently ("Zürich"/"Zurich") is indexed under both.
    return {
        "keys": len(g.by_norm),
        "index_entries": sum(len(v) for v in g.by_norm.values()),
    }


def _countries_counts(g: Any) -> dict[str, int]:
    return {"rows": len(g.by_alpha3), "tokens": len(g.tokens), "hints": len(g.hints)}


#: (name, loader, counts-of-result). Dependency order — see the module docstring.
LOADERS: tuple[tuple[str, Callable[[], Any], Callable[[Any], dict[str, int]]], ...] = (
    ("au", load_au, _au_counts),
    ("cities", load_cities, _cities_counts),
    ("countries", load_countries, _countries_counts),
)


def _is_loaded(loader: Callable[[], Any]) -> bool:
    """Whether `cached_gazetteer` has already populated this loader's box."""
    probe = getattr(loader, "is_loaded", None)
    return bool(probe()) if probe is not None else False


def is_warm() -> bool:
    """Whether every gazetteer in this process is loaded.

    Surfaced on /healthz so a cold worker is visible rather than silent.
    """
    return all(_is_loaded(loader) for _, loader, _ in LOADERS)


def warm() -> dict[str, float]:
    """Load all gazetteers, returning per-loader milliseconds.

    Never raises. A failure here must not take the worker down: the loaders are
    still lazy, so an unwarmed worker serves correctly and merely pays the cost
    on its first request. Crashing instead would turn a transient MySQL blip
    into a restart loop that removes the whole service.
    """
    timings: dict[str, float] = {}
    started = time.perf_counter()

    for name, loader, counts in LOADERS:
        if _is_loaded(loader):
            continue
        at = time.perf_counter()
        try:
            loaded = loader()
        except Exception as exc:
            timings[name] = round((time.perf_counter() - at) * 1000.0, 1)
            log.error(
                "gazetteer_load_failed",
                gazetteer=name,
                ms=timings[name],
                error=f"{type(exc).__name__}: {exc}",
            )
            continue
        timings[name] = round((time.perf_counter() - at) * 1000.0, 1)
        log.info("gazetteer_loaded", gazetteer=name, ms=timings[name], **counts(loaded))

    log.info(
        "gazetteers_warm",
        ms=round((time.perf_counter() - started) * 1000.0, 1),
        warm=is_warm(),
        **{f"{name}_ms": ms for name, ms in timings.items()},
    )
    return timings
