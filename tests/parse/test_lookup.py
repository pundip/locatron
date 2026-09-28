"""G-NAF lookup tests.

The ladder, the tie-break and the substitution cap are exercised through injected
sources, so they run with no database and do not care whether the rows came from
MySQL or anywhere else. The rows in the fixtures are real, copied from
`address_ref`, and `test_fixture_rows_still_match_the_database` asserts they stay
faithful.

Only the tests about the query itself touch MySQL, and they skip when it is
unreachable.
"""

from __future__ import annotations

import pytest

from locatron.db import mysql
from locatron.gazetteer.au import LocalityRow
from locatron.parse.locality import Candidate, Hypothesis
from locatron.parse.lookup import (
    RANGE_MISMATCH_CONFIDENCE_CAP,
    SUBSTITUTED_TYPE_CONFIDENCE_CAP,
    AddressKey,
    GnafRecord,
    Granularity,
    best_without_unit,
    follow_alias,
    lookup,
    match_range,
    match_unit,
    row_by_pid,
    rows_for_number,
)
from locatron.parse.street import StreetHypothesis, StreetMatch, StreetRow, TypeSubstitution
from locatron.parse.tokens import Span
from locatron.resolve.scoring import MatchKind


def _db_available() -> bool:
    try:
        return bool(mysql.health().get("connected"))
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_available(), reason="ReferenceDB unreachable")


# ---------------------------------------------------------------------------
# fixtures: real rows, no database
# ---------------------------------------------------------------------------


def rec(**kw) -> GnafRecord:
    """A GnafRecord with every text column blank, as the table stores them."""
    base = dict(
        address_detail_pid="",
        address_label="",
        address_site_name="",
        building_name="",
        flat_type="",
        flat_number="",
        level_type="",
        level_number="",
        number_first="",
        number_last="",
        lot_number="",
        street_name="",
        street_type="",
        street_suffix="",
        locality_name="",
        state="",
        postcode="",
        alias_principal="P",
        principal_pid="",
        primary_secondary="",
        primary_pid="",
        geocode_type="",
        mb_code="",
        legal_parcel_id="",
        date_created="",
        lat=None,
        lng=None,
    )
    base.update(kw)
    return GnafRecord(**base)  # type: ignore[arg-type]


#: 1 SMITH ST, FITZROY: the 'P' building plus two 'S' units, as the table has it.
SMITH_1 = (
    rec(
        address_detail_pid="GAVIC422113667",
        address_label="1 SMITH ST, FITZROY VIC 3065",
        number_first="1",
        street_name="SMITH",
        street_type="ST",
        locality_name="FITZROY",
        state="VIC",
        postcode="3065",
        primary_secondary="P",
        lat=-37.80831533,
        lng=144.98250453,
    ),
    rec(
        address_detail_pid="GAVIC423408951",
        address_label="UNIT 5 1 SMITH ST, FITZROY VIC 3065",
        flat_type="UNIT",
        flat_number="5",
        number_first="1",
        street_name="SMITH",
        street_type="ST",
        locality_name="FITZROY",
        state="VIC",
        postcode="3065",
        primary_secondary="S",
        lat=-37.80831533,
        lng=144.98250453,
    ),
    rec(
        address_detail_pid="GAVIC423408952",
        address_label="UNIT 6 1 SMITH ST, FITZROY VIC 3065",
        flat_type="UNIT",
        flat_number="6",
        number_first="1",
        street_name="SMITH",
        street_type="ST",
        locality_name="FITZROY",
        state="VIC",
        postcode="3065",
        primary_secondary="S",
        lat=-37.80831533,
        lng=144.98250453,
    ),
)

#: 17-23 WILLS ST, MELBOURNE: a real range.
WILLS_17 = (
    rec(
        address_detail_pid="GAVIC421635951",
        address_label="17-23 WILLS ST, MELBOURNE VIC 3000",
        number_first="17",
        number_last="23",
        street_name="WILLS",
        street_type="ST",
        locality_name="MELBOURNE",
        state="VIC",
        postcode="3000",
        lat=-37.81089957,
        lng=144.95710626,
    ),
)

