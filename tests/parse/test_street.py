"""Street matching tests.

Street matching goes through a fixture store rather than the database. That is
deliberate twice over: these tests run offline, and they survive the swap from
MySQL to the SQLite mirror CLAUDE.md calls for, because what they exercise is
`StreetSource` rather than any particular query. The fixture rows are real, lifted
from `locatron_street`, and `test_fixture_matches_the_database` asserts they stay
faithful.

The few tests that must touch the database are the ones about the database: that
`streets_for_many()` batches, and that the type table still covers every code an
upstream refresh might have added.
"""

from __future__ import annotations

import pytest

from locatron.db import mysql
from locatron.gazetteer.au import LocalityRow
from locatron.parse import scoring
from locatron.parse.locality import Candidate, Hypothesis
from locatron.parse.street import (
    SELF_SPELLED_TYPES,
    TYPE_SPELLINGS,
    UNVERIFIED_TYPES,
    LocalityKey,
    StreetRow,
    codes_for_token,
    known_types,
    match_streets,
    name_similarity,
    resolve_streets,
    streets_for,
    streets_for_many,
    type_forms,
)
from locatron.parse.tokens import Span, tokenize
from locatron.resolve.scoring import MatchKind


def _db_available() -> bool:
    try:
        return bool(mysql.health().get("connected"))
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_available(), reason="ReferenceDB unreachable")

#: Span is frozen, so one shared default is safe to reuse across calls.
_FIRST_TOKEN = Span(0, 1)


# ---------------------------------------------------------------------------
# fixture store
# ---------------------------------------------------------------------------

#: Real rows from locatron_street, captured so these tests run offline.
#: `test_fixture_matches_the_database` asserts they are still faithful.
FIXTURE: dict[LocalityKey, tuple[StreetRow, ...]] = {
    ("VIC", "CARRUM DOWNS", "3201"): (
        StreetRow("VIC", "CARRUM DOWNS", "3201", "HALL RD", "HALL", "RD", "", 477),
        StreetRow(
            "VIC",
            "CARRUM DOWNS",
            "3201",
            "FRANKSTON GARDENS DR",
            "FRANKSTON GARDENS",
            "DR",
            "",
            220,
        ),
        StreetRow("VIC", "CARRUM DOWNS", "3201", "LYREBIRD DR", "LYREBIRD", "DR", "", 201),
        StreetRow("VIC", "CARRUM DOWNS", "3201", "BALLARTO RD", "BALLARTO", "RD", "", 178),
        StreetRow("VIC", "CARRUM DOWNS", "3201", "CLIFTON GR", "CLIFTON", "GR", "", 149),
        StreetRow("VIC", "CARRUM DOWNS", "3201", "CLIFTON PARK DR", "CLIFTON PARK", "DR", "", 84),
        StreetRow("VIC", "CARRUM DOWNS", "3201", "RICHMOND AV", "RICHMOND", "AV", "", 37),
        StreetRow("VIC", "CARRUM DOWNS", "3201", "HOLLY PL", "HOLLY", "PL", "", 8),
    ),
    ("VIC", "CARRUM", "3197"): (
        StreetRow("VIC", "CARRUM", "3197", "STATION ST", "STATION", "ST", "", 90),
        StreetRow("VIC", "CARRUM", "3197", "PARKSIDE BVD", "PARKSIDE", "BVD", "", 20),
    ),
    ("NSW", "RYDE", "2112"): (
        StreetRow("NSW", "RYDE", "2112", "BLAXLAND RD", "BLAXLAND", "RD", "", 1120),
        StreetRow("NSW", "RYDE", "2112", "HAMILTON CR", "HAMILTON", "CR", "", 894),
        StreetRow("NSW", "RYDE", "2112", "CAMERON CR", "CAMERON", "CR", "", 60),
    ),
    ("VIC", "FITZROY", "3065"): (
        StreetRow("VIC", "FITZROY", "3065", "BRUNSWICK ST", "BRUNSWICK", "ST", "", 690),
        StreetRow("VIC", "FITZROY", "3065", "SMITH ST", "SMITH", "ST", "", 549),
        StreetRow("VIC", "FITZROY", "3065", "LITTLE SMITH ST", "LITTLE SMITH", "ST", "", 40),
    ),
    ("VIC", "MELBOURNE", "3000"): (
        StreetRow("VIC", "MELBOURNE", "3000", "COLLINS ST", "COLLINS", "ST", "", 3000),
        StreetRow("VIC", "MELBOURNE", "3000", "WILLS ST", "WILLS", "ST", "", 895),
    ),
    ("NSW", "SMITHFIELD", "2164"): (
        StreetRow(
            "NSW", "SMITHFIELD", "2164", "THE HORSLEY DRIVE", "THE HORSLEY DRIVE", "", "", 586
        ),
        StreetRow("NSW", "SMITHFIELD", "2164", "THE BOULEVARDE", "THE BOULEVARDE", "", "", 60),
    ),
    ("VIC", "SOUTH YARRA", "3141"): (
        StreetRow("VIC", "SOUTH YARRA", "3141", "TOORAK RD", "TOORAK", "RD", "", 900),
        StreetRow("VIC", "SOUTH YARRA", "3141", "SIMMONS ST", "SIMMONS", "ST", "", 454),
        StreetRow("VIC", "SOUTH YARRA", "3141", "SIMMONS CT", "SIMMONS", "CT", "", 94),
    ),
    ("VIC", "BENTLEIGH EAST", "3165"): (
        StreetRow("VIC", "BENTLEIGH EAST", "3165", "CENTRE RD", "CENTRE", "RD", "", 700),
        StreetRow(
            "VIC", "BENTLEIGH EAST", "3165", "CHESTERVILLE RD", "CHESTERVILLE", "RD", "", 206
        ),
        StreetRow("VIC", "BENTLEIGH EAST", "3165", "CHESTERVILLE DR", "CHESTERVILLE", "DR", "", 57),
    ),
}


