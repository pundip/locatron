"""The feedback loop's write path.

The selection rule is tested against a fake response, with no database involved.
The write itself is tested for the property that actually matters: that it cannot
take a request down with it.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from locatron.resolve import unresolved as log
from locatron.schemas import Granularity, MatchMethod, ResolveResponse

#: This module tests `record()` itself, so it opts out of conftest's fake. MySQL
#: is still made unreachable from inside it, so no row can be written.
pytestmark = pytest.mark.real_unresolved_log


def _db_available() -> bool:
    try:
        from locatron.db import mysql

        return bool(mysql.health().get("connected"))
    except Exception:
        return False


#: The pool tests read @@transaction_read_only from a live server, and probe the
#: write pool with an UPDATE that matches no rows. None of them writes data.
needs_db_for_pools = pytest.mark.skipif(not _db_available(), reason="ReferenceDB unreachable")

#: Proves a connection can write without changing anything. MySQL refuses it with
#: error 1792 inside a read-only transaction, and `WHERE 1 = 0` means it can never
#: touch a row -- which matters, because inserting one is exactly what conftest
#: forbids and what put nine synthetic rows in the real table.
#:
#: The alternative instruments do not work here. A fake cannot detect this bug at
#: all: the failure happens at the server, not in Python. And a disposable table is
#: impossible -- the service account is granted SELECT plus INSERT/UPDATE on two
#: tables, so CREATE TABLE and even CREATE TEMPORARY TABLE are denied.
_NO_OP_WRITE = sa.text("UPDATE locatron_unresolved SET hit_count = hit_count WHERE 1 = 0")


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

    monkeypatch.setattr(log, "_engine", fail)
    assert log.record(_response(Granularity.UNRESOLVED, normalized="   ")) is False
    assert called is False


# ---------------------------------------------------------------------------
# rule 1: a failed write never fails the request
# ---------------------------------------------------------------------------


def test_a_database_failure_is_swallowed_and_logged(monkeypatch, caplog) -> None:
    def explode():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(log, "_engine", explode)
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


# ---------------------------------------------------------------------------
# the test-suite guard itself
# ---------------------------------------------------------------------------


def test_the_write_seam_is_the_only_way_to_reach_mysql() -> None:
    """`record()` must go through `_engine()` and nothing else.

    That indirection is what lets conftest make writing impossible per test
    without patching the shared `mysql` module, which every read depends on. A
    direct `mysql.get_engine()` call inside record() would slip past the guard.
    """
    import inspect

    body = inspect.getsource(log.record)
    assert "_engine()" in body
    assert "mysql.get_engine" not in body


@needs_db_for_pools
def test_a_gnaf_lookup_does_not_leave_the_pool_unable_to_write() -> None:
    """The bug this module's warnings were hiding.

    `SET SESSION TRANSACTION READ ONLY` is session-scoped, so issuing it on a
    borrowed connection left it set when that connection returned to the pool. Any
    resolve that dived into address_ref poisoned a connection, and the next
    feedback write to land on it failed with "Cannot execute statement in a READ
    ONLY transaction" -- logged and swallowed, because the write is best effort, so
    the table just stayed empty.

    Two pools now: reads are read-only for the life of the connection, writes use
    a pool that is never touched.
    """
    from locatron.db import mysql
    from locatron.parse.lookup import rows_for_number

    rows_for_number(("CARRUM DOWNS", "CLIFTON PARK", "DR", "65"))

    # The write pool can still open a writable transaction afterwards.
    with mysql.get_engine().begin() as conn:
        conn.execute(sa.text("SELECT 1"))
        assert conn.execute(sa.text("SELECT @@transaction_read_only")).scalar() == 0


@needs_db_for_pools
def test_the_read_pool_cannot_write_at_all() -> None:
    """Stronger than the statement-scoped version it replaced: read connections are
    read-only for their whole life, so a read path that starts writing fails
    immediately and always rather than depending on pool checkout order."""
    from locatron.db import mysql

    with mysql.get_read_engine().connect() as conn:
        assert conn.execute(sa.text("SELECT @@transaction_read_only")).scalar() == 1


@needs_db_for_pools
def test_repeated_lookups_never_leave_the_write_pool_unable_to_write() -> None:
    """Regression for the bug fixed in c25558c, interleaved and repeated.

    `SET SESSION TRANSACTION READ ONLY` is session-scoped. Issued on a connection
    borrowed from the shared pool, it stayed set when that connection went back, so
    a resolve that dived into address_ref left a connection the feedback write could
    not use. The write is best effort -- logged and swallowed -- so the only symptom
    was locatron_unresolved staying empty, and which resolve broke which write
    depended on pool checkout order, so it looked intermittent.

    This reproduces the real sequence rather than the mechanism: an AU address
    resolve that hits address_ref, then the write, over and over, more times than
    the pool holds connections. Several connections are opened first so the pool
    really has more than one, and the write pool is checked on every pass.

    Both of those are load-bearing, not caution. Run against the pre-fix code this
    fails on **pass 3**, not pass 0: the first three passes drew connections that
    happened to be clean. A single lookup followed by a single check -- the obvious
    way to write this test -- would have passed on the broken code and proved
    nothing.

    Asserted with a no-op UPDATE, for the reasons on _NO_OP_WRITE.
    """
    from locatron.config import get_settings
    from locatron.db import mysql
    from locatron.resolve.pipeline import resolve_one

    pool_size = get_settings().mysql_pool_size

    # Force distinct connections into the write pool, so this does not pass by
    # happening to reuse one connection that was never poisoned.
    held = [mysql.get_engine().connect() for _ in range(pool_size)]
    for conn in held:
        conn.close()

    for i in range(3 * pool_size):
        answer = resolve_one("65 Clifton Park Dr Carrum Downs VIC 3201", record_unresolved=False)
        assert answer.granularity is Granularity.ADDRESS, (
            f"pass {i}: premise failed, the resolve did not reach address_ref"
        )

        with mysql.get_engine().begin() as conn:
            read_only = conn.execute(sa.text("SELECT @@transaction_read_only")).scalar()
            assert read_only == 0, (
                f"pass {i}: drew a read-only connection from the write pool, so the "
                f"feedback log could not have written"
            )
            # The write the resolver would really have attempted.
            assert conn.execute(_NO_OP_WRITE).rowcount == 0


@needs_db_for_pools
def test_the_read_pool_stays_read_only_across_checkouts() -> None:
    """The other half: read connections are read-only for their whole life, not for
    one statement. A read path that starts writing has to fail every time rather
    than depending on which connection it drew."""
    from locatron.config import get_settings
    from locatron.db import mysql
    from locatron.parse.lookup import rows_for_number

    for _ in range(2 * get_settings().mysql_pool_size):
        rows_for_number(("CARRUM DOWNS", "CLIFTON PARK", "DR", "65"))
        with mysql.get_read_engine().connect() as conn:
            assert conn.execute(sa.text("SELECT @@transaction_read_only")).scalar() == 1
        with (
            pytest.raises(sa.exc.OperationalError, match="READ ONLY"),
            mysql.get_read_engine().begin() as conn,
        ):
            conn.execute(_NO_OP_WRITE)
