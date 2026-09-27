"""Routing between the AU address path and the world place path.

The expensive failure mode here is silent: a world place string that starts
resolving to an Australian street, or an Australian address that quietly comes
back as a city. Both look like plausible answers, so these tests name the case
rather than only the outcome.

Needs the AU gazetteer and the street mirror, because routing is decided partly
by whether a street matched.
"""

from __future__ import annotations

import pytest

from locatron.db import mysql
from locatron.resolve import au as au_path
from locatron.resolve.pipeline import resolve_one
from locatron.schemas import Granularity


def _stores_available() -> bool:
    try:
        if not mysql.health().get("connected"):
            return False
        from locatron.db import local

        local.read_meta()
        return True
    except Exception:
        return False


needs_stores = pytest.mark.skipif(
    not _stores_available(), reason="needs ReferenceDB and a built street mirror"
)


def _resolve(text: str):
    return resolve_one(text, record_unresolved=False)


# ---------------------------------------------------------------------------
# the world path keeps what it had
# ---------------------------------------------------------------------------


@needs_stores
@pytest.mark.parametrize(
    ("raw", "granularity", "why"),
    [
        ("Greater Melbourne", Granularity.CITY, "a qualifier, not an address"),
        ("Sydney Australia", Granularity.CITY, "AUSTRALIA is a country token, not a state"),
        ("Las Vegas", Granularity.CITY, "nothing Australian about it"),
        ("Delhi", Granularity.CITY, "population tiebreak, not a suburb"),
        ("Richmond", Granularity.CITY, "ambiguous and unqualified"),
        ("Melbourne", Granularity.CITY, "a city name alone"),
        ("London", Granularity.CITY, "fuzzy-matches LONDONDERRY, has no trigger"),
        ("Victoria Australia", Granularity.ADMIN1, "a state name, not the suburb TORRITA"),
        ("VIC", Granularity.ADMIN1, "fires the state trigger, produces no hypothesis"),
        ("Australia", Granularity.COUNTRY, "fuzzy-matches AUSTRAL, has no trigger"),
        ("asdfghjkl", Granularity.UNRESOLVED, "must not raise"),
    ],
)
def test_a_world_place_stays_on_the_world_path(
    raw: str, granularity: Granularity, why: str
) -> None:
    assert _resolve(raw).granularity is granularity, why


@needs_stores
def test_new_york_does_not_become_a_street_in_york_wa() -> None:
    """The case that rules out a bare street match as a trigger.

    Two things are true and both matter. A street really does match -- NEW ST in
    locality YORK, at a healthy score with nothing left unexplained -- which is
    why a bare street match cannot be a trigger. And the resolver never even asks,
    because 'New York' has no street number and no street-type word, so the street
    query is skipped.

    The first half is asserted by forcing the street stage on, so the danger stays
    proven even though the fast path no longer walks into it.
    """
    from locatron.parse.street import streets_for_many

    forced = au_path.parse("New York", source=streets_for_many)
    assert forced.top is not None and forced.top.street is not None, "a street does match"
    assert forced.top.street.row.street_key == "NEW ST"
    assert forced.top.locality.candidate.locality == "YORK"
    assert not forced.top.unexplained, "and it explains every token"
    assert au_path.triggers(forced) == (), "yet nothing routes it here"

    # The path actually taken: no number, no type word, so no query at all.
    p = au_path.parse("New York")
    assert au_path.could_match_a_street(p.ts, p.components) is False
    assert p.top is not None and p.top.street is None
    assert au_path.takes_au_path(p) == (False, "no AU signal")
    assert _resolve("New York").granularity is Granularity.CITY


@needs_stores
def test_a_fuzzy_locality_with_a_token_to_spare_is_rejected() -> None:
    """The gate's first fuzzy rule, reached through the PO box trigger.

    'PO Box 45 Sao Paulo' fuzzy-matches locality PAULS POCKET and still has SAO
    over, which is a foreign city being read as an Australian suburb. A bare 'Sao
    Paulo' never reaches the gate -- it has no address signal -- but the rule still
    has work wherever a trigger does fire.
    """
    p = au_path.parse("PO Box 45 Sao Paulo")
    assert au_path.triggers(p) == ("pobox",)
    assert p.top is not None and p.top.locality.candidate.match == "fuzzy"
    assert p.top.unexplained
    take, why = au_path.takes_au_path(p)
    assert take is False
    assert why == "fuzzy locality with unexplained tokens"


@needs_stores
def test_a_state_token_alone_is_not_an_address() -> None:
    p = au_path.parse("VIC")
    assert any(t.strong for t in p.states)
    assert au_path.takes_au_path(p) == (False, "no AU signal")


# ---------------------------------------------------------------------------
# the AU path takes what is its
# ---------------------------------------------------------------------------