def fixture_source(keys):
    """A `StreetSource` over FIXTURE. What every matching test goes through."""
    return {k: FIXTURE.get(k, ()) for k in keys}


def rows_for(state: str, locality: str, postcode: str) -> tuple[StreetRow, ...]:
    return FIXTURE[(state, locality, postcode)]


def _hyp(
    locality: str,
    state: str,
    postcode: str,
    *,
    address_count: int = 1000,
    locality_span: Span = _FIRST_TOKEN,
    state_span: Span | None = None,
    postcode_span: Span | None = None,
    score: float = 1.0,
) -> Hypothesis:
    """A locality hypothesis built by hand, so street tests need no gazetteer."""
    row = LocalityRow(
        locality_id=abs(hash((locality, state, postcode))) % 100_000,
        locality=locality,
        state=state,
        postcode=postcode,
        address_count=address_count,
        street_count=10,
        is_postal_only=False,
        in_gnaf=True,
        lat=None,
        lng=None,
        postcode_lat=None,
        postcode_lng=None,
    )
    return Hypothesis(
        candidate=Candidate(row=row, key=locality, match=MatchKind.EXACT),
        locality_span=locality_span,
        score=score,
        signals={"base": score},
        confidence=0.5,
        state_span=state_span,
        postcode_span=postcode_span,
    )


# ---------------------------------------------------------------------------
# the type table
# ---------------------------------------------------------------------------


def test_the_three_type_groups_are_disjoint() -> None:
    assert not (set(TYPE_SPELLINGS) & SELF_SPELLED_TYPES)
    assert not (set(TYPE_SPELLINGS) & UNVERIFIED_TYPES)
    assert not (SELF_SPELLED_TYPES & UNVERIFIED_TYPES)


def test_known_types_is_the_union() -> None:
    assert known_types() == set(TYPE_SPELLINGS) | SELF_SPELLED_TYPES | UNVERIFIED_TYPES
    assert len(known_types()) == 196