#: 12 ALICE ST, AMAROO is an alias row; its principal is 49 ROLLSTON ST.
ALICE_12 = (
    rec(
        address_detail_pid="GAACT714849932",
        address_label="12 ALICE ST, AMAROO ACT 2914",
        number_first="12",
        street_name="ALICE",
        street_type="ST",
        locality_name="AMAROO",
        state="ACT",
        postcode="2914",
        alias_principal="A",
        principal_pid="GAACT714849931",
        lat=-35.16673415,
        lng=149.13244879,
    ),
)
ROLLSTON_49 = rec(
    address_detail_pid="GAACT714849931",
    address_label="49 ROLLSTON ST, AMAROO ACT 2914",
    number_first="49",
    street_name="ROLLSTON",
    street_type="ST",
    locality_name="AMAROO",
    state="ACT",
    postcode="2914",
    alias_principal="P",
    lat=-35.16673415,
    lng=149.13244879,
)

STORE: dict[AddressKey, tuple[GnafRecord, ...]] = {
    ("FITZROY", "SMITH", "ST", "1"): SMITH_1,
    ("MELBOURNE", "WILLS", "ST", "17"): WILLS_17,
    ("AMAROO", "ALICE", "ST", "12"): ALICE_12,
}
PRINCIPALS = {"GAACT714849931": ROLLSTON_49}


def source(key: AddressKey) -> tuple[GnafRecord, ...]:
    return STORE.get(key, ())


def principal(pid: str) -> GnafRecord | None:
    return PRINCIPALS.get(pid)


# ---------------------------------------------------------------------------
# hypothesis builders
# ---------------------------------------------------------------------------


def _hyp(
    locality="FITZROY",
    state="VIC",
    postcode="3065",
    *,
    postal_only=False,
    lat=-37.8,
    lng=144.98,
    street_name="SMITH",
    street_type="ST",
    street=True,
    substituted: TypeSubstitution | None = None,
    street_lat=-37.79,
    street_lng=144.97,
) -> StreetHypothesis:
    row = LocalityRow(
        locality_id=1,
        locality=locality,
        state=state,
        postcode=postcode,
        address_count=100,
        street_count=5,
        is_postal_only=postal_only,
        in_gnaf=not postal_only,
        lat=lat,
        lng=lng,
        postcode_lat=lat,
        postcode_lng=lng,
    )
    loc = Hypothesis(
        candidate=Candidate(row=row, key=locality, match=MatchKind.EXACT),
        locality_span=Span(0, 1),
        score=1.0,
        signals={"base": 1.0},
        confidence=0.5,
    )
    match = None
    if street:
        srow = StreetRow(
            state=state,
            locality=locality,
            postcode=postcode,
            street_key=f"{street_name} {street_type}".strip(),
            street_name=street_name,
            street_type=street_type,
            street_suffix="",
            address_count=50,
            lat=street_lat,
            lng=street_lng,
        )
        match = StreetMatch(
            row=srow,
            span=Span(1, 3),
            name_score=100.0,
            reading="name+type",
            type_matched=substituted is None,
            type_mismatch=substituted is not None,
        )
    return StreetHypothesis(
        locality=loc,
        street=match,
        score=2.0,
        signals={"base": 1.0},
        unexplained=(),
        street_type_substituted=substituted,
    )


def _lookup(h, **kw):
    return lookup(h, source=source, principal=principal, **kw)


# ---------------------------------------------------------------------------
# the ladder
# ---------------------------------------------------------------------------


def test_postal_never_queries_gnaf() -> None:
    """G-NAF holds no PO boxes, so this is correctness, not just speed."""
    r = _lookup(_hyp(postal_only=True), number_first="45", po_box=True)
    assert r.granularity == Granularity.POSTAL
    assert r.record is None
    assert r.round_trips == 0
    assert any("no postal addresses" in w for w in r.warnings)


def test_a_postal_only_locality_is_postal_even_without_a_po_box() -> None:
    r = _lookup(_hyp(postal_only=True))
    assert r.granularity == Granularity.POSTAL
    assert r.round_trips == 0


def test_no_street_match_falls_back_to_the_locality_centroid() -> None:
    h = _hyp(street=False, lat=-38.09, lng=145.18)
    r = _lookup(h)
    assert r.granularity == Granularity.LOCALITY
    assert (r.lat, r.lng) == (-38.09, 145.18)
    assert r.record is None
    assert r.round_trips == 0


