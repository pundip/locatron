"""Street matching for a locality hypothesis.

TEMPORARY DEPARTURE FROM CLAUDE.md, approved deliberately.

CLAUDE.md:114-117 says the street gazetteer lives in local SQLite, not Python
memory and not MySQL on the request path, because per-worker Python objects are
duplicated and refcounting defeats copy-on-write. That is still the target. None
of it exists yet: there is no `db/local.py`, no streets loader, no
`locatron.build.refresh` (the module `locatron-build.service` already points at),
and `config.sqlite_path` is declared and unread.

So street rows come from `locatron_street` in MySQL for now, through exactly one
function. The rules that make that swappable rather than load-bearing:

- `streets_for_many()` is the only place street rows are read. Nothing outside
  this module queries `locatron_street`.
- The matcher goes through the batch form, so resolving costs one round trip for
  all three locality hypotheses rather than three.
- The service account's existing SELECT grant is enough. No new grants.
- No in-process cache. The SQLite mirror is the fix; a stopgap cache would
  become the thing nobody removes.
- The matcher takes its source as an argument, so tests inject a fixture and run
  without a database, and the swap to SQLite changes one function.

`address_ref` is never touched here. Fuzzy matching happens against the few
dozen streets of one locality, never across the 532,182-row table and never
across the 15.9M address rows.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import text

from locatron.db import mysql

#: (state, locality, postcode) — the grain of `locatron_street`, and what a
#: locality hypothesis supplies.
LocalityKey = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class StreetRow:
    """One street in one locality, as `locatron_street` stores it.

    `street_name` and `street_type` are kept apart rather than only as the
    collapsed `street_key`, because matching scores them separately: the input's
    trailing token against the type, the rest against the name. The key is still
    carried, since it is what the build script already collapsed
    ('HAMILTON','CR') and ('HAMILTON CR','') onto and what a caller reports.
    """

    state: str
    locality: str
    postcode: str
    street_key: str
    street_name: str
    street_type: str
    street_suffix: str
    address_count: int
    lat: float | None = None
    lng: float | None = None

    @property
    def key(self) -> LocalityKey:
        return (self.state, self.locality, self.postcode)


#: What the matcher needs from a street store. Satisfied by
#: `streets_for_many()` and by a test fixture, which is the point.
StreetSource = Callable[[Sequence[LocalityKey]], Mapping[LocalityKey, tuple[StreetRow, ...]]]

_COLUMNS = (
    "state, locality, postcode, street_key, street_name, street_type, "
    "street_suffix, address_count, lat, lng"
)


def streets_for_many(
    keys: Sequence[LocalityKey],
) -> Mapping[LocalityKey, tuple[StreetRow, ...]]:
    """Every street of each locality, in one query.

    The only place street rows are read. One statement for all the keys, because
    the matcher runs against the top three locality hypotheses and three round
    trips on a latency-sensitive path would be three times the cost of one.

    Keys are matched on (state, locality, postcode), which is the leading part
    of the table's primary key, so each disjunct is index-backed.
    """
    unique = list(dict.fromkeys(keys))
    if not unique:
        return {}

    clauses: list[str] = []
    params: dict[str, str] = {}
    for i, (state, locality, postcode) in enumerate(unique):
        clauses.append(f"(state = :s{i} AND locality = :l{i} AND postcode = :p{i})")
        params[f"s{i}"] = state
        params[f"l{i}"] = locality
        # Defensive pad. The column is char(4), but NT is 0800-0899 and a
        # caller that went through an int anywhere would arrive with '800'.
        params[f"p{i}"] = postcode.rjust(4, "0")

    sql = f"SELECT {_COLUMNS} FROM locatron_street WHERE {' OR '.join(clauses)}"  # noqa: S608

    out: dict[LocalityKey, list[StreetRow]] = {k: [] for k in unique}
    with mysql.session_scope() as s:
        for r in s.execute(text(sql), params).mappings():
            row = StreetRow(
                state=r["state"],
                locality=r["locality"],
                postcode=str(r["postcode"]).rjust(4, "0"),
                street_key=r["street_key"],
                street_name=r["street_name"],
                street_type=r["street_type"],
                street_suffix=r["street_suffix"],
                address_count=int(r["address_count"] or 0),
                lat=float(r["lat"]) if r["lat"] is not None else None,
                lng=float(r["lng"]) if r["lng"] is not None else None,
            )
            out.setdefault(row.key, []).append(row)

    # Biggest first, so a caller taking the head gets the most-addressed street
    # and two runs never disagree.
    return {
        k: tuple(sorted(v, key=lambda r: (-r.address_count, r.street_key))) for k, v in out.items()
    }


def streets_for(key: LocalityKey) -> tuple[StreetRow, ...]:
    """One locality's streets. A convenience over the batch form, not a second
    query path."""
    return streets_for_many([key]).get(key, ())
