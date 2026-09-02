#!/usr/bin/env python3
"""Backfill norm_key columns on the locatron_* tables.

Imports normalize() from the package rather than carrying its own copy, so
there is exactly one implementation. See CLAUDE.md.

    python scripts/normalize_pass.py            # fill NULLs only
    python scripts/normalize_pass.py --all      # recompute after a version bump
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

from locatron.db.mysql import session_scope
from locatron.normalize import NORM_VERSION, normalize

TARGETS = [
    ("locatron_locality", "locality_id", "locality", "norm_key"),
    ("locatron_locality_alias", "alias_id", "alias_display", "alias_norm_key"),
]

BATCH = 5000


def backfill(table: str, pk: str, src: str, dst: str, only_null: bool) -> int:
    where = f"WHERE {dst} IS NULL OR {dst} = ''" if only_null else ""
    with session_scope() as s:
        rows = s.execute(text(f"SELECT {pk}, {src} FROM {table} {where}")).all()  # noqa: S608

    if not rows:
        print(f"  {table}: nothing to do")
        return 0

    updates = [{"nk": normalize(src_val), "nv": NORM_VERSION, "pk": pk_val} for pk_val, src_val in rows]
    stmt = text(f"UPDATE {table} SET {dst} = :nk, norm_version = :nv WHERE {pk} = :pk")  # noqa: S608

    done = 0
    for i in range(0, len(updates), BATCH):
        with session_scope() as s:
            s.execute(stmt, updates[i : i + BATCH])
        done += len(updates[i : i + BATCH])
        print(f"  {table}: {done}/{len(updates)}", end="\r", flush=True)

    print(f"  {table}: {done} rows updated       ")
    return done


def verify() -> bool:
    ok = True
    with session_scope() as s:
        for table, _, _, dst in TARGETS:
            missing = s.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE {dst} IS NULL OR {dst} = ''")  # noqa: S608
            ).scalar()
            stale = s.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE norm_version <> :v"),  # noqa: S608
                {"v": NORM_VERSION},
            ).scalar()
            status = "ok" if not (missing or stale) else "PROBLEM"
            print(f"  {table}: missing={missing} stale_version={stale}  [{status}]")
            ok = ok and not (missing or stale)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="Recompute every row, not just NULLs.")
    args = ap.parse_args()

    print(f"Normalising with NORM_VERSION={NORM_VERSION}")
    for table, pk, src, dst in TARGETS:
        backfill(table, pk, src, dst, only_null=not args.all)

    print("\nVerification:")
    return 0 if verify() else 1


if __name__ == "__main__":
    sys.exit(main())
