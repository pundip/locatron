"""The response envelope's wire contract.

These exist because `Granularity`, `MatchMethod`, and `GeoSource` moved from
`(str, Enum)` to `StrEnum`. That is a behavioural change, not just a lint fix:
under the old form `str(Granularity.CITY)` returned `'Granularity.CITY'`, and
anything that interpolated a member without `.value` would have emitted that
into a Databricks column. Nothing did, but nothing was stopping it either.
"""

from __future__ import annotations

import json

import pytest

from locatron.schemas import (
    Admin1,
    Country,
    Geo,
    GeoSource,
    Granularity,
    MatchMethod,
    ResolveResponse,
    at_least,
)


@pytest.mark.parametrize(
    ("member", "wire"),
    [
        (Granularity.CITY, "city"),
        (Granularity.UNRESOLVED, "unresolved"),
        (MatchMethod.CITY_POPULATION_TIEBREAK, "city_population_tiebreak"),
        (MatchMethod.NONE, "none"),
        (GeoSource.CITY_POINT, "city_point"),
    ],
)
def test_a_member_stringifies_to_its_wire_value(member: object, wire: str) -> None:
    """The whole point of StrEnum: no '.value' needed to get a usable string."""
    assert str(member) == wire
    assert f"{member}" == wire
    assert member == wire
    assert member.value == wire  # type: ignore[attr-defined]


def _response() -> ResolveResponse:
    return ResolveResponse(
        query="Greater Melbourne",
        normalized="GREATER MELBOURNE",
        resolved=True,
        granularity=Granularity.CITY,
        confidence=0.79,
        match_method=MatchMethod.CITY_POPULATION_TIEBREAK,
        country=Country(name="Australia", alpha2="AU", alpha3="AUS"),
        admin1=Admin1(name="Victoria", code="VIC"),
        geo=Geo(lat=-37.8142, lng=144.9631, source=GeoSource.CITY_POINT),
        norm_version="1",
    )


def test_enums_serialise_as_plain_strings() -> None:
    """A Databricks column must receive 'city', never 'Granularity.CITY'."""
    payload = json.loads(json.dumps(_response().model_dump(mode="json")))
    assert payload["granularity"] == "city"
    assert payload["match_method"] == "city_population_tiebreak"
    assert payload["geo"]["source"] == "city_point"


def test_a_response_round_trips_through_json() -> None:
    original = _response()
    revived = ResolveResponse.model_validate(original.model_dump(mode="json"))
    assert revived.granularity is Granularity.CITY
    assert revived.match_method is MatchMethod.CITY_POPULATION_TIEBREAK
    assert revived.geo is not None and revived.geo.source is GeoSource.CITY_POINT


def test_a_granularity_validates_from_its_wire_value() -> None:
    assert Granularity("unresolved") is Granularity.UNRESOLVED
    with pytest.raises(ValueError, match="not a valid"):
        Granularity("suburb-ish")


def test_granularity_order_survived_the_change() -> None:
    """at_least() indexes the declaration order, so the move must not reorder it."""
    assert at_least(Granularity.ADDRESS, Granularity.LOCALITY)
    assert at_least(Granularity.CITY, Granularity.CITY)
    assert not at_least(Granularity.COUNTRY, Granularity.CITY)
    assert not at_least(Granularity.UNRESOLVED, Granularity.COUNTRY)


def test_confidence_is_bounded() -> None:
    for bad in (-0.1, 1.1):
        with pytest.raises(ValueError, match="confidence"):
            ResolveResponse(
                query="x",
                normalized="X",
                resolved=True,
                granularity=Granularity.CITY,
                confidence=bad,
                match_method=MatchMethod.CITY_EXACT,
                norm_version="1",
            )


# ---------------------------------------------------------------------------
# the G-NAF envelope
# ---------------------------------------------------------------------------


def test_au_address_exposes_every_gnaf_column() -> None:
    """The point of `au_address` is that a consumer never has to go back to
    address_ref for a column we did not think to forward. An upstream refresh
    that adds one must fail here rather than quietly drop it.

    `formatted` is ADDRESS_LABEL renamed; lat/lng live in `geo`.
    """
    from locatron.parse.lookup import GnafRecord
    from locatron.schemas import AuAddress

    columns = set(GnafRecord.__dataclass_fields__) - {"lat", "lng", "address_label"}
    exposed = set(AuAddress.model_fields) - {"formatted"}
    assert columns - exposed == set(), "G-NAF columns missing from the response"
    assert exposed - columns == set(), "response fields with no G-NAF column behind them"


def test_the_envelope_carries_a_canonical_pid_and_a_principal() -> None:
    from locatron.schemas import Principal

    r = ResolveResponse(
        query="12 Alice Street Amaroo ACT 2914",
        normalized="12 ALICE STREET AMAROO ACT 2914",
        resolved=True,
        granularity=Granularity.ADDRESS,
        confidence=0.7,
        match_method=MatchMethod.GNAF_EXACT,
        canonical_pid="GAACT714849931",
        principal=Principal(pid="GAACT714849931", formatted="49 ROLLSTON ST, AMAROO ACT 2914"),
        norm_version="1",
    )
    assert r.canonical_pid == "GAACT714849931"
    assert r.principal is not None and r.principal.pid == "GAACT714849931"


def test_the_new_fields_are_optional_so_world_responses_are_unchanged() -> None:
    """Additive means a world-path response built the old way still validates and
    still serialises the same keys it did before, plus nulls."""
    r = ResolveResponse(
        query="Delhi",
        normalized="DELHI",
        resolved=True,
        granularity=Granularity.CITY,
        confidence=0.9,
        match_method=MatchMethod.CITY_POPULATION_TIEBREAK,
        norm_version="1",
    )
    assert r.canonical_pid is None
    assert r.principal is None
    assert r.au_address is None
