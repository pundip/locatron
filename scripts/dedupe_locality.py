#!/usr/bin/env python3
"""Collapse locality rows that differ only in punctuation.

Runs last in the rebuild, after scripts/normalize_pass.py, and that ordering is
the whole point: the grouping key is the `norm_key` that pass fills in, so this
script neither reimplements normalisation nor folds punctuation in SQL. There is
still exactly one normalize(). See CLAUDE.md.

The duplicates it removes come from locatron_locality's unique index being
(state, locality, postcode) on the *raw* locality. G-NAF writes D'AGUILAR with
an ASCII apostrophe (0x27) and AusPost writes D’AGUILAR with U+2019, so the
index sees two distinct localities where normalize() sees one. The AusPost twin
carries no addresses and is flagged is_postal_only, which is simply wrong for a
place that has 1,069 G-NAF addresses.

    python scripts/dedupe_locality.py            # report only, writes nothing
    python scripts/dedupe_locality.py --apply    # remap, collapse, delete

Reporting is the default and --apply is required to write, because the bare
command deletes rows and a destructive default is the wrong way round for
something that runs at the end of a rebuild.

Needs the locatron_build credentials. The service user has no write grant on the
gazetteer tables, which is deliberate. Writes only to locatron_locality and
locatron_locality_alias; never to an upstream table.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from typing import Any, NamedTuple

from sqlalchemy import bindparam, text

from locatron.db.mysql import session_scope

# Every member of every duplicate group, in one read. The ORDER BY encodes the
# survivor rule: in_gnaf first, then the biggest, then the oldest id as a
# stable tiebreak. The first row of each group is the survivor.
MEMBERS_SQL = """
SELECT l.locality_id, l.locality, l.state, l.postcode, l.norm_key,
       l.in_gnaf, l.in_auspost, l.is_postal_only,
       l.address_count, l.street_count, l.geo_source, l.delivery_type,
       l.lat, l.lng, l.postcode_lat, l.postcode_lng,
       l.sa2_name, l.sa3_name, l.sa4_name, l.lga_name, l.remoteness
FROM locatron_locality l
JOIN (
    SELECT state, postcode, norm_key
    FROM locatron_locality
    WHERE norm_key IS NOT NULL AND norm_key <> ''
    GROUP BY state, postcode, norm_key
    HAVING COUNT(*) > 1
) d ON d.state = l.state AND d.postcode = l.postcode AND d.norm_key = l.norm_key
ORDER BY l.state, l.postcode, l.norm_key,
         l.in_gnaf DESC, l.address_count DESC, l.locality_id ASC