@needs_stores
@pytest.mark.parametrize(
    ("raw", "trigger"),
    [
        ("65 Clifton Park Dr Carrum Downs VIC 3201", "postcode"),
        ("PO Box 45 World Square NSW 2002", "pobox"),
        # No postcode and no number: the word DRIVE is the whole signal.
        ("Clifton Park Drive Carrum Downs", "street+type"),
        ("12 Clifton Street 3201", "number+street"),
    ],
)
def test_each_trigger_routes_to_the_au_path(raw: str, trigger: str) -> None:
    p = au_path.parse(raw)
    assert trigger in au_path.triggers(p)
    assert au_path.takes_au_path(p)[0] is True


@needs_stores
@pytest.mark.parametrize(
    "raw",
    [
        "Carrum Downs VIC",
        "St Kilda East VIC",
        "Richmond VIC",
        "Melbourne, Victoria, Australia",
        "Perth, Western Australia",
    ],
)
def test_a_stated_state_is_not_an_address_signal(raw: str) -> None:
    """A state says where in the world the input is, not that it describes a
    street. 'Perth, Western Australia' names a metro area, and answering it with
    the PERTH 6000 locality is a more precise answer to a question nobody asked.

    These still resolve -- the world path handles a place name with a state in it
    perfectly well -- they just do not come here.
    """
    p = au_path.parse(raw)
    assert any(t.strong for t in p.states), "premise: the state really is stated"
    assert au_path.triggers(p) == ()
    assert au_path.takes_au_path(p) == (False, "no AU signal")


@needs_stores
def test_a_state_name_input_still_resolves_through_the_world_path() -> None:
    """Removing the state trigger must not lose these answers, only move them.
    'Ku-ring-gai NSW' still comes back as a locality; the world gazetteer does its
    own fuzzy matching."""
    r = _resolve("Ku-ring-gai NSW")
    assert r.granularity is Granularity.LOCALITY
    assert r.admin1 is not None and r.admin1.code == "NSW"


@needs_stores
def test_an_address_comes_back_with_the_whole_gnaf_record() -> None:
    r = _resolve("65 Clifton Park Dr Carrum Downs VIC 3201")
    assert r.granularity is Granularity.ADDRESS
    assert r.au_address is not None
    assert r.au_address.formatted == "65 CLIFTON PARK DR, CARRUM DOWNS VIC 3201"
    assert r.au_address.number_first == "65"
    assert r.au_address.street_type == "DR"
    assert r.canonical_pid == r.au_address.address_detail_pid
    assert r.country is not None and r.country.alpha3 == "AUS"
    assert r.admin1 is not None and r.admin1.code == "VIC"
    assert r.locality == "CARRUM DOWNS"
    assert r.geo is not None


@needs_stores
def test_an_alias_address_reports_its_principal() -> None:
    r = _resolve("12 Alice Street Amaroo ACT 2914")
    assert r.granularity is Granularity.ADDRESS
    assert r.au_address is not None
    assert r.au_address.formatted == "12 ALICE ST, AMAROO ACT 2914"
    assert r.principal is not None
    assert r.principal.formatted == "49 ROLLSTON ST, AMAROO ACT 2914"
    assert r.canonical_pid == r.principal.pid != r.au_address.address_detail_pid


@needs_stores
def test_blank_gnaf_columns_arrive_as_none_not_empty_string() -> None:
    """address_ref uses '' for absent. A consumer writing `if flat_number` should
    not have to know that."""
    r = _resolve("65 Clifton Park Dr Carrum Downs VIC 3201")
    assert r.au_address is not None
    assert r.au_address.flat_number is None
    assert r.au_address.number_last is None


# ---------------------------------------------------------------------------
# granularity mapping
# ---------------------------------------------------------------------------


@needs_stores
def test_a_bare_postcode_answers_postcode_not_locality() -> None:
    """The input proved the postcode and nothing narrower, so the locality it
    might mean is an alternate rather than the answer."""
    r = _resolve("3201")
    assert r.granularity is Granularity.POSTCODE
    assert r.postcode == "3201"
    assert r.locality is None, "the input never named CARRUM DOWNS"
    assert r.admin1 is not None and r.admin1.code == "VIC"


@needs_stores
def test_a_postcode_covering_many_localities_puts_them_in_candidates() -> None:
    """2000 covers nine localities. Answering with one of them would be a guess
    dressed as a result."""
    r = _resolve("2000")
    assert r.granularity is Granularity.POSTCODE
    assert r.locality is None
    assert r.candidates, "the localities it could mean have to go somewhere"
    assert len(r.candidates) <= au_path.MAX_CANDIDATES
    assert all(c.postcode == "2000" for c in r.candidates)


