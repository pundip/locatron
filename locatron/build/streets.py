"""Build the local SQLite street mirror from `locatron_street`.

This is what makes CLAUDE.md:114-117 true: the street gazetteer belongs in local
SQLite, where gunicorn workers share pages through the OS page cache, rather than
in per-worker Python memory or behind a MySQL round trip on the request path.

Reads only. The session is set `TRANSACTION READ ONLY` before any query, so the
build cannot write to MySQL even when run as the service account, and it needs no
grant beyond SELECT.

The file is built into a temporary path in the same directory and `os.replace()`d
into position, so a reader either sees the previous mirror or the new one, never a
half-built file. Same directory because `os.replace` is only atomic within a
filesystem.

Three things must hold before a byte is written, and the build refuses otherwise:

    normalize_pass has run. `locatron_street` has no norm_key of its own -- that
    pass targets `locatron_locality` and `locatron_locality_alias` only -- so the
    proof lives one table over: their norm_key columns must be populated. The
    mirror is built last in the rebuild order, so a NULL there means the order
    was not followed and the gazetteer would miss silently.

    The source is whole. No blank `street_key` or `street_name`, which is what a
    half-run build SQL leaves behind.

    Postcodes are four characters. NT is 0800-0899 and the canonical form is
    zero-padded everywhere inside Locatron.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from locatron.db import mysql
from locatron.normalize import NORM_VERSION

SCHEMA = """
CREATE TABLE streets (
    state         TEXT    NOT NULL,
    locality      TEXT    NOT NULL,
    postcode      TEXT    NOT NULL,
    street_key    TEXT    NOT NULL,
    street_name   TEXT    NOT NULL,
    street_type   TEXT    NOT NULL,
    street_suffix TEXT    NOT NULL,
    address_count INTEGER NOT NULL,
    lat           REAL,
    lng           REAL
);

-- The only access pattern: every street of one locality. Matches the leading
-- part of the MySQL primary key, so the two stores are queried the same way.
CREATE INDEX ix_streets_locality ON streets (state, locality, postcode);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: Field separator CHAR(31) and NULL sentinel CHAR(1): both non-printable and
#: impossible in these columns, so NULL, '' and any real value stay distinct.
#:
#: CONCAT rather than CONCAT_WS, with COALESCE on every column. CONCAT_WS skips
#: NULLs instead of representing them, which collides as soon as a value contains
#: the separator -- CONCAT_WS('|','A',NULL,'B') and CONCAT_WS('|','A|B',NULL,NULL)
#: are both 'A|B'. CHAR(31) cannot appear in street data so CONCAT_WS would have
#: been safe here by accident; the explicit form removes the dependence on that.
#:
#: SUM, not BIT_XOR: XOR cancels identical rows to zero. The primary key makes
#: duplicates impossible, so this is belt-and-braces, but SUM costs nothing.
DIGEST_SQL = """
SELECT COUNT(*) AS rows_,
       SUM(CRC32(CONCAT(
           COALESCE(state,         CHAR(1)), CHAR(31),
           COALESCE(locality,      CHAR(1)), CHAR(31),
           COALESCE(postcode,      CHAR(1)), CHAR(31),
           COALESCE(street_key,    CHAR(1)), CHAR(31),
           COALESCE(street_name,   CHAR(1)), CHAR(31),
           COALESCE(street_type,   CHAR(1)), CHAR(31),
           COALESCE(street_suffix, CHAR(1)), CHAR(31),
           COALESCE(CAST(address_count AS CHAR), CHAR(1)), CHAR(31),
           COALESCE(CAST(lat AS CHAR), CHAR(1)), CHAR(31),
           COALESCE(CAST(lng AS CHAR), CHAR(1))
       ))) AS crc_sum
FROM locatron_street
"""

_SELECT_SQL = """
SELECT state, locality, postcode, street_key, street_name, street_type,
       street_suffix, address_count, lat, lng
FROM locatron_street
"""

#: Rows per executemany. Large enough that the per-statement cost disappears,
#: small enough that the whole table is never in memory twice.
BATCH = 20_000


class BuildError(RuntimeError):
    """The source is not fit to mirror. Raised before anything is written."""


@dataclass(frozen=True, slots=True)
class SourceDigest:
    """What the source looked like, recorded so drift is detectable later."""

    rows: int
    crc_sum: int
    snapshot_id: str

    @property
    def digest(self) -> str:
        return f"{self.rows}-{self.crc_sum}"


def read_only_session():
    """A MySQL session that cannot write, whoever it is running as."""
    scope = mysql.session_scope()
    session = scope.__enter__()
    session.execute(text("SET SESSION TRANSACTION READ ONLY"))
    return scope, session


