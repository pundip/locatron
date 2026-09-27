"""The response envelope.

One schema covers both the loose-place path and the AU address path, so
consumers never have to branch on which resolver ran. A world answer carries the
same keys an Australian one does, with nulls where there is nothing to report.

`au_address` is the **full G-NAF record** -- every column of the matched
`address_ref` row, not a summary of it. There is deliberately no separate `gnaf`
key: `au_address` is that object, and a second top-level key holding the same
contents would put two sources of truth in one envelope. `tests/test_schemas.py`
asserts the correspondence against `parse.lookup.GnafRecord` in both directions,
so a column added by an upstream refresh fails loudly rather than quietly not
being forwarded.

Resolution never raises for unresolvable input. An unresolvable string comes
back as HTTP 200 with granularity=UNRESOLVED and confidence=0.0.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Granularity(StrEnum):
    """Ordered from most to least specific."""

    UNIT = "unit"
    ADDRESS = "address"
    STREET = "street"
    POSTCODE = "postcode"
    LOCALITY = "locality"
    ADMIN1 = "admin1"
    CITY = "city"
    COUNTRY = "country"
    UNRESOLVED = "unresolved"


_ORDER = [g for g in Granularity]


def at_least(got: Granularity, want: Granularity) -> bool:
    """True if `got` is at least as specific as `want`."""
    return _ORDER.index(got) <= _ORDER.index(want)


class MatchMethod(StrEnum):
    CACHE = "cache"
    GNAF_EXACT = "gnaf_exact"
    GNAF_STREET_CENTROID = "gnaf_street_centroid"
    LOCALITY_EXACT = "locality_exact"
    LOCALITY_ALIAS = "locality_alias"
    LOCALITY_FUZZY = "locality_fuzzy"
    POSTAL_ONLY = "postal_only"
    CITY_EXACT = "city_exact"
    CITY_FUZZY = "city_fuzzy"
    CITY_POPULATION_TIEBREAK = "city_population_tiebreak"
    COUNTRY_BUCKET = "country_bucket"
    STATE_BUCKET = "state_bucket"
    NONE = "none"


class GeoSource(StrEnum):
    GNAF_PROPERTY_CENTROID = "gnaf_property_centroid"
    GNAF_STREET_CENTROID = "gnaf_street_centroid"
    LOCALITY_CENTROID = "locality_centroid"
    POSTCODE_CENTROID = "postcode_centroid"
    CITY_POINT = "city_point"
    NONE = "none"


class Country(BaseModel):
    name: str
    alpha2: str | None = None
    alpha3: str | None = None


class Admin1(BaseModel):
    """State, province, or equivalent."""

    name: str
    code: str | None = None


class Geo(BaseModel):
    lat: float
    lng: float
    source: GeoSource


class AuAddress(BaseModel):
    """Every G-NAF column from the matched `address_ref` row.

    Populated for granularity unit and address, where a row was actually
    matched. Empty for street and below: those answers come from precomputed
    centroids and there is no single row behind them.

    Blank strings from G-NAF arrive here as None, because a consumer checking
    `if flat_number` should not have to know that address_ref uses '' for
    absent.
    """

    address_detail_pid: str | None = None
    flat_type: str | None = None
    flat_number: str | None = None
    level_type: str | None = None
    level_number: str | None = None
    number_first: str | None = None
    number_last: str | None = None
    lot_number: str | None = None
    street_name: str | None = None
    street_type: str | None = None
    street_suffix: str | None = None
    locality_name: str | None = None
    state: str | None = None
    postcode: str | None = None
    building_name: str | None = None
    address_site_name: str | None = None
    mb_code: str | None = None
    legal_parcel_id: str | None = None
    geocode_type: str | None = Field(
        None, description="How G-NAF sited the point, e.g. 'PROPERTY CENTROID'"
    )
    alias_principal: str | None = Field(
        None, description="'P' for a principal row, 'A' for an alias of one"
    )
    principal_pid: str | None = Field(
        None, description="Set on an alias row: the pid it is an alias of"
    )
    primary_secondary: str | None = Field(
        None, description="'P' group head, 'S' member, None for an ordinary address"
    )
    primary_pid: str | None = None
    date_created: str | None = None
    formatted: str | None = Field(
        None,
        description=("G-NAF's own ADDRESS_LABEL, e.g. '65 CLIFTON PARK DR, CARRUM DOWNS VIC 3201'"),
    )


class Principal(BaseModel):
    """The principal address an alias match belongs to.

    Set only when the matched row is a G-NAF alias. The match itself stays the
    alias -- it carries the street and number the input used -- and this is the
    row to join and deduplicate on. See `canonical_pid`.
    """

    pid: str
    formatted: str = Field(description="The principal's own ADDRESS_LABEL")


class Candidate(BaseModel):
    """A runner-up. Populated when the match was ambiguous.

    For a bare postcode this is where the localities it could mean go: the input
    proved the postcode and nothing narrower, so they are alternates rather than
    an answer.
    """

    label: str
    confidence: float
    granularity: Granularity
    country: str | None = None
    admin1: str | None = None
    locality: str | None = None
    postcode: str | None = None
    score: float | None = Field(
        None,
        description=(
            "The raw joint score, before mapping onto 0..1. Exposed so a caller "
            "can see the margin over the winner, which confidence only summarises."
        ),
    )
    reason: str | None = None


class ResolveRequest(BaseModel):
    text: str
    country_bias: str | None = None
    min_granularity: Granularity | None = Field(
        None, description="Stop early once this level is reached. Saves work on bulk jobs."
    )
    include_candidates: bool = False
    use_cache: bool = True


class ResolveResponse(BaseModel):
    query: str
    normalized: str
    resolved: bool
    granularity: Granularity
    confidence: float = Field(ge=0.0, le=1.0)
    match_method: MatchMethod

    country: Country | None = None
    admin1: Admin1 | None = None
    locality: str | None = None
    postcode: str | None = None
    geo: Geo | None = None
    au_address: AuAddress | None = None
    canonical_pid: str | None = Field(
        None,
        description=(
            "The G-NAF pid to join and deduplicate on: the principal's pid when "
            "the match is an alias, the matched row's own pid otherwise. None "
            "below address granularity, where no single row was matched."
        ),
    )
    principal: Principal | None = None

    candidates: list[Candidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    norm_version: str
    snapshot_id: str | None = None
    elapsed_ms: float | None = None
    resolved_at: datetime | None = None


class BatchResolveRequest(BaseModel):
    items: list[str] = Field(max_length=1000)
    country_bias: str | None = None
    min_granularity: Granularity | None = None
    use_cache: bool = True


class BatchResolveResponse(BaseModel):
    results: list[ResolveResponse]
    count: int
    cache_hits: int = 0
    elapsed_ms: float | None = None
