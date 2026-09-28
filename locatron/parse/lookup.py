"""Fetch the full G-NAF record for a winning hypothesis, and degrade gracefully.

Exact, index-backed lookups only. `address_ref` is 15,949,543 rows and is never
fuzzy-matched, never hit with LIKE, and never aggregated at request time. Street
and locality centroids are precomputed in `locatron_street` and
`locatron_locality`, both at 100% coverage, so a fallback reads those instead.

Nothing is wrapped around an indexed column in a WHERE clause. That rule is not
stylistic -- measured on this table:

    WHERE POSTCODE = '0800'               type=ref   1 row           1 ms
    WHERE LPAD(POSTCODE,4,'0') = '0800'   type=ALL   15,886,279 rows 10.6 s

And no padding or trimming is needed anyway. `POSTCODE` is four characters on
every row, and zero rows have `col <> TRIM(col)` for `STREET_NAME`,
`LOCALITY_NAME`, `STREET_TYPE`, `STATE` or `NUMBER_FIRST`. So the values go into
the query exactly as stored, and any tidying happens in Python on the way out.
`CAST(NULLIF(TRIM(col),'') AS DECIMAL(...))` appears in the SELECT list only,
where it costs nothing and guards the `CAST('' AS DECIMAL) = 0` trap even though
this table has no blank coordinates.

Reads use the service account with `SET SESSION TRANSACTION READ ONLY`, the same
as the mirror build, so a lookup cannot write whatever it is running as.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import text

from locatron.db import mysql

#: (LOCALITY_NAME, STREET_NAME, STREET_TYPE, NUMBER_FIRST) -- the leading four
#: columns of ix_ar_loc_st_num, so a lookup is one index dive.
AddressKey = tuple[str, str, str, str]

#: Every column worth returning. LONGITUDE precedes LATITUDE in the table; they
#: are named explicitly here so the order cannot be got wrong by accident.
#:
#: The two CASTs are in the SELECT list, never in WHERE. NULLIF(TRIM(col),'')
#: first because CAST('' AS DECIMAL) is 0, not NULL, and a zero latitude is a
#: real place in the Gulf of Guinea.
_COLUMNS = """
    ADDRESS_DETAIL_PID, ADDRESS_LABEL, ADDRESS_SITE_NAME, BUILDING_NAME,
    FLAT_TYPE, FLAT_NUMBER, LEVEL_TYPE, LEVEL_NUMBER,
    NUMBER_FIRST, NUMBER_LAST, LOT_NUMBER,
    STREET_NAME, STREET_TYPE, STREET_SUFFIX,
    LOCALITY_NAME, STATE, POSTCODE,
    ALIAS_PRINCIPAL, PRINCIPAL_PID, PRIMARY_SECONDARY, PRIMARY_PID,
    GEOCODE_TYPE, MB_CODE, LEGAL_PARCEL_ID, DATE_CREATED,
    CAST(NULLIF(TRIM(LATITUDE), '')  AS DECIMAL(12, 8)) AS LAT,
    CAST(NULLIF(TRIM(LONGITUDE), '') AS DECIMAL(12, 8)) AS LNG
"""

#: Index: ix_ar_loc_st_num(LOCALITY_NAME, STREET_NAME, STREET_TYPE, NUMBER_FIRST).
#: All four columns are equality-matched, so EXPLAIN gives type=ref with 1-7 rows.
#: NUMBER_LAST is deliberately absent: it is in no index, so a range is narrowed
#: by these four and the last is compared in Python rather than turning the query
#: into `Using where` over a wider set.
_BY_NUMBER_SQL = f"""
SELECT {_COLUMNS}
FROM address_ref
WHERE LOCALITY_NAME = :locality
  AND STREET_NAME   = :street_name
  AND STREET_TYPE   = :street_type
  AND NUMBER_FIRST  = :number_first