@needs_stores
def test_naming_the_locality_answers_locality() -> None:
    r = _resolve("Carrum Downs VIC")
    assert r.granularity is Granularity.LOCALITY
    assert r.locality == "CARRUM DOWNS"


@needs_stores
def test_a_po_box_answers_postcode_because_gnaf_has_no_postal_addresses() -> None:
    r = _resolve("PO Box 45 World Square NSW 2002")
    assert r.granularity is Granularity.POSTCODE
    assert r.match_method.value == "postal_only"
    assert r.postcode == "2002"
    assert r.au_address is None, "there is no G-NAF row behind a PO box"


@needs_stores
def test_no_number_given_answers_street() -> None:
    r = _resolve("Clifton Park Drive Carrum Downs")
    assert r.granularity is Granularity.STREET
    assert r.geo is not None and r.geo.source.value == "gnaf_street_centroid"
    assert r.au_address is None, "a street centroid is not one address_ref row"


@needs_stores
def test_a_number_that_does_not_exist_degrades_to_street_and_says_so() -> None:
    r = _resolve("14-40 Wills Street Melbourne VIC 3000")
    assert r.granularity is Granularity.STREET
    assert any("no G-NAF row for number 14" in w for w in r.warnings)
    assert r.confidence <= 0.60, "a degraded answer cannot report full confidence"


# ---------------------------------------------------------------------------
# the invariants that outrank everything
# ---------------------------------------------------------------------------


@needs_stores
@pytest.mark.parametrize(
    "raw", ["", "   ", "asdfghjkl", "Remote / Work from home", "3201", "!!!", "0" * 300]
)
def test_resolution_never_raises(raw: str) -> None:
    r = _resolve(raw)
    assert 0.0 <= r.confidence <= 1.0
    assert r.norm_version


@needs_stores
def test_a_broken_au_path_falls_through_to_the_world_path(monkeypatch) -> None:
    """If the street stage fails at request time, world traffic has to keep
    answering -- and the reason has to be visible rather than silent."""

    def explode(*_args, **_kwargs):
        raise RuntimeError("mirror gone")

    monkeypatch.setattr(au_path, "parse", explode)
    r = _resolve("Delhi")
    assert r.granularity is Granularity.CITY
    assert any("AU path unavailable" in w for w in r.warnings)


# ---------------------------------------------------------------------------
# coarser geography must not be read as a suburb
# ---------------------------------------------------------------------------


@needs_stores
@pytest.mark.parametrize(
    "raw",
    [
        # Every full state name fuzzy-matches some unrelated locality, and each
        # leaves nothing unexplained, because the state token accounted for the
        # tokens. Only the span test separates them.
        #
        # A PO box is what puts them in front of the gate at all: it is a trigger,
        # so these reach routing with a fuzzy locality winning, where a bare state
        # name now stops at "no AU signal".
        "PO Box 45 New South Wales",
        "PO Box 45 Western Australia",
        "PO Box 45 Tasmania",
        "GPO Box 9 New South Wales",
    ],
)
def test_a_state_name_is_not_a_suburb(raw: str) -> None:
    """Which suburb it reaches is deliberately not asserted. 'PO Box 45 New South
    Wales' lands on SOUTH BOWENFELS or SOUTH NOWRA depending on the order MySQL
    returned the gazetteer rows in, because they tie on similarity. The property
    that matters is that a fuzzy locality explaining nothing the state token did
    not is refused, whichever one it is.
    """
    p = au_path.parse(raw)
    assert p.top is not None
    assert au_path.triggers(p) == ("pobox",), "premise: it did reach the gate"
    assert p.top.locality.candidate.match == "fuzzy", "premise: only fuzzy got there"
    assert not p.top.unexplained, "premise: the state token explained the tokens"
    take, why = au_path.takes_au_path(p)
    assert take is False, f"{raw} must not resolve to {p.top.locality.candidate.locality}"
    assert why == "fuzzy locality inside the state token"


@needs_stores
def test_a_country_name_is_not_a_suburb() -> None:
    """'PO Box 45 Australia' fuzzy-matches locality AUSTRALIA FAIR on the token
    AUSTRALIA, which sits outside any state token and so passes the span test. The
    country check is what stops it."""
    p = au_path.parse("PO Box 45 Australia")
    assert p.top is not None
    assert p.top.locality.candidate.match == "fuzzy"
    take, why = au_path.takes_au_path(p)
    assert take is False
    assert why == "fuzzy locality is a country name"