@pytest.mark.parametrize(
    ("code", "spelled"),
    [
        ("DR", "DRIVE"),
        ("CR", "CRESCENT"),
        ("CR", "CRES"),
        ("ST", "STREET"),
        ("RD", "ROAD"),
        ("AV", "AVENUE"),
        ("AV", "AVE"),
        ("CT", "COURT"),
        ("PL", "PLACE"),
        ("TCE", "TERRACE"),
    ],
)
def test_abbreviations_map_both_directions(code: str, spelled: str) -> None:
    """The required pairs, each way round."""
    assert spelled in type_forms(code)
    assert code in type_forms(code)
    assert code in codes_for_token(spelled)
    assert code in codes_for_token(code)


def test_a_self_spelled_code_denotes_itself() -> None:
    assert codes_for_token("LANE") == {"LANE"}
    assert type_forms("LANE") == {"LANE"}


def test_an_unverified_code_is_accepted_as_written_only() -> None:
    assert codes_for_token("BIDI") == {"BIDI"}
    assert type_forms("BIDI") == {"BIDI"}


def test_an_unknown_token_denotes_no_type() -> None:
    """This is what sends 'THE HORSLEY DRIVE' to the whole-name reading."""
    assert codes_for_token("HORSLEY") == frozenset()
    assert codes_for_token("") == frozenset()


def test_drive_does_not_also_denote_road() -> None:
    """The near-miss that fuzzy could not separate."""
    assert codes_for_token("DRIVE") == {"DR"}
    assert codes_for_token("CRESCENT") == {"CR"}
    assert "CT" not in codes_for_token("CRESCENT")
    assert "ST" not in codes_for_token("CRESCENT")


@needs_db
def test_type_table_covers_every_code_in_the_database() -> None:
    """Fails loudly when an upstream refresh introduces a new code, rather than
    letting it silently mismatch."""
    from sqlalchemy import text

    with mysql.session_scope() as s:
        codes = {
            r[0]
            for r in s.execute(
                text("SELECT DISTINCT street_type FROM locatron_street WHERE street_type <> ''")
            )
        }
    missing = codes - known_types()
    assert not missing, f"street types absent from the table: {sorted(missing)}"


# ---------------------------------------------------------------------------
# name similarity
# ---------------------------------------------------------------------------


def test_neither_metric_separates_the_type_codes() -> None:
    """Worth pinning, because it is easy to believe Levenshtein fixes this. It
    does not: CR, CT and ST all score identically against CRESCENT. The type is
    settled by TYPE_SPELLINGS, which is why name and type are scored apart."""
    scores = {name_similarity("HAMILTON CRESCENT", f"HAMILTON {t}") for t in ("CR", "CT", "ST")}
    assert len(scores) == 1


def test_levenshtein_does_not_over_score_a_differing_tail() -> None:
    """What Levenshtein is actually for. A prefix-weighted metric scores
    'CLIFTON PARK' against 'CLIFTON' at 91.67 and 'SMITH' against 'SMITHFIELD'
    at 90.00, treating a shorter or longer street name as the same street."""
    assert name_similarity("CLIFTON PARK", "CLIFTON") == pytest.approx(58.33, abs=0.01)
    assert name_similarity("SMITH", "SMITHFIELD") == pytest.approx(50.0)
    # Both land at or below the threshold, where a prefix metric would sail past.
    assert name_similarity("SMITH", "SMITHFIELD") < scoring.NAME_SIMILARITY_MIN


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [("SMITH", "SMITH", 100.0), ("", "SMITH", 0.0), ("SMITH", "", 0.0)],
)
def test_name_similarity_edges(a: str, b: str, expected: float) -> None:
    assert name_similarity(a, b) == expected


def test_transposition_costs_more_than_a_prefix_metric_would() -> None:
    assert name_similarity("SMTIH", "SMITH") == pytest.approx(60.0)
    assert name_similarity("SMTIH", "SMITH") >= scoring.NAME_SIMILARITY_MIN


# ---------------------------------------------------------------------------
# match_streets
# ---------------------------------------------------------------------------