def test_a_street_with_no_number_uses_the_street_centroid() -> None:
    r = _lookup(_hyp(street_lat=-37.79, street_lng=144.97))
    assert r.granularity == Granularity.STREET
    assert (r.lat, r.lng) == (-37.79, 144.97)
    assert r.round_trips == 0, "a street answer needs no G-NAF query at all"


def test_a_number_that_exists_gives_address_granularity() -> None:
    r = _lookup(_hyp(), number_first="1")
    assert r.granularity == Granularity.ADDRESS
    assert r.record is not None
    assert r.record.address_label == "1 SMITH ST, FITZROY VIC 3065"
    assert r.round_trips == 1


def test_a_number_that_does_not_exist_falls_back_to_the_street() -> None:
    r = _lookup(_hyp(), number_first="99999")
    assert r.granularity == Granularity.STREET
    assert r.record is None
    assert any("no G-NAF row for number 99999" in w for w in r.warnings)
    assert r.round_trips == 1


def test_granularity_values_are_the_agreed_set() -> None:
    assert (
        Granularity.UNIT,
        Granularity.ADDRESS,
        Granularity.STREET,
        Granularity.LOCALITY,
        Granularity.POSTAL,
    ) == ("unit", "address", "street", "locality", "postal")


# ---------------------------------------------------------------------------
# units
# ---------------------------------------------------------------------------


def test_a_matching_unit_gives_unit_granularity() -> None:
    r = _lookup(_hyp(), number_first="1", unit="5")
    assert r.granularity == Granularity.UNIT
    assert r.record is not None and r.record.flat_number == "5"
    assert r.record.primary_secondary == "S", "a unit is a secondary row"
    assert r.round_trips == 1


def test_a_missing_unit_returns_the_building_with_a_warning() -> None:
    """The building is a good answer; a wrong flat number should not discard it."""
    r = _lookup(_hyp(), number_first="1", unit="999")
    assert r.granularity == Granularity.ADDRESS
    assert r.record is not None and r.record.flat_number == ""
    assert r.record.primary_secondary == "P"
    assert any("unit 999 not found" in w for w in r.warnings)


def test_unit_matching_ignores_the_indicator() -> None:
    """Ranking by PRIMARY_SECONDARY first would pick the 'P' building and miss
    every flat, since units are 'S' rows."""
    assert match_unit(SMITH_1, "5").primary_secondary == "S"
    assert match_unit(SMITH_1, "6").flat_number == "6"
    assert match_unit(SMITH_1, "7") is None
    assert match_unit(SMITH_1, "") is None


# ---------------------------------------------------------------------------
# tie-break
# ---------------------------------------------------------------------------


def test_without_a_unit_the_building_wins_not_a_flat() -> None:
    best = best_without_unit(SMITH_1)
    assert best is not None
    assert best.primary_secondary == "P"
    assert best.flat_number == ""


@pytest.mark.parametrize(
    ("indicators", "expected"),
    [
        (["P", "", "S"], "P"),
        (["", "S"], ""),
        (["S"], "S"),
        (["S", ""], ""),
    ],
)
def test_primary_secondary_preference_is_p_then_blank_then_s(
    indicators: list[str], expected: str
) -> None:
    """Blank is ordinary, not missing: it is 65% of the table."""
    rows = [
        rec(address_detail_pid=f"pid{i}", primary_secondary=v) for i, v in enumerate(indicators)
    ]
    assert best_without_unit(rows).primary_secondary == expected


def test_a_principal_outranks_an_alias() -> None:
    rows = [
        rec(address_detail_pid="a", alias_principal="A", primary_secondary="P"),
        rec(address_detail_pid="p", alias_principal="P", primary_secondary="S"),
    ]
    assert best_without_unit(rows).address_detail_pid == "p"


def test_the_tie_break_is_deterministic() -> None:
    rows = [rec(address_detail_pid="zzz"), rec(address_detail_pid="aaa")]
    assert best_without_unit(rows).address_detail_pid == "aaa"
    assert best_without_unit(list(reversed(rows))).address_detail_pid == "aaa"


def test_no_rows_gives_no_choice() -> None:
    assert best_without_unit([]) is None


