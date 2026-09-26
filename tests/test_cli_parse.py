"""Smoke test for `locatron parse`.

A developer command, so this checks that it runs and that its shape is what the
report claims, not that the parser is correct -- that is covered by
tests/parse/. It needs both stores because it runs the real stages: the gazetteer
from MySQL and the street mirror from SQLite.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from locatron.cli import app
from locatron.db import mysql


def _db_available() -> bool:
    try:
        return bool(mysql.health().get("connected"))
    except Exception:
        return False


def _mirror_available() -> bool:
    try:
        from locatron.db import local

        local.read_meta()
        return True
    except Exception:
        return False


needs_stores = pytest.mark.skipif(
    not (_db_available() and _mirror_available()),
    reason="needs ReferenceDB and a built street mirror",
)

runner = CliRunner()


@needs_stores
def test_parse_reports_every_section_for_one_input() -> None:
    result = runner.invoke(app, ["parse", "65 clifton park drive 3201 carrum downs"])
    assert result.exit_code == 0, result.output
    out = result.output

    for section in ("input", "tokens", "components", "hypotheses", "breakdown"):
        assert section in out, section
    # Components, with the postcode padded and the number kept apart from it.
    assert "3201@4" in out
    assert "number     65" in out
    # The street is found and nothing is left over.
    assert "CARRUM DOWNS" in out
    assert "CLIFTON PARK" in out
    assert "granularity=street" in out
    assert "unexplained  -" in out
    # The breakdown ends with a total that names itself.
    assert "= joint score" in out


@needs_stores
def test_parse_separates_blocks_with_a_blank_line() -> None:
    result = runner.invoke(app, ["parse", "Carrum Downs VIC", "Ryde NSW 2112"])
    assert result.exit_code == 0, result.output
    # One block per input, and the second starts after a blank line.
    assert result.output.count("input      ") == 2
    assert "\n\ninput      " in result.output


@needs_stores
def test_parse_is_deterministic() -> None:
    """Two runs of the same inputs must print the same bytes, or the report is
    not something you can diff."""
    args = ["parse", "12 Clifton Street 3201", "Perth 7300", "800"]
    first = runner.invoke(app, args)
    second = runner.invoke(app, args)
    assert first.exit_code == 0 and second.exit_code == 0
    assert first.output == second.output


@needs_stores
def test_parse_shows_a_type_substitution_when_there_is_one() -> None:
    result = runner.invoke(app, ["parse", "12 Clifton Street 3201"])
    assert result.exit_code == 0, result.output
    assert "substituted=STREET(ST)->GR" in result.output
    assert "street_type_mismatch" in result.output


@needs_stores
def test_parse_handles_input_that_resolves_to_nothing() -> None:
    """Must report rather than raise, like the rest of the resolver."""
    result = runner.invoke(app, ["parse", "asdfghjkl"])
    assert result.exit_code == 0, result.output
    assert "hypotheses none" in result.output


@needs_stores
def test_parse_loads_the_gazetteer_once_for_many_inputs() -> None:
    """Ten inputs must not cost ten warm-ups. Asserted through the loader's own
    cache rather than by timing, which would be flaky."""
    from locatron.gazetteer.au import load_au

    load_au.cache_clear()  # type: ignore[attr-defined]
    assert load_au.is_loaded() is False  # type: ignore[attr-defined]
    result = runner.invoke(app, ["parse", *["Carrum Downs VIC"] * 10])
    assert result.exit_code == 0, result.output
    assert load_au.is_loaded() is True  # type: ignore[attr-defined]
    assert result.output.count("input      ") == 10