def _match(text: str, state: str, locality: str, postcode: str):
    toks = tuple(text.split())
    return match_streets(toks, Span(0, len(toks)), rows_for(state, locality, postcode))


@pytest.mark.parametrize(
    ("text", "loc", "expected_key"),
    [
        # spelled input against an abbreviated key, via the table
        ("CLIFTON PARK DRIVE", ("VIC", "CARRUM DOWNS", "3201"), "CLIFTON PARK DR"),
        ("CLIFTON PARK DR", ("VIC", "CARRUM DOWNS", "3201"), "CLIFTON PARK DR"),
        ("HAMILTON CRESCENT", ("NSW", "RYDE", "2112"), "HAMILTON CR"),
        ("HAMILTON CR", ("NSW", "RYDE", "2112"), "HAMILTON CR"),
        ("SMITH STREET", ("VIC", "FITZROY", "3065"), "SMITH ST"),
        ("WILLS STREET", ("VIC", "MELBOURNE", "3000"), "WILLS ST"),
        ("BALLARTO ROAD", ("VIC", "CARRUM DOWNS", "3201"), "BALLARTO RD"),
        ("FRANKSTON GARDENS DRIVE", ("VIC", "CARRUM DOWNS", "3201"), "FRANKSTON GARDENS DR"),
    ],
)
def test_spelled_types_reach_the_abbreviated_key(
    text: str, loc: tuple[str, str, str], expected_key: str
) -> None:
    m = _match(text, *loc)[0]
    assert m.street_key == expected_key
    assert m.type_matched is True
    assert m.type_mismatch is False
    assert m.name_score == pytest.approx(100.0)


def test_the_park_ambiguity_resolves_to_the_whole_key() -> None:
    """CLAUDE.md's case: PARK is itself a valid street type, so a right-to-left
    type peel gives name=CLIFTON, type=PARK, leftover DRIVE. Matching the whole
    key against the stored name and type instead gives CLIFTON PARK DR."""
    m = _match("CLIFTON PARK DRIVE", "VIC", "CARRUM DOWNS", "3201")[0]
    assert m.street_key == "CLIFTON PARK DR"
    assert m.row.street_name == "CLIFTON PARK"
    assert m.row.street_type == "DR"


def test_blank_type_matches_on_the_whole_name() -> None:
    """THE HORSLEY DRIVE is a name ending in DRIVE with no type at all."""
    m = _match("THE HORSLEY DRIVE", "NSW", "SMITHFIELD", "2164")[0]
    assert m.street_key == "THE HORSLEY DRIVE"
    assert m.row.street_type == ""
    assert m.reading == "whole-name"
    assert m.name_score == pytest.approx(100.0)


def test_a_misspelled_name_still_matches() -> None:
    m = _match("CLFITON PARK DR", "VIC", "CARRUM DOWNS", "3201")[0]
    assert m.street_key == "CLIFTON PARK DR"
    assert scoring.NAME_SIMILARITY_MIN <= m.name_score < 100.0


def test_a_name_absent_from_the_locality_matches_nothing() -> None:
    assert _match("WOOLLOOMOOLOO STREET", "VIC", "CARRUM DOWNS", "3201") == ()
    assert _match("ZAMBEZI ROAD", "VIC", "CARRUM DOWNS", "3201") == ()


def test_empty_input_or_no_rows_matches_nothing() -> None:
    assert match_streets((), Span(0, 0), rows_for("VIC", "FITZROY", "3065")) == ()
    assert match_streets(("SMITH", "ST"), Span(0, 2), ()) == ()


# --- the same-name / different-type rule ------------------------------------


@pytest.mark.parametrize(
    ("text", "expected_key", "expected_type"),
    [
        ("SIMMONS STREET", "SIMMONS ST", "ST"),
        ("SIMMONS COURT", "SIMMONS CT", "CT"),
        ("SIMMONS ST", "SIMMONS ST", "ST"),
        ("SIMMONS CT", "SIMMONS CT", "CT"),
    ],
)
def test_same_name_st_ct_pair_resolves_by_type(
    text: str, expected_key: str, expected_type: str
) -> None:
    """A real pair from the 265 in the data. The type decides, and the wrong one
    is not offered at all."""
    ms = _match(text, "VIC", "SOUTH YARRA", "3141")
    assert ms[0].street_key == expected_key
    assert ms[0].row.street_type == expected_type
    assert ms[0].type_matched is True
    assert not any(m.type_mismatch for m in ms), "the other type must be blocked"


