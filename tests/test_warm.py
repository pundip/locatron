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


@pytest.fixture(autouse=True)
def _stub_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the SQLite street mirror.

    warm() opens it for real, so without this every test here would need a
    built 54 MB file. The mirror's own behaviour is covered in
    tests/parse/test_street.py.
    """
    from locatron.db import local

    opened = {"yes": False}

    def connect() -> None:
        opened["yes"] = True

    monkeypatch.setattr(warm_mod.local, "is_open", lambda: opened["yes"])
    monkeypatch.setattr(warm_mod.local, "connect", connect)
    monkeypatch.setattr(
        warm_mod.local,
        "check_mirror",
        lambda: local.MirrorMeta(
            norm_version="1",
            snapshot_id="test",
            source_digest="d",
            row_count=1,
            built_at="now",
            path="stub",
        ),
    )


@pytest.fixture
def loaders(monkeypatch: pytest.MonkeyPatch) -> dict[str, _Loader]:
    made = {"au": _Loader(), "cities": _Loader(), "countries": _Loader()}
    monkeypatch.setattr(
        warm_mod,
        "LOADERS",
        tuple(
            (name, ldr, lambda _g, n=name: {"rows": len(n), "keys": 1})
            for name, ldr in made.items()
        ),
    )
    return made


def test_warm_loads_every_gazetteer_once(loaders: dict[str, _Loader]) -> None:
    timings = warm_mod.warm()
    assert sorted(timings) == ["au", "cities", "countries", "streets"]
    assert all(ldr.calls == 1 for ldr in loaders.values())
    assert warm_mod.is_warm() is True


def test_warm_is_idempotent(loaders: dict[str, _Loader]) -> None:
    """A second call must not reload. Nothing calls it twice today, but a
    worker that warmed already should not pay again if something does."""
    warm_mod.warm()
    # Once open, the mirror is not reopened either.
    second = warm_mod.warm()
    assert all(ldr.calls == 1 for ldr in loaders.values())
    assert second == {}


def test_warm_never_raises_when_a_loader_fails(loaders: dict[str, _Loader]) -> None:
    """A transient MySQL failure must not turn into a worker restart loop."""
    loaders["cities"].boom = True
    timings = warm_mod.warm()

    # The failure is recorded and the other two still load.
    assert sorted(timings) == ["au", "cities", "countries", "streets"]
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
    # Counts are splatted onto the line, not collapsed into one number.
    assert [e["rows"] for e in loaded] == [2, 6, 9]
    assert all(e["keys"] == 1 for e in loaded)

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


# ---------------------------------------------------------------------------
# count reporting
# ---------------------------------------------------------------------------


class _FakeAu:
    """Two locality rows under one key, plus aliases, mirroring the real shape."""

    by_id = {1: object(), 2: object(), 3: object()}
    by_norm = {"PERTH": (object(), object()), "ARANDA": (object(),)}
    aliases = {"NT WINNELLIE": (object(), object()), "MT ELIZA": (object(),)}


class _FakeCities:
    by_norm = {"ZURICH": (object(),), "SAO PAULO": (object(), object())}


class _FakeCountries:
    by_alpha3 = {"AUS": object(), "NZL": object()}
    tokens = {"AUSTRALIA": "AUS"}
    hints = {"AUSTRALIA": "AUS", "DOWN UNDER": "AUS", "NZ": "NZL"}


def test_au_counts_keep_rows_keys_and_aliases_apart() -> None:
    """The confusion this fixes: 3 rows under 2 keys is not 2 rows."""
    assert warm_mod._au_counts(_FakeAu()) == {
        "rows": 3,
        "keys": 2,
        "aliases": 3,
        "alias_keys": 2,
    }


def test_cities_counts_call_it_index_entries_not_rows() -> None:
    """A city indexed under two spellings is one row but two entries, so the
    label must not claim to be a row count."""
    assert warm_mod._cities_counts(_FakeCities()) == {"keys": 2, "index_entries": 3}


def test_countries_counts() -> None:
    assert warm_mod._countries_counts(_FakeCountries()) == {
        "rows": 2,
        "tokens": 1,
        "hints": 3,
    }


def test_every_loader_has_a_counts_callable_returning_a_dict() -> None:
    fakes = {"au": _FakeAu(), "cities": _FakeCities(), "countries": _FakeCountries()}
    for name, _, counts in warm_mod.LOADERS:
        out = counts(fakes[name])
        assert isinstance(out, dict) and out, name
        assert all(isinstance(v, int) for v in out.values()), name


# ---------------------------------------------------------------------------
# the street mirror is the one failure that propagates
# ---------------------------------------------------------------------------


def test_a_missing_mirror_fails_the_worker(
    loaders: dict[str, _Loader], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike a gazetteer load, this must not be swallowed. There is no lazy
    fallback for streets, so a worker that cannot open the mirror must refuse to
    start rather than quietly stop finding them."""
    from locatron.db import local

    def boom() -> None:
        raise local.MirrorError("street mirror missing at /nowhere")

    monkeypatch.setattr(warm_mod.local, "check_mirror", boom)
    with pytest.raises(local.MirrorError):
        warm_mod.warm()


def test_a_gazetteer_failure_is_still_swallowed(loaders: dict[str, _Loader]) -> None:
    """The contrast: a transient MySQL blip must not restart-loop the service."""
    loaders["cities"].boom = True
    warm_mod.warm()  # does not raise
    assert warm_mod.is_warm() is False
