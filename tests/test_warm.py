"""Gazetteer warming tests.

The behaviour worth pinning down is that warming is a latency optimisation and
nothing more: it must never be able to take a worker down, and a process that
skipped it must still report itself honestly rather than claiming to be warm.

No database needed. The loaders are replaced with stubs, because what is being
tested is the warm wrapper's control flow, not the SQL underneath it.
"""

from __future__ import annotations

from typing import Any

import pytest
from structlog.testing import capture_logs

from locatron.gazetteer import warm as warm_mod


class _Loader:
    """Stand-in for a `cached_gazetteer`-wrapped loader."""

    def __init__(self, *, entries: int = 3, boom: bool = False) -> None:
        self.entries = entries
        self.boom = boom
        self.calls = 0
        self._loaded = False

    def __call__(self) -> Any:
        self.calls += 1
        if self.boom:
            raise RuntimeError("mysql went away")
        self._loaded = True
        return object()

    def is_loaded(self) -> bool:
        return self._loaded


@pytest.fixture
def loaders(monkeypatch: pytest.MonkeyPatch) -> dict[str, _Loader]:
    made = {"au": _Loader(), "cities": _Loader(), "countries": _Loader()}
    monkeypatch.setattr(
        warm_mod,
        "LOADERS",
        tuple((name, ldr, lambda _g, n=name: len(n)) for name, ldr in made.items()),
    )
    return made


def test_warm_loads_every_gazetteer_once(loaders: dict[str, _Loader]) -> None:
    timings = warm_mod.warm()
    assert sorted(timings) == ["au", "cities", "countries"]
    assert all(ldr.calls == 1 for ldr in loaders.values())
    assert warm_mod.is_warm() is True


def test_warm_is_idempotent(loaders: dict[str, _Loader]) -> None:
    """A second call must not reload. Nothing calls it twice today, but a
    worker that warmed already should not pay again if something does."""
    warm_mod.warm()
    second = warm_mod.warm()
    assert all(ldr.calls == 1 for ldr in loaders.values())
    assert second == {}


def test_warm_never_raises_when_a_loader_fails(loaders: dict[str, _Loader]) -> None:
    """A transient MySQL failure must not turn into a worker restart loop."""
    loaders["cities"].boom = True
    timings = warm_mod.warm()

    # The failure is recorded and the other two still load.
    assert sorted(timings) == ["au", "cities", "countries"]
    assert loaders["au"].is_loaded() and loaders["countries"].is_loaded()
    assert warm_mod.is_warm() is False


def test_failed_load_is_logged_at_error(loaders: dict[str, _Loader]) -> None:
    loaders["au"].boom = True
    with capture_logs() as logs:
        warm_mod.warm()
    failures = [e for e in logs if e["event"] == "gazetteer_load_failed"]
    assert len(failures) == 1
    assert failures[0]["gazetteer"] == "au"
    assert failures[0]["log_level"] == "error"
    assert failures[0]["error"] == "RuntimeError: mysql went away"


def test_each_loader_timed_and_logged_at_info(loaders: dict[str, _Loader]) -> None:
    """The per-loader timings are the whole point: a slow warm has to be
    attributable to one gazetteer, not just a single total."""
    with capture_logs() as logs:
        warm_mod.warm()

    loaded = [e for e in logs if e["event"] == "gazetteer_loaded"]
    assert [e["gazetteer"] for e in loaded] == ["au", "cities", "countries"]
    assert all(e["log_level"] == "info" for e in loaded)
    assert all(isinstance(e["ms"], float) for e in loaded)
    assert [e["entries"] for e in loaded] == [2, 6, 9]

    total = [e for e in logs if e["event"] == "gazetteers_warm"]
    assert len(total) == 1
    assert total[0]["warm"] is True
    # Every loader's time is broken out on the summary line too.
    assert {"au_ms", "cities_ms", "countries_ms"} <= set(total[0])


def test_is_warm_false_before_warming(loaders: dict[str, _Loader]) -> None:
    assert warm_mod.is_warm() is False


def test_is_warm_handles_a_loader_without_the_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_is_loaded` must not explode on a plain function.

    `cached_gazetteer` supplies `is_loaded`, but nothing enforces that, and a
    health endpoint is the last place that should raise.
    """
    monkeypatch.setattr(warm_mod, "LOADERS", ((("plain"), lambda: object(), len),))
    assert warm_mod.is_warm() is False


def test_real_loaders_are_registered_in_dependency_order() -> None:
    """countries loads au and cities itself, so it must be warmed last or its
    timing absorbs theirs."""
    from locatron.gazetteer.au import load_au
    from locatron.gazetteer.cities import load_cities
    from locatron.gazetteer.countries import load_countries

    assert [name for name, _, _ in warm_mod.LOADERS] == ["au", "cities", "countries"]
    assert [ldr for _, ldr, _ in warm_mod.LOADERS] == [load_au, load_cities, load_countries]