@pytest.mark.parametrize(
    ("text", "expected_key"),
    [
        ("CHESTERVILLE ROAD", "CHESTERVILLE RD"),
        ("CHESTERVILLE DRIVE", "CHESTERVILLE DR"),
        ("CHESTERVILLE RD", "CHESTERVILLE RD"),
        ("CHESTERVILLE DR", "CHESTERVILLE DR"),
    ],
)
def test_same_name_rd_dr_pair_resolves_by_type(text: str, expected_key: str) -> None:
    """A real pair from the 69 in the data."""
    ms = _match(text, "VIC", "BENTLEIGH EAST", "3165")
    assert ms[0].street_key == expected_key
    assert ms[0].type_matched is True


def test_exact_type_beats_a_bigger_same_name_street() -> None:
    """SIMMONS ST has 454 addresses against SIMMONS CT's 94, so size must not
    decide when the type does."""
    m = _match("SIMMONS COURT", "VIC", "SOUTH YARRA", "3141")[0]
    assert m.street_key == "SIMMONS CT"
    assert m.row.address_count < 454


def test_type_mismatch_is_allowed_when_no_same_name_street_has_that_type() -> None:
    """Carrum Downs has HALL RD and no HALL ST, so 'HALL STREET' reaches it with
    the penalty rather than failing."""
    m = _match("HALL STREET", "VIC", "CARRUM DOWNS", "3201")[0]
    assert m.street_key == "HALL RD"
    assert m.type_matched is False
    assert m.type_mismatch is True
    assert m.name_score == pytest.approx(100.0)
    assert m.match_score == pytest.approx(1.0 + scoring.TYPE_MISMATCH_PENALTY)


def test_type_mismatch_still_beats_a_poor_name_with_no_type() -> None:
    """The ordering bug this exposed: CLIFTON GR (name 100, wrong type) must beat
    CLIFTON PARK DR (name 64.29 on the whole-name reading)."""
    ms = _match("CLIFTON STREET", "VIC", "CARRUM DOWNS", "3201")
    assert ms[0].street_key == "CLIFTON GR"
    assert ms[0].type_mismatch is True
    assert "CLIFTON PARK DR" in [m.street_key for m in ms[1:]]


def test_mismatch_penalty_is_bounded_by_that_case() -> None:
    """If the penalty ever exceeds 0.36, CLIFTON GR loses to CLIFTON PARK DR."""
    assert -0.36 < scoring.TYPE_MISMATCH_PENALTY < 0.0


# ---------------------------------------------------------------------------
# joint scoring
# ---------------------------------------------------------------------------


def _resolve(raw: str, hyps, consumed=()):
    ts = tokenize(raw)
    return ts, resolve_streets(ts, hyps, consumed=consumed, source=fixture_source)


def test_street_match_contributes_and_nothing_is_left_over() -> None:
    ts = tokenize("65 clifton park drive 3201 carrum downs")
    hyps = [
        _hyp(
            "CARRUM DOWNS",
            "VIC",
            "3201",
            address_count=13079,
            locality_span=Span(5, 7),
            postcode_span=Span(4, 5),
            score=2.44,
        )
    ]
    res = resolve_streets(ts, hyps, consumed=[Span(0, 1)], source=fixture_source)
    assert res[0].street is not None
    assert res[0].street.street_key == "CLIFTON PARK DR"
    assert res[0].granularity == "street"
    assert res[0].unexplained == ()
    assert res[0].signals["street_match"] == pytest.approx(scoring.STREET_MATCH_WEIGHT)


