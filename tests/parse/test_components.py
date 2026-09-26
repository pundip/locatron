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

from locatron.parse.components import (
    find_po_boxes,
    find_postcodes,
    find_street_numbers,
    find_units_and_levels,
)
from locatron.parse.tokens import Span, tokenize

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
def test_three_digit_tokens_come_back_padded_and_flagged(raw: str, expected: list[str]) -> None:
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


@pytest.mark.parametrize("raw", [r for r in AU_INPUTS + NON_AU_INPUTS if "Box" not in r])
def test_no_po_box_in_rows_that_have_none(raw: str) -> None:
    """Every golden row except the PO Box one must yield nothing."""
    assert find_po_boxes(tokenize(raw)) == ()


# ---------------------------------------------------------------------------
# units and levels
# ---------------------------------------------------------------------------

#: The only two golden rows with a sub-dwelling, as (kind, value, keyword, hint).
EXPECTED_UNITS: dict[str, list[tuple[str, str, str | None, str | None]]] = {
    "5/12 Smith Street Fitzroy VIC 3065": [("unit", "5", None, "12")],
    "Unit 5 12 Smith Street Fitzroy 3065": [("unit", "5", "UNIT", None)],
}


@pytest.mark.parametrize("raw", ALL_INPUTS)
def test_units_in_every_golden_row(raw: str) -> None:
    """28 of the 30 rows must yield nothing. A keyword list that over-matches
    steals tokens from the street name, so this is the guard against that."""
    got = [
        (u.kind, u.value, u.keyword, u.street_number_hint)
        for u in find_units_and_levels(tokenize(raw))
    ]
    assert got == EXPECTED_UNITS.get(raw, [])


@pytest.mark.parametrize(
    ("raw", "kind", "value", "keyword", "span"),
    [
        ("UNIT 5", "unit", "5", "UNIT", (0, 2)),
        ("Unit 5", "unit", "5", "UNIT", (0, 2)),
        ("U 5", "unit", "5", "UNIT", (0, 2)),
        ("FLAT 5", "unit", "5", "FLAT", (0, 2)),
        ("Flat 12A", "unit", "12A", "FLAT", (0, 2)),
        ("SHOP 2", "unit", "2", "SHOP", (0, 2)),
        ("L 3", "level", "3", "LEVEL", (0, 2)),
        ("LEVEL 3", "level", "3", "LEVEL", (0, 2)),
        ("Level 12", "level", "12", "LEVEL", (0, 2)),
        # Letter-led unit values, as used on ground floors.
        ("UNIT G01", "unit", "G01", "UNIT", (0, 2)),
    ],
)
def test_keyword_forms(
    raw: str, kind: str, value: str, keyword: str, span: tuple[int, int]
) -> None:
    (u,) = find_units_and_levels(tokenize(raw))
    assert (u.kind, u.value, u.keyword) == (kind, value, keyword)
    assert (u.span.start, u.span.end) == span
    assert u.street_number_hint is None


@pytest.mark.parametrize(
    ("raw", "unit", "hint"),
    [
        ("5/12", "5", "12"),
        ("5/12 Smith Street", "5", "12"),
        ("12A/34", "12A", "34"),
        # The right-hand side may itself be a range.
        ("1/14-40", "1", "14-40"),
        ("2/6C", "2", "6C"),
    ],
)
def test_slash_form(raw: str, unit: str, hint: str) -> None:
    (u,) = find_units_and_levels(tokenize(raw))
    assert (u.kind, u.value, u.keyword, u.street_number_hint) == ("unit", unit, None, hint)
    assert (u.span.start, u.span.end) == (0, 1), "the slash form is one token"


def test_keyword_plus_slash_yields_both_readings() -> None:
    """'UNIT 5/12' is the keyword form and the slash form at once. Both are
    returned, longest span first, and the scorer picks."""
    got = find_units_and_levels(tokenize("UNIT 5/12 Smith Street"))
    assert [(u.value, u.keyword, u.street_number_hint) for u in got] == [
        ("5", "UNIT", "12"),
        ("5", None, "12"),
    ]
    assert len(got[0].span) == 2
    assert len(got[1].span) == 1
    assert got[0].span.overlaps(got[1].span)


@pytest.mark.parametrize(
    "raw",
    [
        "UNIT",  # keyword with nothing after it
        "LEVEL",
        "UNIT SMITH",  # next token is not a value
        "LEVEL STREET",
        "5 UNIT",  # wrong order
        "14-40 Wills Street",  # a range is not a unit
        "Ku-ring-gai NSW",  # hyphens, no digits
        "Remote / Work from home",  # a bare slash token
        "/",
        "3201",
        "",
    ],
)
def test_not_a_unit_or_level(raw: str) -> None:
    assert find_units_and_levels(tokenize(raw)) == ()