"""

#: Index: ix_ar_pid(ADDRESS_DETAIL_PID). Used only to follow PRINCIPAL_PID off an
#: alias row, which is the one case that needs a second round trip.
_BY_PID_SQL = f"""
SELECT {_COLUMNS}
FROM address_ref
WHERE ADDRESS_DETAIL_PID = :pid
"""


@dataclass(frozen=True, slots=True)
class GnafRecord:
    """One `address_ref` row. Blank strings stay blank, as the table stores them."""

    address_detail_pid: str
    address_label: str
    address_site_name: str
    building_name: str
    flat_type: str
    flat_number: str
    level_type: str
    level_number: str
    number_first: str
    number_last: str
    lot_number: str
    street_name: str
    street_type: str
    street_suffix: str
    locality_name: str
    state: str
    postcode: str
    alias_principal: str
    principal_pid: str
    primary_secondary: str
    primary_pid: str
    geocode_type: str
    mb_code: str
    legal_parcel_id: str
    date_created: str
    lat: float | None
    lng: float | None

    @property
    def is_alias(self) -> bool:
        """ALIAS_PRINCIPAL is 'A' or 'P', never the words. 'A' is 5.27% of rows."""
        return self.alias_principal == "A"

    @property
    def has_flat(self) -> bool:
        return bool(self.flat_number)

    @property
    def is_range(self) -> bool:
        return bool(self.number_last)


#: What a lookup needs from the address store. Satisfied by `rows_for_number` and
#: by a test fixture, which is how the tests run with no database.
AddressSource = Callable[[AddressKey], tuple[GnafRecord, ...]]

#: Following an alias to its principal. Separate from AddressSource because it is
#: a different index and a different round trip.
PrincipalSource = Callable[[str], GnafRecord | None]


def _record(row) -> GnafRecord:
    """Map a result row, defaulting every text column to '' as the table does."""

    def s(key: str) -> str:
        v = row[key]
        return "" if v is None else str(v)

    return GnafRecord(
        address_detail_pid=s("ADDRESS_DETAIL_PID"),
        address_label=s("ADDRESS_LABEL"),
        address_site_name=s("ADDRESS_SITE_NAME"),
        building_name=s("BUILDING_NAME"),
        flat_type=s("FLAT_TYPE"),
        flat_number=s("FLAT_NUMBER"),
        level_type=s("LEVEL_TYPE"),
        level_number=s("LEVEL_NUMBER"),
        number_first=s("NUMBER_FIRST"),
        number_last=s("NUMBER_LAST"),
        lot_number=s("LOT_NUMBER"),
        street_name=s("STREET_NAME"),
        street_type=s("STREET_TYPE"),
        street_suffix=s("STREET_SUFFIX"),
        locality_name=s("LOCALITY_NAME"),
        state=s("STATE"),
        # Already four characters on every row, so this is belt-and-braces for a
        # future reload rather than a fix for anything present.
        postcode=s("POSTCODE").rjust(4, "0"),
        alias_principal=s("ALIAS_PRINCIPAL"),
        principal_pid=s("PRINCIPAL_PID"),
        primary_secondary=s("PRIMARY_SECONDARY"),
        primary_pid=s("PRIMARY_PID"),
        geocode_type=s("GEOCODE_TYPE"),
        mb_code=s("MB_CODE"),
        legal_parcel_id=s("LEGAL_PARCEL_ID"),
        date_created=s("DATE_CREATED"),
        lat=float(row["LAT"]) if row["LAT"] is not None else None,
        lng=float(row["LNG"]) if row["LNG"] is not None else None,
    )


def rows_for_number(key: AddressKey) -> tuple[GnafRecord, ...]:
    """Every `address_ref` row at one street number, units included.

    One index dive on ix_ar_loc_st_num. Returns the rows unsorted; choosing
    between them is `_rank` in the tie-break stage, done in Python because the
    candidate set is a handful of rows and the rules are easier to read and test
    there than in an ORDER BY.
    """
    locality, street_name, street_type, number_first = key
    with mysql.read_session_scope() as s:
        return tuple(
            _record(r)
            for r in s.execute(
                text(_BY_NUMBER_SQL),
                {
                    "locality": locality,
                    "street_name": street_name,
                    "street_type": street_type,
                    "number_first": number_first,
                },
            ).mappings()
        )


def row_by_pid(pid: str) -> GnafRecord | None:
    """One row by ADDRESS_DETAIL_PID, for following an alias to its principal."""
    if not pid:
        return None
    with mysql.read_session_scope() as s:
        row = s.execute(text(_BY_PID_SQL), {"pid": pid}).mappings().first()
        return _record(row) if row else None


@dataclass
class RoundTrips:
    """Counts MySQL queries for one lookup, so the budget is observable rather
    than asserted. At most two: the number lookup, and an alias follow."""

    count: int = 0
    labels: list[str] = field(default_factory=list)

    def record(self, label: str) -> None:
        self.count += 1
        self.labels.append(label)


def counted(source: AddressSource, trips: RoundTrips, label: str) -> AddressSource:
    """Wrap a source so its calls are counted."""

    def wrapped(key: AddressKey) -> tuple[GnafRecord, ...]:
        trips.record(label)
        return source(key)

    return wrapped


def counted_principal(source: PrincipalSource, trips: RoundTrips, label: str) -> PrincipalSource:
    def wrapped(pid: str) -> GnafRecord | None:
        trips.record(label)
        return source(pid)

    return wrapped


def normalise_number(number_first: str, number_last: str | None) -> tuple[str, str | None]:
    """The number as `address_ref` stores it.

    An alpha suffix stays inside number_first -- '6C' is one value there, with
    NUMBER_LAST blank -- so nothing is split off. A range keeps both halves.
    """
    return number_first, (number_last or None)


def address_keys(
    locality: str, street_name: str, street_type: str, numbers: Sequence[str]
) -> list[AddressKey]:
    """Keys to try, in order. Used by the range ladder in the next stage."""
    return [(locality, street_name, street_type, n) for n in numbers]


# ---------------------------------------------------------------------------
# tie-break, units and ranges
# ---------------------------------------------------------------------------
#
# All of this runs in Python on the handful of rows one street number returns,
# not in an ORDER BY. The rules are easier to read and to test here, and the
# candidate set is 1 to 7 rows.

#: PRIMARY_SECONDARY preference. Blank is the ordinary case, not missing data:
#: 10,414,829 rows are blank against 4,966,618 'S' and 568,096 'P'. So a blank
#: standalone address outranks a secondary row, and a group's head outranks both.
_PRIMARY_RANK = {"P": 0, "": 1, "S": 2}

#: ALIAS_PRINCIPAL preference. 'P' is principal, 'A' is an alias pointing at one
#: through PRINCIPAL_PID. Never the words: 'PRINCIPAL' matches nothing.
_ALIAS_RANK = {"P": 0, "A": 1}


def _rank(record: GnafRecord) -> tuple[int, int, str]:
    """Sort key for choosing between rows at the same street number.

    Principal before alias, then the PRIMARY_SECONDARY preference, then the pid
    so two runs never disagree.
    """
    return (
        _ALIAS_RANK.get(record.alias_principal, 9),
        _PRIMARY_RANK.get(record.primary_secondary, 9),
        record.address_detail_pid,
    )


def best_without_unit(records: Sequence[GnafRecord]) -> GnafRecord | None:
    """The row to return when the input named no unit.

    Prefers a principal over an alias, then 'P' over blank over 'S'. A building
    with units returns the building rather than an arbitrary flat, because the
    'P' row is the one without a FLAT_NUMBER.
    """
    if not records:
        return None
    return sorted(records, key=_rank)[0]


def match_unit(records: Sequence[GnafRecord], unit: str) -> GnafRecord | None:
    """The row whose FLAT_NUMBER is `unit`, regardless of indicator.

    Indicator-blind on purpose: a unit is almost always an 'S' row, so ranking
    by PRIMARY_SECONDARY first would pick the building and miss the flat. Among
    several rows sharing a FLAT_NUMBER the usual rank breaks the tie.
    """
    if not unit:
        return None
    hits = [r for r in records if r.flat_number == unit]
    if not hits:
        return None
    return sorted(hits, key=_rank)[0]


def match_range(
    records: Sequence[GnafRecord], number_last: str | None
) -> tuple[GnafRecord | None, bool]:
    """(row, exact) for a number range.

    '14-40' wants the row whose NUMBER_FIRST is 14 and NUMBER_LAST is 40. When no
    row carries that range, the first half alone is the next best answer -- the
    caller has already narrowed to NUMBER_FIRST, so those rows are in hand and
    cost nothing. `exact` says which happened, so a warning can name it.
    """
    if not records:
        return None, False
    if number_last is None:
        return best_without_unit(records), True

    exact = [r for r in records if r.number_last == number_last]
    if exact:
        return sorted(exact, key=_rank)[0], True
    return best_without_unit(records), False


@dataclass(frozen=True, slots=True)
class PrincipalRef:
    """The principal an alias row points at. Reference only, not the answer."""

    pid: str
    address: str
    """ADDRESS_LABEL of the principal, which is G-NAF's own formatting."""


