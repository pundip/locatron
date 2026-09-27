"""The feedback loop's write path.

The selection rule is tested against a fake response, with no database involved.
The write itself is tested for the property that actually matters: that it cannot
take a request down with it.
"""

from __future__ import annotations

import pytest

from locatron.resolve import unresolved as log
from locatron.schemas import Granularity, MatchMethod, ResolveResponse


def _response(
    granularity: Granularity,
    *,
    query: str = "somewhere odd",
    normalized: str = "SOMEWHERE ODD",
    confidence: float = 0.3,
) -> ResolveResponse:
    return ResolveResponse(
        query=query,
        normalized=normalized,
        resolved=granularity is not Granularity.UNRESOLVED,
        granularity=granularity,
        confidence=confidence,
        match_method=MatchMethod.LOCALITY_FUZZY,
        norm_version="1",
    )


# ---------------------------------------------------------------------------
# what gets recorded
# ---------------------------------------------------------------------------


def test_nothing_resolved_is_always_recorded() -> None:
    assert log.is_worth_recording(_response(Granularity.UNRESOLVED)) is True


def test_nothing_resolved_is_recorded_even_with_no_leftover_tokens() -> None:
    """An input that resolved to nothing is the clearest signal there is, whether
    or not the parser had tokens left over."""
    assert log.is_worth_recording(_response(Granularity.UNRESOLVED), ()) is True


@pytest.mark.parametrize(
    "granularity",
    [Granularity.LOCALITY, Granularity.ADMIN1, Granularity.CITY, Granularity.COUNTRY],
)
def test_locality_or_coarser_with_leftover_tokens_is_recorded(
    granularity: Granularity,
) -> None:
    assert log.is_worth_recording(_response(granularity), ("REGION",)) is True


@pytest.mark.parametrize(
    "granularity",
    [Granularity.LOCALITY, Granularity.ADMIN1, Granularity.CITY, Granularity.COUNTRY],
)
def test_locality_or_coarser_with_nothing_left_over_is_not(granularity: Granularity) -> None:
    """'Carrum Downs VIC' resolving to a locality is the correct and complete
    answer. Recording it would bury the rows that are actionable."""
    assert log.is_worth_recording(_response(granularity), ()) is False


@pytest.mark.parametrize(
    "granularity",
    [Granularity.UNIT, Granularity.ADDRESS, Granularity.STREET, Granularity.POSTCODE],
)
def test_a_precise_answer_is_never_recorded(granularity: Granularity) -> None:
    """Even with tokens left over: an address that matched G-NAF exactly is not a
    gap in the gazetteer."""
    assert log.is_worth_recording(_response(granularity), ("SUITE",)) is False


def test_an_empty_key_is_not_recorded(monkeypatch) -> None:
    """An empty input normalises to an empty key, which is not a distinct thing
    anyone can act on and would collide with every other blank."""
    called = False

    def fail(*_a, **_k):
        nonlocal called
        called = True
        raise AssertionError("should not reach the database")

    monkeypatch.setattr(log.mysql, "get_engine", fail)
    assert log.record(_response(Granularity.UNRESOLVED, normalized="   ")) is False
    assert called is False


# ---------------------------------------------------------------------------
# rule 1: a failed write never fails the request
# ---------------------------------------------------------------------------


def test_a_database_failure_is_swallowed_and_logged(monkeypatch, caplog) -> None:
    def explode():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(log.mysql, "get_engine", explode)
    assert log.record(_response(Granularity.UNRESOLVED)) is False


def test_a_write_failure_does_not_stop_a_resolve(monkeypatch) -> None:
    """The property this whole module is built around, asserted against a record()
    that raises rather than swallowing -- because the no-raise invariant must not
    depend on this module staying bug-free."""
    from locatron.resolve import pipeline

    def explode(*_a, **_k):
        raise RuntimeError("table is gone")

    monkeypatch.setattr(pipeline.unresolved_log, "record", explode)
    r = pipeline.resolve_one("asdfghjkl", record_unresolved=True)
    assert r.granularity is Granularity.UNRESOLVED
    assert 0.0 <= r.confidence <= 1.0
    assert any("unresolved log failed" in w for w in r.warnings), "and it says so"


def test_the_upsert_decides_best_before_it_updates_best() -> None:
    """MySQL evaluates ON DUPLICATE KEY UPDATE left to right. If best_confidence
    were assigned before best_granularity, every comparison after it would read
    the new value and be trivially false, and the columns would never move
    together again."""
    sql = str(log._UPSERT)
    body = sql.split("ON DUPLICATE KEY UPDATE", 1)[1]
    assert body.index("best_granularity") < body.index("best_confidence  =")
    assert body.index("match_method") < body.index("best_confidence  =")


def test_it_writes_to_one_table_and_that_table_is_the_log() -> None:
    """The service account may write to locatron_unresolved and nothing else. A
    typo here would fail at the grant, but late and confusingly."""
    sql = str(log._UPSERT).upper()
    assert sql.count("INSERT INTO") == 1
    assert "LOCATRON_UNRESOLVED" in sql
    for forbidden in ("ADDRESS_REF", "AUSTRALIANPOSTCODES", "CITIES", "COUNTRIES"):
        assert forbidden not in sql


def test_long_values_are_truncated_rather_than_rejected() -> None:
    """A 4KB scraped string is exactly the kind of input worth recording, and the
    columns are varchar(255)."""
    assert log._MAX_LEN == 255