def test_unexplained_token_penalty_separates_carrum_downs_from_carrum() -> None:
    """The requirement: a wider margin than Prompt B's 0.15, via the penalty."""
    ts = tokenize("Carrum Downs VIC")
    downs = _hyp(
        "CARRUM DOWNS",
        "VIC",
        "3201",
        address_count=13079,
        locality_span=Span(0, 2),
        state_span=Span(2, 3),
        score=2.04,
    )
    carrum = _hyp(
        "CARRUM",
        "VIC",
        "3197",
        address_count=3800,
        locality_span=Span(0, 1),
        state_span=Span(2, 3),
        score=1.86,
    )
    res = resolve_streets(ts, [downs, carrum], source=fixture_source)

    assert res[0].locality.candidate.locality == "CARRUM DOWNS"
    assert res[0].unexplained == ()
    loser = next(h for h in res if h.locality.candidate.locality == "CARRUM")
    # DOWNS is explained by nothing: Carrum has no street called DOWNS.
    assert [ts.text_of(s) for s in loser.unexplained] == ["DOWNS"]
    assert loser.signals["unexplained_tokens"] == scoring.UNEXPLAINED_TOKEN_PENALTY
    margin = res[0].score - loser.score
    assert margin > 0.15, f"margin {margin:.3f} is no better than Prompt B's"
    assert margin == pytest.approx(0.78)


def test_no_street_match_becomes_locality_only_and_tokens_go_unexplained() -> None:
    ts = tokenize("12 Woolloomooloo Street 3201")
    hyps = [
        _hyp(
            "CARRUM DOWNS",
            "VIC",
            "3201",
            address_count=13079,
            locality_span=Span(0, 0),
            postcode_span=Span(3, 4),
            score=2.09,
        )
    ]
    res = resolve_streets(ts, hyps, consumed=[Span(0, 1)], source=fixture_source)
    assert res[0].street is None
    assert res[0].granularity == "locality"
    assert [ts.text_of(s) for s in res[0].unexplained] == ["WOOLLOOMOOLOO STREET"]
    assert res[0].signals["unexplained_tokens"] == pytest.approx(
        2 * scoring.UNEXPLAINED_TOKEN_PENALTY
    )


def test_type_mismatch_penalty_is_visible_in_the_breakdown() -> None:
    """Required: the penalty must show, not be hidden inside the street score."""
    ts = tokenize("12 Clifton Street 3201")
    hyps = [
        _hyp(
            "CARRUM DOWNS",
            "VIC",
            "3201",
            address_count=13079,
            locality_span=Span(0, 0),
            postcode_span=Span(3, 4),
            score=2.09,
        )
    ]
    res = resolve_streets(ts, hyps, consumed=[Span(0, 1)], source=fixture_source)
    top = res[0]
    assert top.street is not None and top.street.street_key == "CLIFTON GR"
    assert top.signals["street_type_mismatch"] == scoring.TYPE_MISMATCH_PENALTY
    assert top.unexplained == (), "both tokens are explained by the mismatch reading"
    assert top.score == pytest.approx(sum(top.signals.values()))


def test_a_strong_street_overtakes_a_stronger_locality() -> None:
    """Why three hypotheses are matched, not one."""
    ts = tokenize("Hamilton Crescent Ryde")
    # RYDE scores lower to begin with, and its streets contain the input.
    hamilton = _hyp(
        "HAMILTON", "NSW", "2303", address_count=9000, locality_span=Span(0, 1), score=1.60
    )
    ryde = _hyp("RYDE", "NSW", "2112", address_count=19853, locality_span=Span(2, 3), score=1.35)
    res = resolve_streets(ts, [hamilton, ryde], source=fixture_source)
    assert res[0].locality.candidate.locality == "RYDE"
    assert res[0].street is not None and res[0].street.street_key == "HAMILTON CR"
    assert res[0].score > res[1].score


def test_only_the_top_n_hypotheses_are_considered() -> None:
    ts = tokenize("Smith Street Fitzroy")
    hyps = [_hyp(f"PLACE{i}", "VIC", "3065", locality_span=Span(2, 3)) for i in range(6)]
    res = resolve_streets(ts, hyps, source=fixture_source, top_n=2)
    assert len(res) == 2


