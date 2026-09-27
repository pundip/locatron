"""MySQL access.

Synchronous by design. FastAPI runs `def` endpoints in a threadpool, which
gets us concurrency without an async MySQL driver. See CLAUDE.md.

Upstream tables (address_ref, AustralianPostcodes, Cities, Countries,
aus_state_bucket, country_bucket) are read-only. Nothing in this package
writes to them.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from locatron.config import get_settings

READ_ONLY_TABLES = frozenset(
    {
        "address_ref",
        "AustralianPostcodes",
        "Cities",
        "Countries",
        "aus_state_bucket",
        "country_bucket",
    }
)

DERIVED_TABLES = frozenset(
    {
        "locatron_street",
        "locatron_locality",
        "locatron_locality_alias",
        "locatron_unresolved",
    }
)


@lru_cache
def get_engine() -> Engine:
    s = get_settings()
    return create_engine(
        s.mysql_url,
        pool_size=s.mysql_pool_size,
        max_overflow=s.mysql_pool_max_overflow,
        pool_recycle=s.mysql_pool_recycle_seconds,
        pool_pre_ping=True,
        future=True,
    )


@lru_cache
def get_read_engine() -> Engine:
    """A second pool whose connections are read-only for their whole life.

    `SET SESSION TRANSACTION READ ONLY` is session-scoped, not statement-scoped, so
    issuing it inside a borrowed connection leaves it set when that connection goes
    back to the pool -- and the next borrower cannot write. That silently disabled
    the `locatron_unresolved` log: any resolve that dived into `address_ref` set the
    flag, and the next feedback write to land on the same pooled connection failed
    with "Cannot execute statement in a READ ONLY transaction". The write is best
    effort, so it was logged and swallowed, and the table simply stayed empty.

    Two pools fixes it at the root rather than by resetting the flag afterwards,
    which an exception mid-query would skip. This one is read-only by construction,
    set once per new connection; `get_engine()` is for everything else and its
    connections are never touched. A read path that starts writing now fails
    immediately and always, rather than depending on which connection it drew.

    Costs a second pool of `mysql_pool_size`. Worth it: the alternative is a
    correctness property that holds or not depending on pool checkout order.
    """
    s = get_settings()
    engine = create_engine(
        s.mysql_url,
        pool_size=s.mysql_pool_size,
        max_overflow=s.mysql_pool_max_overflow,
        pool_recycle=s.mysql_pool_recycle_seconds,
        pool_pre_ping=True,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _set_read_only(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
        finally:
            cursor.close()

    return engine


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


@lru_cache
def get_read_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_read_engine(), expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    sm = get_sessionmaker()
    session = sm()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def read_session_scope() -> Iterator[Session]:
    """A session on the read-only pool. Use this for every upstream-table query."""
    sm = get_read_sessionmaker()
    session = sm()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session


def fetch_all(sql: str, **params: Any) -> list[dict[str, Any]]:
    with session_scope() as s:
        return [dict(r) for r in s.execute(text(sql), params).mappings()]


def fetch_one(sql: str, **params: Any) -> dict[str, Any] | None:
    with session_scope() as s:
        row = s.execute(text(sql), params).mappings().first()
        return dict(row) if row else None


def scalar(sql: str, **params: Any) -> Any:
    with session_scope() as s:
        return s.execute(text(sql), params).scalar()


def health() -> dict[str, Any]:
    """Connectivity plus a sanity check on the derived tables.

    Deliberately verifies norm_key is populated. A gazetteer with NULL
    norm_key values will silently match nothing, which is far worse than
    failing a health check.
    """
    out: dict[str, Any] = {"connected": False}
    try:
        with session_scope() as s:
            out["connected"] = s.execute(text("SELECT 1")).scalar() == 1
            out["version"] = s.execute(text("SELECT VERSION()")).scalar()

            for table in sorted(DERIVED_TABLES):
                try:
                    out[table] = s.execute(
                        text(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
                    ).scalar()
                except Exception:
                    out[table] = None

            out["locality_norm_key_missing"] = s.execute(
                text(
                    "SELECT COUNT(*) FROM locatron_locality WHERE norm_key IS NULL OR norm_key = ''"
                )
            ).scalar()
            out["alias_norm_key_missing"] = s.execute(
                text(
                    "SELECT COUNT(*) FROM locatron_locality_alias "
                    "WHERE alias_norm_key IS NULL OR alias_norm_key = ''"
                )
            ).scalar()
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out