# ---------------------------------------------------------------------------
# ranges
# ---------------------------------------------------------------------------


def test_an_exact_range_matches() -> None:
    r = _lookup(
        _hyp(locality="MELBOURNE", postcode="3000", street_name="WILLS"),
        number_first="17",
        number_last="23",
    )
    assert r.granularity == Granularity.ADDRESS
    assert r.record is not None
    assert (r.record.number_first, r.record.number_last) == ("17", "23")
    assert not any("range" in w for w in r.warnings)


def test_a_range_with_no_exact_row_falls_back_to_the_first_half() -> None:
    r = _lookup(
        _hyp(locality="MELBOURNE", postcode="3000", street_name="WILLS"),
        number_first="17",
        number_last="99",
    )
    assert r.granularity == Granularity.ADDRESS
    assert any("no G-NAF row for the range 17-99" in w for w in r.warnings)
    assert r.round_trips == 1, "the fallback reuses rows already in hand"


def test_match_range_reports_whether_it_was_exact() -> None:
    assert match_range(WILLS_17, "23")[1] is True
    assert match_range(WILLS_17, "99")[1] is False
    assert match_range(WILLS_17, None)[1] is True
    assert match_range([], "23") == (None, False)


def test_an_alpha_suffix_stays_inside_number_first() -> None:
    """G-NAF stores 6C in NUMBER_FIRST with NUMBER_LAST blank, so nothing splits
    it off and the key is the whole value."""
    rows = (rec(address_detail_pid="x", number_first="6C", street_name="SMITH", street_type="ST"),)
    store = {("FITZROY", "SMITH", "ST", "6C"): rows}
    r = lookup(_hyp(), number_first="6C", source=lambda k: store.get(k, ()), principal=principal)
    assert r.granularity == Granularity.ADDRESS
    assert r.record is not None and r.record.number_first == "6C"


# ---------------------------------------------------------------------------
# aliases
# ---------------------------------------------------------------------------


def test_an_alias_row_is_returned_as_itself_not_as_its_principal() -> None:
    """Somebody who types '12 Alice Street Amaroo' wants 12 Alice Street. The
    alias row carries that street, that number and its own coordinates, so it is
    the answer; the principal comes back beside it as the id to join on."""
    r = _lookup(
        _hyp(locality="AMAROO", state="ACT", postcode="2914", street_name="ALICE"),
        number_first="12",
    )
    assert r.granularity == Granularity.ADDRESS
    assert r.record is not None
    assert r.record.address_detail_pid == "GAACT714849932"
    assert r.record.address_label == "12 ALICE ST, AMAROO ACT 2914"
    assert r.record.alias_principal == "A", "the returned record is the alias itself"
    assert (r.lat, r.lng) == (-35.16673415, 149.13244879), "the alias row's coordinates"

    assert r.principal is not None
    assert r.principal.pid == "GAACT714849931"
    assert r.principal.address == "49 ROLLSTON ST, AMAROO ACT 2914"
    assert r.canonical_pid == "GAACT714849931", "dedupe on the principal, not the alias"
    assert "the input address is an alias of 49 ROLLSTON ST, AMAROO ACT 2914" in r.warnings
    assert r.round_trips == 2, "the follow is the second trip, and the budget is 2"


def test_a_principal_match_is_its_own_canonical_pid() -> None:
    r = _lookup(_hyp(), number_first="1")
    assert r.record is not None
    assert r.principal is None
    assert r.canonical_pid == r.record.address_detail_pid


def test_below_address_granularity_there_is_no_canonical_pid() -> None:
    """Street and locality answers come from centroids, so there is no G-NAF row
    to identify and nothing for a consumer to join on."""
    r = _lookup(_hyp(street=False))
    assert r.granularity == Granularity.LOCALITY
    assert r.canonical_pid == ""
    assert r.principal is None


def test_a_dangling_principal_keeps_the_alias_rather_than_returning_nothing() -> None:
    orphan = rec(
        address_detail_pid="orphan",
        alias_principal="A",
        principal_pid="GONE",
        address_label="9 NOWHERE ST",
        number_first="9",
    )
    got, ref, warnings = follow_alias(orphan, lambda pid: None)
    assert got.address_detail_pid == "orphan"
    assert ref is None, "nothing to point at, so no principal block"
    assert any("could not be found" in w for w in warnings)