"""

ALIASES_SQL = """
SELECT alias_id, alias_norm_key, alias_display, locality_id, alias_type, confidence
FROM locatron_locality_alias
WHERE locality_id IN :ids
ORDER BY alias_display, confidence DESC, alias_id ASC
"""

_IDS = bindparam("ids", expanding=True)

DUP_GROUPS_SQL = """
SELECT COUNT(*) FROM (
    SELECT state, postcode, norm_key
    FROM locatron_locality
    WHERE norm_key IS NOT NULL AND norm_key <> ''
    GROUP BY state, postcode, norm_key
    HAVING COUNT(*) > 1
) t
"""

# Columns worth reporting when the dropped row has a value the survivor lacks.
# Dropping such a value is real data loss, so it is surfaced rather than
# silently accepted; merging them is deliberately out of scope here.
CARRIED = (
    "delivery_type",
    "lat",
    "lng",
    "postcode_lat",
    "postcode_lng",
    "sa2_name",
    "sa3_name",
    "sa4_name",
    "lga_name",
    "remoteness",
)


class Group(NamedTuple):
    state: str
    postcode: str
    norm_key: str
    survivor: dict[str, Any]
    dropped: list[dict[str, Any]]

    @property
    def in_auspost(self) -> int:
        """in_auspost OR-ed across the group."""
        return int(any(r["in_auspost"] for r in [self.survivor, *self.dropped]))

    @property
    def is_postal_only(self) -> int:
        """Recomputed, not inherited: a place with G-NAF addresses is not postal."""
        return int(not self.survivor["in_gnaf"])

    def lost_fields(self) -> dict[int, list[str]]:
        """Per dropped id, the columns it fills and the survivor does not."""
        out: dict[int, list[str]] = {}
        for row in self.dropped:
            gaps = [c for c in CARRIED if row.get(c) is not None and self.survivor.get(c) is None]
            if gaps:
                out[row["locality_id"]] = gaps
        return out


class AliasPlan(NamedTuple):
    remap: list[tuple[int, int]]
    """(alias_id, survivor_locality_id) — rows that move."""
    delete: list[int]
    """alias_ids collapsed away because the survivor already has that display."""
    collisions: list[tuple[str, int, int]]
    """(alias_display, kept alias_id, dropped alias_id) for the report."""


def load_groups() -> list[Group]:
    with session_scope() as s:
        rows = [dict(r) for r in s.execute(text(MEMBERS_SQL)).mappings()]

    by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_key[(r["state"], r["postcode"], r["norm_key"])].append(r)

    # dict preserves the SQL order, so members[0] is the survivor.
    return [
        Group(state=k[0], postcode=k[1], norm_key=k[2], survivor=v[0], dropped=v[1:])
        for k, v in by_key.items()
    ]


def plan_aliases(groups: list[Group]) -> AliasPlan:
    """Work out which alias rows move to the survivor and which collapse.

    The unique index is (alias_display, locality_id), so remapping a dropped
    row's alias onto the survivor collides whenever both already carry the same
    display. Within a display the keeper is the highest confidence, then the
    lowest alias_id, so an exact canonical hit is never displaced by a
    low-trust auspost_variant.
    """
    ids = [g.survivor["locality_id"] for g in groups]
    ids += [r["locality_id"] for g in groups for r in g.dropped]
    if not ids:
        return AliasPlan([], [], [])

    with session_scope() as s:
        alias_rows = [
            dict(r) for r in s.execute(text(ALIASES_SQL).bindparams(_IDS), {"ids": ids}).mappings()
        ]

    return _plan_from_rows(groups, alias_rows)


def _plan_from_rows(groups: list[Group], alias_rows: list[dict[str, Any]]) -> AliasPlan:
    """The planning itself, with no database in the way so it can be tested."""
    survivor_of: dict[int, int] = {}
    for g in groups:
        sid = g.survivor["locality_id"]
        survivor_of[sid] = sid
        for r in g.dropped:
            survivor_of[r["locality_id"]] = sid

    # (survivor_id, alias_display) -> every alias row that wants to live there.
    buckets: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for a in alias_rows:
        buckets[(survivor_of[a["locality_id"]], a["alias_display"])].append(a)

    remap: list[tuple[int, int]] = []
    delete: list[int] = []
    collisions: list[tuple[str, int, int]] = []

    for (sid, display), members in buckets.items():
        members.sort(key=lambda a: (-float(a["confidence"]), a["alias_id"]))
        keeper, *rest = members
        if keeper["locality_id"] != sid:
            remap.append((keeper["alias_id"], sid))
        for other in rest:
            delete.append(other["alias_id"])
            collisions.append((display, keeper["alias_id"], other["alias_id"]))

    return AliasPlan(remap=remap, delete=delete, collisions=collisions)


def report(groups: list[Group], plan: AliasPlan) -> None:
    if not groups:
        print("No duplicate (state, postcode, norm_key) groups. Nothing to do.")
        return

    print(f"{len(groups)} duplicate group(s) on (state, postcode, norm_key)\n")
    for g in groups:
        s = g.survivor
        print(f"  {g.norm_key}  [{g.state}/{g.postcode}]")
        print(
            f"    KEEP  id={s['locality_id']:<6} {s['locality']!r:<22} "
            f"gnaf={s['in_gnaf']} auspost={s['in_auspost']} po={s['is_postal_only']} "
            f"ac={s['address_count']} sc={s['street_count']}"
        )
        for r in g.dropped:
            print(
                f"    DROP  id={r['locality_id']:<6} {r['locality']!r:<22} "
                f"gnaf={r['in_gnaf']} auspost={r['in_auspost']} po={r['is_postal_only']} "
                f"ac={r['address_count']} sc={r['street_count']}"
            )
        if (s["in_auspost"], s["is_postal_only"]) != (g.in_auspost, g.is_postal_only):
            print(
                f"    SET   in_auspost {s['in_auspost']} -> {g.in_auspost}, "
                f"is_postal_only {s['is_postal_only']} -> {g.is_postal_only}"
            )
        for dropped_id, gaps in g.lost_fields().items():
            print(f"    NOTE  id={dropped_id} is the only row with: {', '.join(gaps)}")
        print()

    print(f"aliases to remap:   {len(plan.remap)}")
    for alias_id, sid in plan.remap:
        print(f"    alias {alias_id} -> locality_id {sid}")
    print(f"aliases to collapse: {len(plan.delete)}")
    for display, kept, dropped in plan.collisions:
        print(f"    {display!r}: keep alias {kept}, delete alias {dropped}")

    total_dropped = sum(len(g.dropped) for g in groups)
    print(f"\nlocality rows to delete: {total_dropped}    survivors to update: {len(groups)}")


def apply(groups: list[Group], plan: AliasPlan) -> None:
    """Remap, collapse and delete in a single transaction.

    Order matters inside it: the alias deletes run before the remaps, or a
    remap onto a display the survivor already carries would violate
    uq_lla_alias_target mid-transaction.
    """
    dropped_ids = [r["locality_id"] for g in groups for r in g.dropped]

    with session_scope() as s:
        if plan.delete:
            s.execute(
                text("DELETE FROM locatron_locality_alias WHERE alias_id IN :ids").bindparams(
                    bindparam("ids", expanding=True)
                ),
                {"ids": plan.delete},
            )
        for alias_id, sid in plan.remap:
            s.execute(
                text("UPDATE locatron_locality_alias SET locality_id = :sid WHERE alias_id = :aid"),
                {"sid": sid, "aid": alias_id},
            )
        for g in groups:
            s.execute(
                text(
                    "UPDATE locatron_locality "
                    "SET in_auspost = :ap, is_postal_only = :po "
                    "WHERE locality_id = :lid"
                ),
                {"ap": g.in_auspost, "po": g.is_postal_only, "lid": g.survivor["locality_id"]},
            )
        if dropped_ids:
            s.execute(
                text("DELETE FROM locatron_locality WHERE locality_id IN :ids").bindparams(
                    bindparam("ids", expanding=True)
                ),
                {"ids": dropped_ids},
            )

    print(
        f"Applied: {len(dropped_ids)} locality rows deleted, {len(groups)} survivors "
        f"updated, {len(plan.remap)} aliases remapped, {len(plan.delete)} aliases collapsed."
    )


def verify() -> bool:
    with session_scope() as s:
        dups = s.execute(text(DUP_GROUPS_SQL)).scalar()
        orphans = s.execute(
            text(
                "SELECT COUNT(*) FROM locatron_locality_alias a "
                "LEFT JOIN locatron_locality l ON l.locality_id = a.locality_id "
                "WHERE l.locality_id IS NULL"
            )
        ).scalar()
    print(f"  duplicate (state, postcode, norm_key) groups: {dups}")
    print(f"  alias rows pointing at a missing locality:     {orphans}")
    return not dups and not orphans


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Actually remap, collapse and delete. Without it, nothing is written.",
    )
    # Accepted so that spelling out the default does not fail, and so a habit of
    # typing it cannot silently become an apply.
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Report only. This is the default; the flag is accepted for clarity.",
    )
    args = ap.parse_args()

    if args.apply and args.dry_run:
        print("--apply and --dry-run contradict each other", file=sys.stderr)
        return 1

    groups = load_groups()
    plan = plan_aliases(groups)
    report(groups, plan)

    if not args.apply:
        print("\nNothing written. Re-run with --apply to make these changes.")
        return 0

    if groups:
        apply(groups, plan)

    print("\nVerification:")
    return 0 if verify() else 1


if __name__ == "__main__":
    sys.exit(main())
