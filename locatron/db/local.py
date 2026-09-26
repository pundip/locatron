"""The local SQLite street mirror, opened read-only, one connection per worker.

CLAUDE.md:114-117: the street gazetteer lives here rather than in Python memory,
because per-worker objects are duplicated and CPython refcounting dirties
copy-on-write pages, so `--preload` buys nothing. SQLite lets the workers share
the same pages through the OS page cache.

Two rules make that work:

    One connection per worker, opened in `post_worker_init` and never before the
    fork. A sqlite3 connection carries a file descriptor and per-connection
    state; inheriting one across fork means several processes sharing one cursor
    position and one lock, which corrupts reads under concurrency.

    Read-only at the URI level (`mode=ro`), so the serving process cannot write
    to the mirror even by accident. The build is the only writer, and it writes a
    different file and renames it into place.

Staleness fails loudly. A missing file or a `norm_version` that disagrees with
the running code raises at worker startup, because the alternative -- falling
back to MySQL -- is the silent-miss failure CLAUDE.md is built to avoid. The
check is local: worker startup reads the mirror's own meta table and queries no
database.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from locatron.config import get_settings
from locatron.normalize import NORM_VERSION


class MirrorError(RuntimeError):
    """The street mirror is missing, unreadable, or built by another version."""


@dataclass(frozen=True, slots=True)
class MirrorMeta:
    """What a mirror says about itself."""

    norm_version: str
    snapshot_id: str
    source_digest: str
    row_count: int
    built_at: str
    path: str

    @property
    def is_current(self) -> bool:
        return self.norm_version == NORM_VERSION


# One connection per process. A lock around creation only: sqlite3 connections
# are safe to use from several threads once `check_same_thread=False` is set, and
# FastAPI runs sync endpoints in a threadpool.
_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


def mirror_path() -> Path:
    return Path(get_settings().sqlite_path)


def read_meta(path: str | Path | None = None) -> MirrorMeta:
    """Read a mirror's meta table without holding the shared connection open.

    Used by `locatron check` and by the startup check, both of which want to
    inspect a file they are not about to serve from.
    """
    target = Path(path) if path is not None else mirror_path()
    if not target.exists():
        raise MirrorError(
            f"street mirror missing at {target}. Build it with `uv run locatron build streets`."
        )
    try:
        conn = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise MirrorError(f"cannot open street mirror at {target}: {exc}") from exc
    try:
        rows = dict(conn.execute("SELECT key, value FROM meta"))
    except sqlite3.Error as exc:
        raise MirrorError(f"street mirror at {target} has no readable meta: {exc}") from exc
    finally:
        conn.close()

    return MirrorMeta(
        norm_version=rows.get("norm_version", ""),
        snapshot_id=rows.get("snapshot_id", ""),
        source_digest=rows.get("source_digest", ""),
        row_count=int(rows.get("row_count", 0) or 0),
        built_at=rows.get("built_at", ""),
        path=str(target),
    )


def check_mirror(path: str | Path | None = None) -> MirrorMeta:
    """Read the meta and refuse a mirror the running code cannot trust.

    No MySQL query: this runs at worker startup, on the latency-sensitive
    process, and a database round trip there is exactly what the mirror exists to
    remove.
    """
    meta = read_meta(path)
    if not meta.is_current:
        raise MirrorError(
            f"street mirror at {meta.path} was built with NORM_VERSION "
            f"{meta.norm_version!r}, this code is {NORM_VERSION!r}. Rebuild it with "
            f"`uv run locatron build streets`. Refusing to serve rather than "
            f"falling back to MySQL, because a version mismatch misses silently."
        )
    if meta.row_count <= 0:
        raise MirrorError(f"street mirror at {meta.path} reports {meta.row_count} rows")
    return meta


def connect() -> sqlite3.Connection:
    """This worker's read-only connection, opened on first use.

    Called from `post_worker_init`, so the connection is created after the fork
    and belongs to one process.
    """
    global _conn
    if _conn is not None:
        return _conn
    with _lock:
        if _conn is not None:
            return _conn
        meta = check_mirror()
        conn = sqlite3.connect(
            f"file:{Path(meta.path).as_posix()}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        # Reads only, so no journal to negotiate and no write lock to wait on.
        conn.execute("PRAGMA query_only = ON")
        _conn = conn
        return _conn


def close() -> None:
    """Drop this process's connection. For tests and for a clean shutdown."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def is_open() -> bool:
    return _conn is not None