def test_unit_and_level_together() -> None:
    got = find_units_and_levels(tokenize("Level 3 Shop 2 Smith Street"))
    assert [(u.kind, u.value) for u in got] == [("level", "3"), ("unit", "2")]
    assert got[0].span.end <= got[1].span.start


def test_spelled_unit_leaves_the_street_number_behind() -> None:
    """'UNIT 5 12 SMITH ST': the extractor claims 'UNIT 5' and the 12 is left
    for the street number extractor."""
    ts = tokenize("Unit 5 12 Smith Street Fitzroy 3065")
    (u,) = find_units_and_levels(ts)
    assert ts.text_of(u.span) == "UNIT 5"
    assert tuple(t.text for t in ts.remaining([u.span])) == (
        "12",
        "SMITH",
        "STREET",
        "FITZROY",
        "3065",
    )


def test_slash_form_consumes_only_its_own_token() -> None:
    ts = tokenize("5/12 Smith Street Fitzroy VIC 3065")
    (u,) = find_units_and_levels(ts)
    assert ts.text_of(u.span) == "5/12"
    assert tuple(t.text for t in ts.remaining([u.span])) == (
        "SMITH",
        "STREET",
        "FITZROY",
        "VIC",
        "3065",
    )


# ---------------------------------------------------------------------------
# street numbers
# ---------------------------------------------------------------------------

#: Every golden row -> (number_first, number_last, from_slash) candidates.
#: Four-digit postcodes appear here too, deliberately: '3201' is a street number
#: shape as well as a postcode, and only the gazetteer can say which.
EXPECTED_NUMBERS: dict[str, list[tuple[str, str | None, bool]]] = {
    "65 clifton park drive 3201 carrum downs": [("65", None, False), ("3201", None, False)],
    "65 Clifton Park Dr Carrum Downs VIC 3201": [("65", None, False), ("3201", None, False)],
    "5/12 Smith Street Fitzroy VIC 3065": [("12", None, True), ("3065", None, False)],
    "Unit 5 12 Smith Street Fitzroy 3065": [
        ("5", None, False),
        ("12", None, False),
        ("3065", None, False),
    ],
    "14-40 Wills Street Melbourne VIC 3000": [("14", "40", False), ("3000", None, False)],
    "3201": [("3201", None, False)],
    "PO Box 45 World Square NSW 2002": [("45", None, False), ("2002", None, False)],
    "Ryde NSW 2112": [("2112", None, False)],
    "Hamilton Crescent Ryde NSW 2112": [("2112", None, False)],
}


@pytest.mark.parametrize("raw", ALL_INPUTS)
def test_street_numbers_in_every_golden_row(raw: str) -> None:
    got = [
        (n.number_first, n.number_last, n.from_slash) for n in find_street_numbers(tokenize(raw))
    ]
    assert got == EXPECTED_NUMBERS.get(raw, [])


@pytest.mark.parametrize(
    ("raw", "first"),
    [
        ("65", "65"),
        ("65 Smith St", "65"),
        ("1", "1"),
        ("123456", "123456"),
        # Alpha suffix stays inside number_first, as address_ref stores it.
        ("6C", "6C"),
        ("59B Moynihan St", "59B"),
        ("12A", "12A"),
        ("1A", "1A"),
    ],
)
def test_single_numbers(raw: str, first: str) -> None:
    n = find_street_numbers(tokenize(raw))[0]
    assert (n.number_first, n.number_last) == (first, None)
    assert n.is_range is False
    assert n.from_slash is False


@pytest.mark.parametrize(
    ("raw", "first", "last"),
    [
        ("14-40", "14", "40"),
        ("14-40 Wills Street", "14", "40"),
        ("100-104", "100", "104"),
        ("1-3", "1", "3"),
        # Suffixes on both ends of a range.
        ("1A-1C", "1A", "1C"),
        ("6C-8", "6C", "8"),
        ("8-10B", "8", "10B"),
    ],
)
def test_ranges(raw: str, first: str, last: str) -> None:
    n = find_street_numbers(tokenize(raw))[0]
    assert (n.number_first, n.number_last) == (first, last)
    assert n.is_range is True


