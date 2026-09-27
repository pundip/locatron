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
    with mysql.session_scope() as s:
        s.execute(text("SET SESSION TRANSACTION READ ONLY"))
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
    with mysql.session_scope() as s:
        s.execute(text("SET SESSION TRANSACTION READ ONLY"))
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


def follow_alias(
    record: GnafRecord, principal: PrincipalSource
) -> tuple[GnafRecord, tuple[str, ...]]:
    """Resolve an alias row to its principal, or keep the alias if it dangles.

    An alias row is a real answer -- the input used a name G-NAF records as an
    alias -- so it is never discarded. It is followed, because the principal is
    the canonical record and the one a consumer should store. 5.27% of rows are
    aliases.
    """
    if not record.is_alias:
        return record, ()

    target = principal(record.principal_pid)
    if target is None:
        return record, (
            f"matched an alias record ({record.address_detail_pid}) whose "
            f"principal {record.principal_pid!r} could not be found; returning "
            f"the alias",
        )
    alias_name = " ".join(x for x in (record.street_name, record.street_type) if x)
    principal_name = " ".join(x for x in (target.street_name, target.street_type) if x)
    return target, (
        f"input used the alias street {alias_name!r}; returned the principal "
        f"record {principal_name!r} ({target.address_detail_pid})",
    )
