"""Component extractor tests, table-driven.

Every AU row of `tests/golden/golden.csv` appears here, keyed by its `note`
column so a failure names the case the golden set was probing. The world rows
are covered too, because an extractor that fires on 'Las Vegas' would poison a
hypothesis for an address that is not Australian at all.

These are pure-function tests. Nothing here touches the database.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from locatron.parse.components import find_po_boxes, find_postcodes
from locatron.parse.tokens import tokenize

GOLDEN = Path(__file__).resolve().parent.parent / "golden" / "golden.csv"


def _golden_rows() -> list[dict[str, str]]:
    """Every golden row.

    Keyed on `input`, not `note`: three notes contain an unquoted comma
    ("postal only, no G-NAF match"), so csv.DictReader truncates them at the
    comma and the rest spills into the restkey. `locatron golden` reads the file
    the same way and does not care, because it only uses `note` for --filter,
    but a test table keyed on it would silently miss rows.
    """
    with GOLDEN.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


ROWS = _golden_rows()
BY_INPUT = {r["input"]: r for r in ROWS}
ALL_INPUTS = tuple(r["input"] for r in ROWS)

#: The Australian rows, derived from the data rather than retyped. Note this
#: includes the AU *place* rows ('Perth', 'VIC') as well as the address ones,
#: because an extractor firing on those would poison a hypothesis just as badly.
AU_INPUTS = tuple(r["input"] for r in ROWS if r["expected_country"] == "AUS")
NON_AU_INPUTS = tuple(r["input"] for r in ROWS if r["expected_country"] != "AUS")


def test_golden_set_shape_is_unchanged() -> None:
    """Guards the tables below against drift in golden.csv."""
    assert len(ROWS) == 30, f"golden.csv now has {len(ROWS)} rows"
    assert len(AU_INPUTS) == 20
    assert len(NON_AU_INPUTS) == 10


# ---------------------------------------------------------------------------
# postcodes
# ---------------------------------------------------------------------------

#: Every golden input -> the four-digit postcodes it should yield. All 30 rows,
#: so a new row cannot be added without an expectation.
EXPECTED_POSTCODES: dict[str, list[str]] = {
    # --- AU address rows, the ones phase 2 has to parse ---
    "65 clifton park drive 3201 carrum downs": ["3201"],
    "65 Clifton Park Dr Carrum Downs VIC 3201": ["3201"],
    "5/12 Smith Street Fitzroy VIC 3065": ["3065"],
    "Unit 5 12 Smith Street Fitzroy 3065": ["3065"],
    "14-40 Wills Street Melbourne VIC 3000": ["3000"],
    "Clifton Park Drive Carrum Downs": [],
    "Carrum Downs VIC": [],
    "3201": ["3201"],
    "PO Box 45 World Square NSW 2002": ["2002"],
    "Ryde NSW 2112": ["2112"],
    "Hamilton Crescent Ryde NSW 2112": ["2112"],
    "St Kilda East VIC": [],
    "Ku-ring-gai NSW": [],
    # --- AU place rows ---
    "Greater Melbourne": [],
    "Sydney Australia": [],
    "Melbourne": [],
    "Perth": [],
    "Victoria Australia": [],
    "VIC": [],
    "Australia": [],
    # --- world and unresolved rows ---
    "New York": [],
    "Las Vegas": [],
    "Delhi": [],
    "Springfield": [],
    "Remote / Work from home": [],
    "": [],
    "asdfghjkl": [],
    "London": [],
    "Zurich": [],
    "Sao Paulo": [],
}


def test_every_golden_row_has_a_postcode_expectation() -> None:
    assert set(EXPECTED_POSTCODES) == set(ALL_INPUTS)


@pytest.mark.parametrize("raw", ALL_INPUTS)
def test_postcodes_in_every_golden_row(raw: str) -> None:
    got = [c.postcode for c in find_postcodes(tokenize(raw)) if not c.padded]
    assert got == EXPECTED_POSTCODES[raw]


@pytest.mark.parametrize("raw", NON_AU_INPUTS)
def test_no_postcode_in_world_or_unresolved_rows(raw: str) -> None:
    """An extractor that fires on 'Las Vegas' would poison a hypothesis."""
    assert find_postcodes(tokenize(raw)) == ()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3000", ["3000"]),
        ("VIC 3000", ["3000"]),
        # NT keeps its leading zero. Coercing to int would give 800.
        ("Darwin NT 0800", ["0800"]),
        ("NT 0899", ["0899"]),
        ("0801", ["0801"]),
        # Not four digits.
        ("65", []),
        ("655", []),
        ("12345", []),
        ("6C", []),
        ("14-40", []),
        ("5/12", []),
        ("ABCD", []),
        # Several candidates, all returned, in token order.
        ("3000 then 3201", ["3000", "3201"]),
        ("2112 RYDE NSW 2112", ["2112", "2112"]),
    ],
)
def test_postcode_recognition(raw: str, expected: list[str]) -> None:
    got = [c.postcode for c in find_postcodes(tokenize(raw)) if not c.padded]
    assert got == expected


def test_postcode_is_a_string_not_an_int() -> None:
    (c,) = find_postcodes(tokenize("Darwin NT 0800"))
    assert c.postcode == "0800"
    assert isinstance(c.postcode, str)
    assert len(c.postcode) == 4


def test_postcode_span_points_at_its_own_token() -> None:
    ts = tokenize("65 CLIFTON PARK DRIVE 3201 CARRUM DOWNS")
    (c,) = find_postcodes(ts)
    assert c.span.start == 4
    assert ts.text_of(c.span) == "3201"


def test_postcode_position_is_not_assumed() -> None:
    """Both golden orderings must yield the same postcode from different slots."""
    before = find_postcodes(tokenize("65 clifton park drive 3201 carrum downs"))
    after = find_postcodes(tokenize("65 Clifton Park Dr Carrum Downs VIC 3201"))
    assert [c.postcode for c in before] == [c.postcode for c in after] == ["3201"]
    assert before[0].span.start == 4
    assert after[0].span.start == 7


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Darwin NT 800", ["0800"]),
        ("899", ["0899"]),
        ("100", ["0100"]),
    ],
)
def test_three_digit_tokens_come_back_padded_and_flagged(
    raw: str, expected: list[str]
) -> None:
    """Recovered, but marked, so a scorer can weight them down or drop them."""
    cands = find_postcodes(tokenize(raw))
    assert [c.postcode for c in cands] == expected
    assert all(c.padded for c in cands)


def test_padded_candidates_are_separable_from_real_ones() -> None:
    cands = find_postcodes(tokenize("Level 3 800 Bourke St Melbourne 3000"))
    assert [(c.postcode, c.padded) for c in cands] == [("0800", True), ("3000", False)]


def test_no_postcodes_in_an_empty_stream() -> None:
    assert find_postcodes(tokenize("")) == ()


# ---------------------------------------------------------------------------
# PO boxes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "kind", "number", "span"),
    [
        ("PO BOX 45", "PO", "45", (0, 3)),
        ("PO Box 45", "PO", "45", (0, 3)),
        ("po box 45", "PO", "45", (0, 3)),
        # normalize() turns the periods into separators: ['P','O','BOX','45'].
        ("P.O. BOX 45", "PO", "45", (0, 4)),
        ("P.O.BOX 45", "PO", "45", (0, 4)),
        ("GPO BOX 45", "GPO", "45", (0, 3)),
        ("G.P.O. Box 1", "GPO", "1", (0, 5)),
        ("POBOX 45", "PO", "45", (0, 2)),
        ("GPOBOX 7", "GPO", "7", (0, 2)),
        # Alpha suffix on the box number.
        ("PO BOX 45A", "PO", "45A", (0, 3)),
    ],
)
def test_po_box_forms(raw: str, kind: str, number: str, span: tuple[int, int]) -> None:
    (b,) = find_po_boxes(tokenize(raw))
    assert (b.kind, b.number) == (kind, number)
    assert (b.span.start, b.span.end) == span


def test_po_box_in_the_golden_row() -> None:
    ts = tokenize("PO Box 45 World Square NSW 2002")
    (b,) = find_po_boxes(ts)
    assert (b.kind, b.number) == ("PO", "45")
    # Consumes exactly 'PO BOX 45', leaving the locality and state behind.
    assert ts.text_of(b.span) == "PO BOX 45"
    assert tuple(t.text for t in ts.remaining([b.span])) == ("WORLD", "SQUARE", "NSW", "2002")


def test_gpo_is_not_read_as_a_bare_o_box() -> None:
    """Longest prefix first, or 'G P O BOX' matches the 'O BOX' tail."""
    (b,) = find_po_boxes(tokenize("G.P.O. BOX 12"))
    assert b.kind == "GPO"
    assert b.span.start == 0


@pytest.mark.parametrize(
    "raw",
    [
        "PO BOX",  # no number
        "PO BOX ABC",  # number is not number-shaped
        "BOX 45",  # no PO prefix
        "45 BOX PO",  # wrong order
        "POST OFFICE 45",
        "65 Smith Street Fitzroy VIC 3065",
        "",
    ],
)
def test_not_a_po_box(raw: str) -> None:
    assert find_po_boxes(tokenize(raw)) == ()


def test_two_po_boxes_are_both_returned() -> None:
    boxes = find_po_boxes(tokenize("PO BOX 1 and GPO BOX 2"))
    assert [(b.kind, b.number) for b in boxes] == [("PO", "1"), ("GPO", "2")]
    assert boxes[0].span.end <= boxes[1].span.start


def test_po_box_number_is_not_also_swallowed_by_the_next_scan() -> None:
    """The scan must step past the number, not re-enter on it."""
    boxes = find_po_boxes(tokenize("PO BOX 45 PO BOX 46"))
    assert [(b.number, b.span.start) for b in boxes] == [("45", 0), ("46", 3)]


@pytest.mark.parametrize(
    "raw", [r for r in AU_INPUTS + NON_AU_INPUTS if "Box" not in r]
)
def test_no_po_box_in_rows_that_have_none(raw: str) -> None:
    """Every golden row except the PO Box one must yield nothing."""
    assert find_po_boxes(tokenize(raw)) == ()