def test_a_principal_row_is_not_followed() -> None:
    got, ref, warnings = follow_alias(SMITH_1[0], lambda pid: pytest.fail("should not follow"))
    assert got is SMITH_1[0]
    assert ref is None
    assert warnings == ()


# ---------------------------------------------------------------------------
# the substituted-type cap
# ---------------------------------------------------------------------------


def _substituted() -> TypeSubstitution:
    return TypeSubstitution(written_as="STREET", input_type="ST", matched_type="GR")


def test_a_substituted_type_caps_confidence_at_address_level() -> None:
    h = _hyp(substituted=_substituted())
    r = _lookup(h, number_first="1")
    assert r.granularity == Granularity.ADDRESS
    assert r.confidence_cap == SUBSTITUTED_TYPE_CONFIDENCE_CAP
    assert any("street type substituted" in w for w in r.warnings)


def test_the_substitution_warning_names_both_types() -> None:
    r = _lookup(_hyp(substituted=_substituted()), number_first="1")
    warning = next(w for w in r.warnings if "substituted" in w)
    assert "'STREET'" in warning
    assert "ST" in warning
    assert "GR" in warning


def test_the_cap_applies_at_unit_level_too() -> None:
    r = _lookup(_hyp(substituted=_substituted()), number_first="1", unit="5")
    assert r.granularity == Granularity.UNIT
    assert r.confidence_cap == SUBSTITUTED_TYPE_CONFIDENCE_CAP


def test_no_substitution_means_no_cap() -> None:
    r = _lookup(_hyp(), number_first="1")
    assert r.confidence_cap is None
    assert not any("substituted" in w for w in r.warnings)


def test_the_cap_is_a_ceiling_below_one() -> None:
    assert 0.0 < SUBSTITUTED_TYPE_CONFIDENCE_CAP < 1.0
    assert 0.0 < RANGE_MISMATCH_CONFIDENCE_CAP < 1.0


def _wills(**kw):
    """A lookup against 17-23 WILLS ST, the only stored row at NUMBER_FIRST 17."""
    return _lookup(
        _hyp(locality="MELBOURNE", state="VIC", postcode="3000", street_name="WILLS"),
        number_first="17",
        **kw,
    )


def test_a_range_that_does_not_match_the_stored_one_caps_confidence() -> None:
    """'17-99' asked about 17 through 99 and got a row that runs 17 to 23. The
    first number is exact, the extent is not the one asked for."""
    r = _wills(number_last="99")
    assert r.granularity == Granularity.ADDRESS
    assert r.record is not None and r.record.number_last == "23"
    assert r.confidence_cap == RANGE_MISMATCH_CONFIDENCE_CAP


def test_the_range_warning_names_the_range_asked_for_and_the_one_stored() -> None:
    warning = next(w for w in _wills(number_last="99").warnings if "range" in w)
    assert "17-99" in warning, "the range the input asked for"
    assert "17-23" in warning, "the range actually stored"


def test_the_exact_range_is_not_a_mismatch() -> None:
    r = _wills(number_last="23")
    assert r.record is not None and r.record.number_last == "23"
    assert r.confidence_cap is None
    assert not any("range" in w for w in r.warnings)


def test_a_bare_number_matching_a_stored_range_is_not_a_mismatch() -> None:
    """'17 Wills Street' asks nothing about extent, so landing on the stored
    17-23 answers the question that was asked. Capping this would penalise every
    single-number input that happens to sit at the head of a range."""
    r = _wills()
    assert r.record is not None and r.record.number_last == "23"
    assert r.confidence_cap is None
    assert not any("range" in w for w in r.warnings)


def test_a_range_against_a_row_with_no_stored_range_still_mismatches() -> None:
    """1 SMITH ST carries no NUMBER_LAST at all, so '1-5' did not get its extent
    either. The warning has no stored range to name."""
    r = _lookup(_hyp(), number_first="1", number_last="5")
    assert r.granularity == Granularity.ADDRESS
    assert r.confidence_cap == RANGE_MISMATCH_CONFIDENCE_CAP
    warning = next(w for w in r.warnings if "range" in w)
    assert "1-5" in warning
    assert "whose stored range is" not in warning