def follow_alias(
    record: GnafRecord, principal: PrincipalSource
) -> tuple[GnafRecord, PrincipalRef | None, tuple[str, ...]]:
    """(matched record, principal reference, warnings) for a possibly-alias row.

    The alias row IS the answer. It carries the street and number the input
    actually used and its own coordinates, so returning the principal instead
    would answer a question nobody asked -- somebody who types
    '12 Alice Street Amaroo' wants 12 Alice Street, not 49 Rollston Street.

    The principal still matters, because it is the row a consumer should join on
    and deduplicate by, so it comes back alongside as a reference and as
    canonical_pid. 5.27% of address_ref rows are aliases.
    """
    if not record.is_alias:
        return record, None, ()

    target = principal(record.principal_pid)
    if target is None:
        return (
            record,
            None,
            (
                f"matched an alias record ({record.address_detail_pid}) whose "
                f"principal {record.principal_pid!r} could not be found",
            ),
        )
    ref = PrincipalRef(pid=target.address_detail_pid, address=target.address_label)
    return record, ref, (f"the input address is an alias of {ref.address}",)


# ---------------------------------------------------------------------------
# the fallback ladder
# ---------------------------------------------------------------------------


class Granularity:
    """How far down the ladder a lookup got. Lower is more precise."""

    UNIT = "unit"
    ADDRESS = "address"
    STREET = "street"
    LOCALITY = "locality"
    POSTAL = "postal"