def preflight(session) -> None:
    """Refuse to build from a source that is not whole."""
    missing_locality = session.execute(
        text("SELECT COUNT(*) FROM locatron_locality WHERE norm_key IS NULL OR norm_key = ''")
    ).scalar()
    missing_alias = session.execute(
        text(
            "SELECT COUNT(*) FROM locatron_locality_alias "
            "WHERE alias_norm_key IS NULL OR alias_norm_key = ''"
        )
    ).scalar()
    if missing_locality or missing_alias:
        raise BuildError(
            f"normalize_pass has not run: {missing_locality} locality and "
            f"{missing_alias} alias rows have no norm_key. The mirror is built "
            f"last in the rebuild order -- run scripts/normalize_pass.py first."
        )

    blank = session.execute(
        text("SELECT COUNT(*) FROM locatron_street WHERE street_key = '' OR street_name = ''")
    ).scalar()
    if blank:
        raise BuildError(
            f"{blank} locatron_street rows have a blank street_key or "
            f"street_name. Rerun sql/build_locatron_street.sql."
        )

    short = session.execute(
        text("SELECT COUNT(*) FROM locatron_street WHERE CHAR_LENGTH(postcode) < 4")
    ).scalar()
    if short:
        raise BuildError(
            f"{short} locatron_street rows have a postcode shorter than 4 "
            f"characters. NT is 0800-0899; LPAD on load."
        )


def source_digest(session) -> SourceDigest:
    """Row count and content digest of `locatron_street`, plus the snapshot id.

    The snapshot id is borrowed from `locatron_locality`, which is the only
    derived table carrying one. It describes the locality build rather than the
    street build -- a locality-only rebuild advances it while the street data is
    untouched -- so the digest, not the snapshot id, is what actually detects
    drift.
    """
    row = session.execute(text(DIGEST_SQL)).mappings().first()
    snapshot = (
        session.execute(text("SELECT snapshot_id FROM locatron_locality LIMIT 1")).scalar()
        or "unknown"
    )
    return SourceDigest(
        rows=int(row["rows_"] or 0), crc_sum=int(row["crc_sum"] or 0), snapshot_id=str(snapshot)
    )


def build(path: str | Path, *, progress=None) -> dict[str, str]:
    """Build the mirror at `path`. Returns the meta it wrote.

    Never leaves a partial file in place: the work happens in a temporary file
    beside the target and is moved over it only once the row count matches.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    scope, session = read_only_session()
    try:
        preflight(session)
        digest = source_digest(session)
        if not digest.rows:
            raise BuildError("locatron_street is empty")

        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".building", dir=str(target.parent)
        )
        os.close(fd)
        tmp = Path(tmp_name)

        try:
            written = _write(tmp, session, digest, progress=progress)
            if written != digest.rows:
                raise BuildError(
                    f"row count mismatch: wrote {written} to SQLite, "
                    f"locatron_street has {digest.rows}"
                )
            # Atomic within the directory, so a reader never sees a partial file.
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    finally:
        scope.__exit__(None, None, None)

    return read_meta(target)


def _write(tmp: Path, session, digest: SourceDigest, *, progress=None) -> int:
    """Stream the source into a fresh SQLite file. Returns rows written."""
    conn = sqlite3.connect(str(tmp))
    try:
        # No journal and no fsync: the file is disposable until os.replace, and
        # a crash mid-build leaves the previous mirror untouched.
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.executescript(SCHEMA)

        written = 0
        batch: list[tuple] = []
        result = session.execute(text(_SELECT_SQL))
        for r in result:
            postcode = str(r[2]).rjust(4, "0")
            batch.append(
                (
                    r[0],
                    r[1],
                    postcode,
                    r[3],
                    r[4],
                    r[5],
                    r[6],
                    int(r[7] or 0),
                    float(r[8]) if r[8] is not None else None,
                    float(r[9]) if r[9] is not None else None,
                )
            )
            if len(batch) >= BATCH:
                conn.executemany("INSERT INTO streets VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
                written += len(batch)
                batch.clear()
                if progress:
                    progress(written, digest.rows)
        if batch:
            conn.executemany("INSERT INTO streets VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
            written += len(batch)
            if progress:
                progress(written, digest.rows)

        # Asserted here as well as in preflight: this is the last point at which
        # a padding slip could have crept in.
        bad = conn.execute("SELECT COUNT(*) FROM streets WHERE LENGTH(postcode) < 4").fetchone()[0]
        if bad:
            raise BuildError(f"{bad} rows reached SQLite with a postcode shorter than 4")

        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [
                ("norm_version", NORM_VERSION),
                ("snapshot_id", digest.snapshot_id),
                ("source_digest", digest.digest),
                ("row_count", str(written)),
                ("built_at", datetime.now(UTC).isoformat(timespec="seconds")),
            ],
        )
        conn.commit()
        conn.execute("ANALYZE")
        conn.commit()
        return written
    finally:
        conn.close()


def read_meta(path: str | Path) -> dict[str, str]:
    """The meta table of an existing mirror, read-only."""
    uri = f"file:{Path(path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return {k: v for k, v in conn.execute("SELECT key, value FROM meta")}
    finally:
        conn.close()


def build_with_timing(path: str | Path, *, progress=None) -> tuple[dict[str, str], float]:
    started = time.perf_counter()
    meta = build(path, progress=progress)
    return meta, time.perf_counter() - started
