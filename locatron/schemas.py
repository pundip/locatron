"""The response envelope.

One schema covers both the loose-place path and the AU address path, so
consumers never have to branch on which resolver ran.

Resolution never raises for unresolvable input. An unresolvable string comes
back as HTTP 200 with granularity=UNRESOLVED and confidence=0.0.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Granularity(str, Enum):
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


class MatchMethod(str, Enum):
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


class GeoSource(str, Enum):
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
    """G-NAF fields. Populated only for granularity unit/address/street."""

    address_detail_pid: str | None = None
    flat_type: str | None = None
    flat_number: str | None = None
    level_type: str | None = None
    level_number: str | None = None
    number_first: str | None = None
    number_last: str | None = None
    street_name: str | None = None
    street_type: str | None = None
    street_suffix: str | None = None
    locality_name: str | None = None
    state: str | None = None
    postcode: str | None = None
    mb_code: str | None = None
    alias_principal: str | None = None
    primary_secondary: str | None = None
    formatted: str | None = Field(
        None, description="Single-line canonical form, e.g. '65 CLIFTON PARK DR, CARRUM DOWNS VIC 3201'"
    )


class Candidate(BaseModel):
    """A runner-up. Populated when the match was ambiguous."""

    label: str
    confidence: float
    granularity: Granularity
    country: str | None = None
    admin1: str | None = None
    locality: str | None = None
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