def test_two_doubts_take_the_tighter_cap() -> None:
    """A substituted type and a range mismatch at once must not let the looser
    range cap raise the ceiling the substitution set."""
    r = _lookup(
        _hyp(
            locality="MELBOURNE",
            state="VIC",
            postcode="3000",
            street_name="WILLS",
            substituted=_substituted(),
        ),
        number_first="17",
        number_last="99",
    )
    assert RANGE_MISMATCH_CONFIDENCE_CAP > SUBSTITUTED_TYPE_CONFIDENCE_CAP, "premise"
    assert r.confidence_cap == SUBSTITUTED_TYPE_CONFIDENCE_CAP
    assert any("substituted" in w for w in r.warnings)
    assert any("range" in w for w in r.warnings)


# ---------------------------------------------------------------------------
# the result carries its provenance
# ---------------------------------------------------------------------------


def test_the_result_carries_the_hypothesis_it_came_from() -> None:
    h = _hyp()
    r = _lookup(h, number_first="1")
    assert r.hypothesis is h


def test_round_trips_never_exceed_two() -> None:
    for kwargs in [
        {},
        {"number_first": "1"},
        {"number_first": "1", "unit": "5"},
        {"number_first": "1", "unit": "999"},
        {"number_first": "99999"},
        {"number_first": "45", "po_box": True},
    ]:
        r = _lookup(_hyp(), **kwargs)
        assert r.round_trips <= 2, (kwargs, r.round_trip_labels)


def test_address_level_flag() -> None:
    assert _lookup(_hyp(), number_first="1").is_address_level is True
    assert _lookup(_hyp(), number_first="1", unit="5").is_address_level is True
    assert _lookup(_hyp()).is_address_level is False
    assert _lookup(_hyp(street=False)).is_address_level is False


# ---------------------------------------------------------------------------
# the query itself
# ---------------------------------------------------------------------------


@needs_db
def test_fixture_rows_still_match_the_database() -> None:
    """These fixtures are only useful while they are faithful."""
    live = {r.address_detail_pid: r for r in rows_for_number(("FITZROY", "SMITH", "ST", "1"))}
    for expected in SMITH_1:
        assert expected.address_detail_pid in live, expected.address_label
        got = live[expected.address_detail_pid]
        assert got.flat_number == expected.flat_number
        assert got.primary_secondary == expected.primary_secondary
        assert got.alias_principal == expected.alias_principal


@needs_db
def test_the_real_query_returns_units_and_the_building_together() -> None:
    rows = rows_for_number(("FITZROY", "SMITH", "ST", "1"))
    assert len(rows) >= 3
    assert any(r.primary_secondary == "P" and not r.flat_number for r in rows)
    assert any(r.primary_secondary == "S" and r.flat_number for r in rows)


@needs_db
def test_the_real_query_returns_nothing_for_a_number_that_does_not_exist() -> None:
    assert rows_for_number(("FITZROY", "SMITH", "ST", "99999")) == ()


@needs_db
def test_coordinates_come_back_as_floats_not_blank_strings() -> None:
    """CAST('' AS DECIMAL) is 0, not NULL, so the NULLIF guard matters even though
    this table has no blanks."""
    rows = rows_for_number(("FITZROY", "SMITH", "ST", "1"))
    assert all(isinstance(r.lat, float) and isinstance(r.lng, float) for r in rows)


@needs_db
def test_the_real_alias_keeps_its_own_row_and_names_its_principal() -> None:
    rows = rows_for_number(("AMAROO", "ALICE", "ST", "12"))
    assert rows and rows[0].alias_principal == "A"
    got, ref, warnings = follow_alias(rows[0], row_by_pid)
    assert got is rows[0], "the alias row is the answer"
    assert ref is not None
    assert ref.pid == "GAACT714849931"
    assert ref.address == "49 ROLLSTON ST, AMAROO ACT 2914"
    assert warnings == (f"the input address is an alias of {ref.address}",)


@needs_db
def test_row_by_pid_on_a_missing_pid_is_none() -> None:
    assert row_by_pid("NOT-A-REAL-PID") is None
    assert row_by_pid("") is None
