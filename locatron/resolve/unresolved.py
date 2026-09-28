"""The feedback loop: what Locatron could not resolve well.

Reviewing `locatron_unresolved` by `hit_count` and promoting the real entries
into `locatron_locality_alias` is what makes the service better over months. It
only works if the resolver actually writes to it, so `record()` is called on
every answer that came back thin.

Three rules, in order of importance:

1. A failed write never fails the request. The table is a feedback loop, not part
   of the answer, and a resolve that raises because a log insert deadlocked would
   be a far worse bug than a missing row.
2. It writes to `locatron_unresolved` and nothing else. This is one of the two
   tables the service account has write access to; the grants enforce it as well
   as this module does.
3. It is keyed on (norm_key, norm_version), matching the table's primary key, so
   a NORM_VERSION bump starts a fresh count rather than merging two
   normalisations into one row.
"""

from __future__ import annotations

import structlog
from sqlalchemy import text

from locatron.db import mysql
from locatron.normalize import NORM_VERSION
from locatron.schemas import Granularity, ResolveResponse, at_least

log = structlog.get_logger(__name__)

#: Longest value the table's varchar(255) columns hold. Truncated rather than
#: rejected: a 4KB scraped string is exactly the kind of input worth recording.
_MAX_LEN = 255

_UPSERT = text(
    """
    INSERT INTO locatron_unresolved
        (norm_key, norm_version, sample_raw, hit_count,
         best_granularity, best_confidence, match_method, reviewed)
    VALUES
        (:norm_key, :norm_version, :sample_raw, 1,
         :granularity, :confidence, :match_method, 0)
    ON DUPLICATE KEY UPDATE
        hit_count        = hit_count + 1,
        sample_raw       = :sample_raw,
        best_granularity = IF(:confidence > COALESCE(best_confidence, -1),
                              :granularity, best_granularity),
        match_method     = IF(:confidence > COALESCE(best_confidence, -1),
                              :match_method, match_method),
        best_confidence  = GREATEST(COALESCE(best_confidence, -1), :confidence)
    """
)
"""Assignment order matters: best_granularity and match_method are decided
against the *old* best_confidence, so that column is updated last. MySQL
evaluates ON DUPLICATE KEY UPDATE left to right, and putting best_confidence
first would make every comparison after it trivially false."""


def _engine():
    """The engine `record()` writes through.

    A seam, so a test can make writing impossible without touching
    `locatron.db.mysql`, which every read in the process shares. Patching that
    module instead takes the gazetteer down with it, and a resolve that cannot
    load its gazetteer fails long before it reaches the feedback log -- which is
    exactly the confusing failure this indirection avoids.
    """
    return mysql.get_engine()


def is_worth_recording(response: ResolveResponse, unexplained: tuple[str, ...] = ()) -> bool:
    """Whether this answer belongs in the feedback loop.

    Two cases, and only two:

    - Nothing resolved at all. Always worth a row.
    - It resolved no finer than a locality *and* left input tokens unexplained.
      The unexplained part is what makes it actionable: 'Carrum Downs VIC'
      resolving to a locality is the correct and complete answer and would only
      be noise here, while 'Greater Dandenong Region VIC' resolving to a locality
      with two tokens spare is a missing alias waiting to be added.
    """
    if response.granularity is Granularity.UNRESOLVED:
        return True
    # at_least(LOCALITY, got) reads as "is a locality at least as specific as
    # what we got" -- true exactly when `got` is locality-level or coarser.
    return bool(unexplained) and at_least(Granularity.LOCALITY, response.granularity)


def record(
    response: ResolveResponse,
    *,
    unexplained: tuple[str, ...] = (),
    norm_key: str | None = None,
) -> bool:
    """Log one thin answer, incrementing `hit_count` if the key is already there.

    Returns whether a row was written, for tests and for the caller's own
    logging. Never raises: a database that is down, read-only, or missing the
    table costs a log line and nothing else.
    """
    if not is_worth_recording(response, unexplained):
        return False

    key = (norm_key if norm_key is not None else response.normalized).strip()
    if not key:
        # An empty input normalises to an empty key, which is not a distinct
        # thing anyone can act on and would collide with every other blank.
        return False

    params = {
        "norm_key": key[:_MAX_LEN],
        "norm_version": NORM_VERSION,
        "sample_raw": (response.query or "")[:_MAX_LEN],
        "granularity": response.granularity.value,
        "confidence": float(response.confidence),
        "match_method": response.match_method.value,
    }
    try:
        with _engine().begin() as conn:
            conn.execute(_UPSERT, params)
    except Exception as exc:  # noqa: BLE001 - rule 1: never fail the request
        log.warning(
            "unresolved_log_failed",
            norm_key=params["norm_key"],
            error=f"{type(exc).__name__}: {exc}",
        )
        return False
    return True