#: Ceiling on confidence when the matched street's type is not the one the input
#: asked for. A substituted type means we answered a question slightly different
#: from the one asked -- 'Clifton Street' resolved to CLIFTON GR -- and a
#: unit or address returned at full confidence would invite a downstream consumer
#: to treat it as exact. Named rather than inline so it can be tuned with the
#: other weights, and applied as a cap rather than a subtraction so it cannot
#: push a weak match below the floor.
SUBSTITUTED_TYPE_CONFIDENCE_CAP = 0.70

#: Ceiling on confidence when the input gave a number range and no stored row
#: carries it, so the ladder fell back to NUMBER_FIRST alone. '17-99 Wills St'
#: lands on the stored 17-23: the street and the first number are confirmed
#: exact, but the extent is not the one asked for, and 17-99 may well span
#: several G-NAF rows. Looser than the substituted-type cap because the doubt is
#: narrower -- there the matched street was a different street.
#:
#: Only a *given* range can mismatch. A bare '17' matching a stored 17-23 asks
#: nothing about extent, so nothing caps it.
RANGE_MISMATCH_CONFIDENCE_CAP = 0.75

#: Ceiling when a unit was asked for, the building was found and that flat was
#: not. The building is still the right answer to return -- it is where the
#: address is -- but it is not the address that was asked for, and 'Unit 9 1
#: Smith Street Fitzroy' where the flats run 1 to 6 should not read as a hit.
#: Tightest of the caps, because the missing part was stated explicitly rather
#: than inferred.
UNIT_NOT_FOUND_CONFIDENCE_CAP = 0.65