def test_within_is_not_overlaps() -> None:
    """The distinction the span test rests on, at the Span level so it holds
    whatever routing does with it. 'Ku-ring-gai NSW' matches its locality over a
    span that includes the state token yet reaches a token beyond it; a state name
    read as a suburb does not."""
    from locatron.parse.tokens import Span

    reaches_beyond, state = Span(0, 2), Span(1, 2)
    assert reaches_beyond.overlaps(state)
    assert not reaches_beyond.within(state)

    inside = Span(1, 3)
    assert inside.within(Span(0, 3))
    assert Span(2, 2).within(Span(2, 2)), "an empty span is within itself"


# ---------------------------------------------------------------------------
# the work that gets skipped
# ---------------------------------------------------------------------------


@needs_stores
@pytest.mark.parametrize("raw", ["Greater Melbourne", "Las Vegas", "Delhi", "London", "New York"])
def test_a_world_input_does_not_pay_for_the_australian_stages(raw: str) -> None:
    """Wiring the AU parser in front of the world path made 'Las Vegas' spend
    51 ms failing to be an Australian suburb, on the path CLAUDE.md's first use
    case is built around. Neither expensive stage can change the routing decision
    for an input like this, so neither runs.
    """
    p = au_path.parse(raw)
    assert au_path.cheap_triggers(p.components) is False
    assert au_path.could_match_a_street(p.ts, p.components) is False
    # No street fetch: every hypothesis came back without one.
    assert all(h.street is None for h in p.hypotheses)
    assert au_path.triggers(p) == ()


@needs_stores
@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ("Clifton Park Drive Carrum Downs", "DRIVE is a street-type word"),
        ("12 Clifton Street 3201", "a street number is present"),
        ("65 Clifton Park Dr Carrum Downs VIC 3201", "both"),
    ],
)
def test_an_input_that_could_name_a_street_still_gets_the_street_stage(raw: str, why: str) -> None:
    p = au_path.parse(raw)
    assert au_path.could_match_a_street(p.ts, p.components) is True, why
    assert p.top is not None and p.top.street is not None


@needs_stores
@pytest.mark.parametrize("raw", ["PO Box 45 New South Wales", "Perth 7300"])
def test_the_fuzzy_sweep_runs_when_the_input_looks_like_an_address(raw: str) -> None:
    """The other side of the skip. A postcode or a PO box earns the sweep; without
    one, a fuzzy locality could not route here anyway."""
    p = au_path.parse(raw)
    assert au_path.cheap_triggers(p.components) is True


# ---------------------------------------------------------------------------
# candidates describe themselves, not the winner
# ---------------------------------------------------------------------------


@needs_stores
def test_a_candidate_reports_its_own_granularity() -> None:
    """Runner-ups used to be stamped with the winner's level, so an alternate that
    matched only a street was reported as `address` -- a claim that a row in
    address_ref backed it, when nothing had been looked up at all."""
    r = resolve_one(
        "65 Clifton Park Dr Carrum Downs VIC 3201",
        record_unresolved=False,
        include_candidates=True,
    )
    assert r.granularity is Granularity.ADDRESS
    assert r.candidates, "premise: there is a runner-up to describe"
    for c in r.candidates:
        assert c.granularity is not Granularity.ADDRESS
        assert c.granularity is not Granularity.UNIT


@needs_stores
def test_a_candidate_never_claims_a_row_was_matched() -> None:
    """address and unit mean a row was found in address_ref. A candidate is never
    looked up, so neither level can honestly apply to one."""
    for raw in [
        "65 Clifton Park Dr Carrum Downs VIC 3201",
        "5/1 Smith Street Fitzroy VIC 3065",
        "2000",
        "Ryde NSW 2112",
    ]:
        r = resolve_one(raw, record_unresolved=False, include_candidates=True)
        assert all(
            c.granularity in (Granularity.STREET, Granularity.LOCALITY, Granularity.POSTCODE)
            for c in r.candidates
        ), (raw, [c.granularity.value for c in r.candidates])


@needs_stores
def test_a_bare_postcode_offers_its_localities_at_postcode_level() -> None:
    r = resolve_one("2000", record_unresolved=False, include_candidates=True)
    assert r.candidates
    assert all(c.granularity is Granularity.POSTCODE for c in r.candidates)
    assert all(c.postcode == "2000" for c in r.candidates)


@needs_stores
def test_candidates_that_round_to_zero_confidence_are_dropped() -> None:
    """An alternate the scoring has already ruled out is noise in a list whose job
    is to show what was close. '5/1 Smith Street Fitzroy VIC 3065' has a runner-up
    scoring well below zero."""
    p = au_path.parse("5/1 Smith Street Fitzroy VIC 3065")
    assert p.runner_up is not None and p.runner_up < 0, "premise: a negative runner-up"

    r = resolve_one(
        "5/1 Smith Street Fitzroy VIC 3065", record_unresolved=False, include_candidates=True
    )
    assert all(c.confidence > 0.0 for c in r.candidates), [c.confidence for c in r.candidates]