def test_signals_always_sum_to_the_score() -> None:
    ts = tokenize("65 clifton park drive 3201 carrum downs")
    hyps = [
        _hyp("CARRUM DOWNS", "VIC", "3201", locality_span=Span(5, 7), postcode_span=Span(4, 5)),
        _hyp("CARRUM", "VIC", "3197", locality_span=Span(5, 6)),
    ]
    for h in resolve_streets(ts, hyps, consumed=[Span(0, 1)], source=fixture_source):
        assert h.score == pytest.approx(sum(h.signals.values()))


def test_no_hypotheses_yields_nothing() -> None:
    assert resolve_streets(tokenize("anything"), [], source=fixture_source) == ()


def test_a_street_is_never_stitched_across_the_locality() -> None:
    """Contiguity: the locality sits between the two street words, so no span can
    cover both."""
    ts = tokenize("SMITH FITZROY STREET")
    hyps = [_hyp("FITZROY", "VIC", "3065", locality_span=Span(1, 2))]
    res = resolve_streets(ts, hyps, source=fixture_source)
    if res[0].street is not None:
        span = res[0].street.span
        assert not span.overlaps(Span(1, 2))
        assert len(span) == 1, "only one side of the locality can be claimed"


def test_the_source_is_called_once_for_all_hypotheses() -> None:
    """The batching condition: one round trip, not one per hypothesis."""
    calls: list[int] = []

    def counting_source(keys):
        calls.append(len(list(keys)))
        return fixture_source(keys)

    ts = tokenize("Smith Street Fitzroy")
    hyps = [
        _hyp("FITZROY", "VIC", "3065", locality_span=Span(2, 3)),
        _hyp("CARRUM DOWNS", "VIC", "3201", locality_span=Span(2, 3)),
        _hyp("RYDE", "NSW", "2112", locality_span=Span(2, 3)),
    ]
    resolve_streets(ts, hyps, source=counting_source)
    assert calls == [3], "expected one call carrying three keys"


# ---------------------------------------------------------------------------
# the store itself
# ---------------------------------------------------------------------------


@needs_db
def test_fixture_matches_the_database() -> None:
    """The fixture is only useful while it is faithful."""
    fetched = streets_for_many(list(FIXTURE))
    for key, expected in FIXTURE.items():
        live = {r.street_key: r for r in fetched[key]}
        for row in expected:
            assert row.street_key in live, f"{row.street_key} gone from {key}"
            got = live[row.street_key]
            assert (got.street_name, got.street_type) == (row.street_name, row.street_type), key


@needs_db
def test_streets_for_many_batches_and_keys_every_result() -> None:
    keys = [("VIC", "CARRUM DOWNS", "3201"), ("NSW", "RYDE", "2112")]
    got = streets_for_many(keys)
    assert set(got) == set(keys)
    assert all(r.key == k for k, rows in got.items() for r in rows)
    assert len(got[keys[0]]) > 100


@needs_db
def test_streets_for_is_the_single_key_form() -> None:
    key = ("NSW", "RYDE", "2112")
    assert streets_for(key) == streets_for_many([key])[key]


@needs_db
def test_streets_for_an_unknown_locality_is_empty() -> None:
    assert streets_for(("VIC", "NOWHERE AT ALL", "9999")) == ()


@needs_db
def test_streets_for_many_with_no_keys_makes_no_query() -> None:
    assert streets_for_many([]) == {}


@needs_db
def test_nt_postcodes_keep_their_leading_zero() -> None:
    rows = streets_for(("NT", "DARWIN CITY", "0800"))
    assert rows
    assert all(r.postcode == "0800" for r in rows)


@needs_db
def test_an_unpadded_postcode_is_padded_on_the_way_in() -> None:
    """A caller that went through an int arrives with '800'."""
    assert streets_for(("NT", "DARWIN CITY", "800")) == streets_for(("NT", "DARWIN CITY", "0800"))