@pytest.mark.parametrize(
    ("raw", "first", "last"),
    [
        ("5/12", "12", None),
        ("5/12 Smith Street", "12", None),
        ("12A/34", "34", None),
        # The right of the slash may be a range.
        ("1/14-40", "14", "40"),
        ("2/6C", "6C", None),
    ],
)
def test_numbers_from_the_slash_form(raw: str, first: str, last: str | None) -> None:
    n = find_street_numbers(tokenize(raw))[0]
    assert (n.number_first, n.number_last) == (first, last)
    assert n.from_slash is True


@pytest.mark.parametrize(
    "raw",
    [
        # The case a looser range pattern would break: hyphens survive
        # normalisation, so the locality arrives here intact.
        "Ku-ring-gai NSW",
        "Ku-ring-gai",
        "KUR-RING-GAI",
        "St Kilda East VIC",
        "Clifton Park Drive Carrum Downs",
        "Carrum Downs VIC",
        "SMITH",
        "VIC",
        "Australia",
        "",
        "/",
        "Remote / Work from home",
        # Not number-shaped on both sides of the hyphen.
        "A-1",
        "1-B",
        "ONE-TWO",
        # Too many digits to be a street number.
        "1234567",
        # A letter-led unit value is not a street number.
        "G01",
    ],
)
def test_not_a_street_number(raw: str) -> None:
    assert find_street_numbers(tokenize(raw)) == ()


def test_hyphenated_locality_is_never_a_range() -> None:
    """The single most likely way to break this extractor."""
    assert find_street_numbers(tokenize("Ku-ring-gai NSW")) == ()
    assert find_street_numbers(tokenize("14-40 Ku-ring-gai Road"))[0].number_last == "40"


def test_range_and_single_are_distinguishable() -> None:
    (rng,) = find_street_numbers(tokenize("14-40"))
    (single,) = find_street_numbers(tokenize("6C"))
    assert rng.is_range and rng.number_last == "40"
    assert not single.is_range and single.number_last is None


def test_span_points_at_the_number_token() -> None:
    ts = tokenize("65 CLIFTON PARK DRIVE")
    (n,) = find_street_numbers(ts)
    assert ts.text_of(n.span) == "65"
    assert tuple(t.text for t in ts.remaining([n.span])) == ("CLIFTON", "PARK", "DRIVE")


def test_slash_span_is_the_whole_token_shared_with_the_unit() -> None:
    """One token satisfies two components, so both spans are the same token."""
    ts = tokenize("5/12 Smith Street")
    (u,) = find_units_and_levels(ts)
    (n,) = find_street_numbers(ts)
    assert u.span == n.span
    assert ts.text_of(n.span) == "5/12"
    assert n.from_slash is True


# ---------------------------------------------------------------------------
# the extractors together
# ---------------------------------------------------------------------------


def test_worked_example_leaves_the_street_behind() -> None:
    """CLAUDE.md's worked example. With the number, postcode and locality
    claimed, CLIFTON PARK DRIVE comes back as one run."""
    ts = tokenize("65 clifton park drive 3201 carrum downs")
    number = find_street_numbers(ts)[0]
    postcode = find_postcodes(ts)[0]
    assert ts.text_of(number.span) == "65"
    assert ts.text_of(postcode.span) == "3201"
    # The locality span is the gazetteer's job; stand in for it here.
    locality = Span(5, 7)
    runs = ts.runs([number.span, postcode.span, locality])
    assert runs == (Span(1, 4),)
    assert ts.text_of(runs[0]) == "CLIFTON PARK DRIVE"


def test_spelled_unit_row_yields_unit_number_and_postcode() -> None:
    ts = tokenize("Unit 5 12 Smith Street Fitzroy 3065")
    (unit,) = find_units_and_levels(ts)
    numbers = find_street_numbers(ts)
    (postcode,) = find_postcodes(ts)
    assert (unit.value, unit.keyword) == ("5", "UNIT")
    # The 5 inside 'UNIT 5' is also number-shaped, so it is proposed too. The
    # scorer prefers the reading where the unit keyword claims it.
    assert [n.number_first for n in numbers] == ["5", "12", "3065"]
    assert postcode.postcode == "3065"
    street = ts.runs([unit.span, Span(2, 3), postcode.span, Span(5, 6)])
    assert ts.text_of(street[0]) == "SMITH STREET"


def test_po_box_row_yields_no_street_number_once_the_box_is_claimed() -> None:
    ts = tokenize("PO Box 45 World Square NSW 2002")
    (box,) = find_po_boxes(ts)
    left = [n for n in find_street_numbers(ts) if not n.span.overlaps(box.span)]
    assert [n.number_first for n in left] == ["2002"]