#: Ceiling when a street number was given and no row on that street carries it,
#: so the answer fell back to the street centroid. Tighter than
#: UNIT_NOT_FOUND, because there at least the building was found: here nothing
#: below the street was.
#:
#: Without this a degraded answer reported full confidence. '14-40 Wills Street
#: Melbourne VIC 3000' parses perfectly -- postcode, state, locality and street
#: all corroborate, joint score 4.03 -- and then finds no row at number 14 and
#: returns the street. The parse being excellent says nothing about an address
#: that is not in G-NAF, and 1.000 on a street centroid invites a consumer to
#: treat it as the address asked for.
NUMBER_NOT_FOUND_CONFIDENCE_CAP = 0.60

#: Ceiling when the matched row is a G-NAF alias. Mildest of the caps: the
#: address is real, the coordinates are its own, and the only reservation is that
#: G-NAF considers a different row canonical, which `canonical_pid` and
#: `principal` already report.
ALIAS_CONFIDENCE_CAP = 0.85


def _tighter(current: float | None, cap: float) -> float:
    """The lower of two ceilings, so a second doubt can only narrow the first."""
    return cap if current is None else min(current, cap)


@dataclass(frozen=True, slots=True)
class LookupResult:
    """What a lookup concluded, and how it got there."""

    granularity: str
    lat: float | None
    lng: float | None
    record: GnafRecord | None
    """The full G-NAF row, for unit and address granularity. None below that:
    street and locality answers come from precomputed centroids, and postal never
    touches address_ref at all."""
    warnings: tuple[str, ...]
    hypothesis: object
    """The StreetHypothesis this came from, carried so a caller can explain the
    answer without re-running the parse."""
    principal: PrincipalRef | None = None
    """Set only when `record` is an alias row. The alias is the answer; this says
    which principal it belongs to, for a consumer that needs to deduplicate."""
    canonical_pid: str = ""
    """The pid to join and deduplicate on: the principal's when the match is an
    alias, the record's own otherwise. Blank below address granularity, where
    there is no G-NAF row."""
    confidence_cap: float | None = None
    """Set when something about the match should stop a caller reporting full
    confidence. None means nothing capped it."""
    round_trips: int = 0
    round_trip_labels: tuple[str, ...] = ()

    @property
    def is_address_level(self) -> bool:
        return self.granularity in (Granularity.UNIT, Granularity.ADDRESS)


