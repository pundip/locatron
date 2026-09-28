"""Test-wide guards.

The one here that matters: no test may write to `locatron_unresolved`. That table
is the feedback loop -- reviewing it by `hit_count` and promoting real entries
into `locatron_locality_alias` is what makes the resolver better over months --
and a test suite that files synthetic rows into it buries the real traffic it
exists to surface. Rows also cannot be removed by the service account, which has
INSERT and UPDATE on that table but not DELETE, so a stray write needs a DBA to
undo.

Passing `record_unresolved=False` at each call site was not enough. There are 35
`resolve_one()` calls across the suite that do not pass it, the API tests reach
the resolver through an endpoint and cannot pass it at all, and the next test
written would reintroduce the hole silently. So the guard is autouse and applies
to every test, whether or not it knows this table exists.
"""

from __future__ import annotations

import pytest

from locatron.resolve import unresolved as unresolved_log
from locatron.schemas import ResolveResponse


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_unresolved_log: exercise unresolved.record() itself rather than a fake. "
        "MySQL is still made unreachable from it, so no write is possible.",
    )


@pytest.fixture(autouse=True)
def unresolved_writes(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> list[ResolveResponse]:
    """Replace the feedback-table write with a fake, and hand back what it caught.

    Autouse, so a test gets the guard without asking. Request it by name to assert
    that the resolver *tried* to log something:

        def test_something(unresolved_writes):
            resolve_one("asdfghjkl")
            assert [r.query for r in unresolved_writes] == ["asdfghjkl"]

    A test marked `real_unresolved_log` keeps the real `record()` -- it is testing
    that function -- but has MySQL made unreachable from inside it, so the worst it
    can do is exercise the error path it already asserts.
    """
    caught: list[ResolveResponse] = []

    if request.node.get_closest_marker("real_unresolved_log"):

        def refuse():
            raise AssertionError(
                "a test reached MySQL from the unresolved log; "
                "record() must never write during tests"
            )

        # `unresolved._engine` and not `mysql.get_engine`: the latter is the engine
        # every read in the process shares, so patching it takes the gazetteer down
        # too and the resolver fails long before it reaches the log.
        monkeypatch.setattr(unresolved_log, "_engine", refuse)
        return caught

    def fake_record(
        response: ResolveResponse,
        *,
        unexplained: tuple[str, ...] = (),
        norm_key: str | None = None,
    ) -> bool:
        # Mirrors the real selection rule so a test observing this list sees what
        # would actually have been written, not every response that went past.
        if not unresolved_log.is_worth_recording(response, unexplained):
            return False
        caught.append(response)
        return True

    monkeypatch.setattr(unresolved_log, "record", fake_record)
    return caught
