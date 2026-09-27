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
    """The case that rules out a bare street match as a trigger. 'New York'
    matches street NEW ST in locality YORK at a healthy score with nothing left
    unexplained, so only the type-word and number requirements keep it out."""
    p = au_path.parse("New York")
    assert p.top is not None and p.top.street is not None, "premise: a street does match"
    assert p.top.street.reading == "whole-name", "no type word came from the input"
    assert au_path.triggers(p) == ()
    assert _resolve("New York").granularity is Granularity.CITY


@needs_stores
def test_victoria_australia_is_rejected_for_being_fuzzy_and_incomplete() -> None:
    """It does fire the state trigger. The gate is what saves it, and the reason
    matters: a fuzzy locality that leaves a token unexplained is a state name
    being read as a suburb."""
    p = au_path.parse("Victoria Australia")
    assert "state" in au_path.triggers(p)
    take, why = au_path.takes_au_path(p)
    assert take is False
    assert "fuzzy" in why and "unexplained" in why


@needs_stores
def test_a_state_token_alone_has_nothing_to_resolve() -> None:
    p = au_path.parse("VIC")
    assert "state" in au_path.triggers(p)
    assert au_path.takes_au_path(p) == (False, "no locality hypothesis")


# ---------------------------------------------------------------------------
# the AU path takes what is its
# ---------------------------------------------------------------------------


@needs_stores
@pytest.mark.parametrize(
    ("raw", "trigger"),
    [
        ("65 Clifton Park Dr Carrum Downs VIC 3201", "postcode"),
        ("Carrum Downs VIC", "state"),
        ("PO Box 45 World Square NSW 2002", "pobox"),
        # No postcode, no state and no number: the word DRIVE is the whole signal.
        ("Clifton Park Drive Carrum Downs", "street+type"),
    ],
)
def test_each_trigger_routes_to_the_au_path(raw: str, trigger: str) -> None:
    p = au_path.parse(raw)
    assert trigger in au_path.triggers(p)
    assert au_path.takes_au_path(p)[0] is True


@needs_stores
def test_a_fuzzy_locality_that_explains_everything_is_allowed() -> None:
    """'Ku-ring-gai NSW' fuzzy-matches KU-RING-GAI CHASE and leaves nothing over,
    which is the other side of the Victoria Australia rule."""
    p = au_path.parse("Ku-ring-gai NSW")
    assert p.top is not None and p.top.locality.candidate.match == "fuzzy"
    assert not p.top.unexplained
    assert au_path.takes_au_path(p)[0] is True
    assert _resolve("Ku-ring-gai NSW").granularity is Granularity.LOCALITY


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
    ("raw", "reaches"),
    [
        # Every full state name fuzzy-matches some unrelated locality, and each
        # leaves nothing unexplained, because the state token accounted for the
        # tokens. Only the span test separates them.
        ("New South Wales", "SOUTH BOWENFELS"),
        ("Western Australia", "AUSTRALIND"),
        ("South Australia", "SOUTHEND"),
        ("Tasmania", "MATHINNA"),
    ],
)
def test_a_state_name_is_not_a_suburb(raw: str, reaches: str) -> None:
    p = au_path.parse(raw)
    assert p.top is not None
    assert p.top.locality.candidate.locality == reaches, "premise: it really does match that"
    assert not p.top.unexplained, "premise: the state token explained the tokens"
    take, why = au_path.takes_au_path(p)
    assert take is False, f"{raw} must not resolve to {reaches}"
    assert why == "fuzzy locality inside the state token"


@needs_stores
def test_a_country_name_is_not_a_suburb() -> None:
    """'New South Wales Australia' fuzzy-matches locality AUSTRAL on the token
    AUSTRALIA, which sits outside the state token's span and so passes the span
    test. The country check is what stops it."""
    p = au_path.parse("New South Wales Australia")
    assert p.top is not None
    assert p.top.locality.candidate.locality == "AUSTRAL"
    take, why = au_path.takes_au_path(p)
    assert take is False
    assert why == "fuzzy locality is a country name"
    assert _resolve("New South Wales Australia").admin1.code == "NSW"


@needs_stores
def test_a_fuzzy_locality_reaching_past_the_state_token_is_still_allowed() -> None:
    """The other side of the span test, and the reason it is `within` rather than
    `overlaps`: KU-RING-GAI CHASE is matched over a span that includes the state
    token, but it also covers a token the state token does not."""
    p = au_path.parse("Ku-ring-gai NSW")
    h = p.top.locality
    assert h.state_span is not None
    assert h.locality_span.overlaps(h.state_span), "it does overlap"
    assert not h.locality_span.within(h.state_span), "but it reaches beyond"
    assert au_path.takes_au_path(p)[0] is True