def lookup(
    hypothesis,
    *,
    number_first: str | None = None,
    number_last: str | None = None,
    unit: str | None = None,
    po_box: bool = False,
    street_centroid: tuple[float | None, float | None] | None = None,
    source: AddressSource = rows_for_number,
    principal: PrincipalSource = row_by_pid,
) -> LookupResult:
    """Walk the ladder for one winning hypothesis.

        postal    a PO box, or a locality flagged is_postal_only. G-NAF holds no
                  PO boxes at all, so address_ref is never queried.
        locality  no street matched. Locality centroid.
        street    a street matched but no number was given, or no row exists for
                  the number. Street centroid from the mirror.
        address   a row at that number.
        unit      a row at that number whose FLAT_NUMBER matches.

    At most two MySQL round trips: one dive on ix_ar_loc_st_num, and one more only
    when an alias has to be followed. `round_trips` reports the actual count.
    """
    trips = RoundTrips()
    warnings: list[str] = []
    candidate = hypothesis.locality.candidate

    # --- postal: never touches address_ref -------------------------------
    if po_box or candidate.is_postal_only:
        if po_box:
            warnings.append("PO box: G-NAF holds no postal addresses, so no street match")
        return _result(
            Granularity.POSTAL,
            candidate.row.lat,
            candidate.row.lng,
            None,
            warnings,
            hypothesis,
            trips,
        )

    street = hypothesis.street
    if street is None:
        return _result(
            Granularity.LOCALITY,
            candidate.row.lat,
            candidate.row.lng,
            None,
            warnings,
            hypothesis,
            trips,
        )

    # A substituted type caps confidence wherever the ladder lands, and says so.
    cap: float | None = None
    sub = hypothesis.street_type_substituted
    if sub is not None:
        cap = _tighter(cap, SUBSTITUTED_TYPE_CONFIDENCE_CAP)
        warnings.append(
            f"street type substituted: input said {sub.written_as!r} "
            f"({sub.input_type or 'unknown'}), matched {sub.matched_type or 'none'}"
        )

    s_lat, s_lng = street_centroid if street_centroid else (street.row.lat, street.row.lng)

    if not number_first:
        return _result(Granularity.STREET, s_lat, s_lng, None, warnings, hypothesis, trips, cap)

    # --- address and unit ------------------------------------------------
    key: AddressKey = (
        street.row.locality,
        street.row.street_name,
        street.row.street_type,
        number_first,
    )
    rows = counted(source, trips, f"rows_for_number {key}")(key)

    if not rows:
        warnings.append(
            f"no G-NAF row for number {number_first} on "
            f"{street.street_key} in {street.row.locality}; fell back to the street"
        )
        cap = _tighter(cap, NUMBER_NOT_FOUND_CONFIDENCE_CAP)
        return _result(Granularity.STREET, s_lat, s_lng, None, warnings, hypothesis, trips, cap)

    if unit:
        hit = match_unit(rows, unit)
        if hit is not None:
            hit, ref, alias_warnings = follow_alias(
                hit, counted_principal(principal, trips, "row_by_pid")
            )
            warnings.extend(alias_warnings)
            if hit.is_alias:
                cap = _tighter(cap, ALIAS_CONFIDENCE_CAP)
            return _result(
                Granularity.UNIT,
                hit.lat,
                hit.lng,
                hit,
                warnings,
                hypothesis,
                trips,
                cap,
                ref,
            )
        # The building exists, the flat does not. Return the building rather than
        # nothing, and say which part failed.
        warnings.append(
            f"unit {unit} not found at {number_first} {street.street_key}; returned the building"
        )
        cap = _tighter(cap, UNIT_NOT_FOUND_CONFIDENCE_CAP)

    chosen, exact = match_range(rows, number_last)
    if number_last and not exact:
        # The row we settled for may carry a range of its own, and naming it is
        # the difference between "we guessed" and "17-99 is stored as 17-23".
        stored = (
            f"{chosen.number_first}-{chosen.number_last}"
            if chosen is not None and chosen.number_last
            else ""
        )
        warnings.append(
            f"no G-NAF row for the range {number_first}-{number_last}; "
            f"matched {number_first} alone"
            + (f", whose stored range is {stored}" if stored else "")
        )
        cap = _tighter(cap, RANGE_MISMATCH_CONFIDENCE_CAP)
    if chosen is None:
        return _result(Granularity.STREET, s_lat, s_lng, None, warnings, hypothesis, trips, cap)

    chosen, ref, alias_warnings = follow_alias(
        chosen, counted_principal(principal, trips, "row_by_pid")
    )
    warnings.extend(alias_warnings)
    if chosen.is_alias:
        cap = _tighter(cap, ALIAS_CONFIDENCE_CAP)
    return _result(
        Granularity.ADDRESS,
        chosen.lat,
        chosen.lng,
        chosen,
        warnings,
        hypothesis,
        trips,
        cap,
        ref,
    )


def _result(
    granularity: str,
    lat: float | None,
    lng: float | None,
    record: GnafRecord | None,
    warnings: Sequence[str],
    hypothesis,
    trips: RoundTrips,
    cap: float | None = None,
    principal_ref: PrincipalRef | None = None,
) -> LookupResult:
    # An alias's canonical identity is its principal's; everything else is its
    # own. Blank when there is no G-NAF row to identify.
    if principal_ref is not None:
        canonical = principal_ref.pid
    elif record is not None:
        canonical = record.address_detail_pid
    else:
        canonical = ""
    return LookupResult(
        granularity=granularity,
        lat=lat,
        lng=lng,
        record=record,
        warnings=tuple(warnings),
        hypothesis=hypothesis,
        principal=principal_ref,
        canonical_pid=canonical,
        confidence_cap=cap,
        round_trips=trips.count,
        round_trip_labels=tuple(trips.labels),
    )
