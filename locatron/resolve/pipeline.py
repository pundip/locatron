"""The resolver entry point.

`resolve_one()` is the single call every consumer goes through: the CLI, the
FastAPI app, and the bulk exporter later. Keeping it a plain library function —
no request object, no session parameter, no HTTP types — is what lets the API
wrap it without a refactor.

It picks between two paths. The Australian address path parses the input into
locality, street, number and unit and dives once into `address_ref`; the world
place path matches a loose place name against cities, countries and buckets. The
world path is the default and the fallback, and the AU path has to be earned by
the triggers in CLAUDE.md's Routing section.

The one invariant that matters here: this never raises for unresolvable input.
Empty strings, whitespace, and garbage all come back as granularity=UNRESOLVED
with confidence 0.0, because a Databricks job handles a column far more
gracefully than an exception. See CLAUDE.md.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from locatron.config import get_settings
from locatron.normalize import NORM_VERSION, normalize
from locatron.resolve import au as au_path
from locatron.resolve import unresolved as unresolved_log
from locatron.resolve import world
from locatron.schemas import Granularity, MatchMethod, ResolveResponse


def unresolved(
    text: str,
    *,
    normalized: str | None = None,
    warnings: list[str] | None = None,
    elapsed_ms: float | None = None,
) -> ResolveResponse:
    """The floor response. Always HTTP 200 at the API layer."""
    return ResolveResponse(
        query=text,
        normalized=normalized if normalized is not None else normalize(text),
        resolved=False,
        granularity=Granularity.UNRESOLVED,
        confidence=0.0,
        match_method=MatchMethod.NONE,
        warnings=warnings or [],
        norm_version=NORM_VERSION,
        elapsed_ms=elapsed_ms,
        resolved_at=datetime.now(UTC),
    )


def _try_au(text: str) -> tuple[au_path.AuParse | None, list[str]]:
    """Parse for the AU path, or explain why that could not be attempted.

    A failure here falls through to the world path rather than failing the
    request. The street stage reads the SQLite mirror, and a worker whose mirror
    is missing or stale already refuses to start -- so by the time a request
    arrives, a failure here is something unexpected rather than the silent-miss
    case the mirror exists to prevent. Falling through keeps world traffic
    answering, and the error travels in `warnings` so it is visible rather than
    silent.
    """
    try:
        return au_path.parse(text), []
    except Exception as exc:  # noqa: BLE001 - see the docstring
        return None, [f"AU path unavailable, used the world path: {type(exc).__name__}: {exc}"]


def resolve_one(
    text: str,
    *,
    country_bias: str | None = None,
    min_granularity: Granularity | None = None,
    include_candidates: bool = False,
    record_unresolved: bool | None = None,
) -> ResolveResponse:
    """Resolve one loose location string or Australian address.

    `record_unresolved` defaults to the `log_unresolved` setting. Pass False for
    synthetic inputs -- the golden set, tests -- so they do not accumulate in the
    feedback table that is meant to hold real traffic.

    Never raises. An unresolvable input, a malformed one, and an unexpected
    internal failure all return a well-formed response — the last of those with
    the exception recorded in `warnings`, so a bulk job surfaces the problem
    without dying on row 400,000 of 2,000,000.
    """
    started = time.perf_counter()

    def elapsed() -> float:
        return round((time.perf_counter() - started) * 1000.0, 3)

    if not text or not text.strip():
        return unresolved(text or "", normalized="", elapsed_ms=elapsed())

    if record_unresolved is None:
        record_unresolved = get_settings().log_unresolved

    warnings: list[str] = []
    unexplained: tuple[str, ...] = ()
    try:
        parsed, au_warnings = _try_au(text)
        warnings.extend(au_warnings)

        if parsed is not None and au_path.takes_au_path(parsed)[0]:
            answer = au_path.resolve_au(parsed)
            unexplained = answer.unexplained
            response = _au_response(text, answer, warnings, elapsed())
        else:
            response = _world_response(
                text,
                warnings,
                elapsed,
                country_bias=country_bias,
                min_granularity=min_granularity,
                include_candidates=include_candidates,
            )
    except Exception as exc:  # noqa: BLE001 - the no-raise invariant outranks it
        return unresolved(
            text,
            warnings=[*warnings, f"resolver error: {type(exc).__name__}: {exc}"],
            elapsed_ms=elapsed(),
        )

    if record_unresolved:
        # `record()` swallows its own database errors, so this is defence in
        # depth: the no-raise invariant must not depend on the feedback log being
        # bug-free. A thin answer is still an answer.
        try:
            unresolved_log.record(response, unexplained=unexplained)
        except Exception as exc:  # noqa: BLE001 - the invariant outranks the log
            response.warnings.append(
                f"unresolved log failed: {type(exc).__name__}: {exc}"
            )
    return response


def _au_response(
    text: str, answer: au_path.AuAnswer, warnings: list[str], elapsed_ms: float
) -> ResolveResponse:
    country = au_path.australia()
    return ResolveResponse(
        query=text,
        normalized=normalize(text),
        resolved=answer.granularity is not Granularity.UNRESOLVED,
        granularity=answer.granularity,
        confidence=answer.confidence,
        match_method=answer.match_method,
        country=country.to_schema() if country else None,
        admin1=answer.admin1,
        locality=answer.locality,
        postcode=answer.postcode,
        geo=answer.geo,
        au_address=answer.au_address,
        canonical_pid=answer.canonical_pid,
        principal=answer.principal,
        candidates=answer.candidates,
        warnings=[*warnings, *answer.warnings],
        norm_version=NORM_VERSION,
        elapsed_ms=elapsed_ms,
        resolved_at=datetime.now(UTC),
    )


def _world_response(
    text: str,
    warnings: list[str],
    elapsed,
    *,
    country_bias: str | None,
    min_granularity: Granularity | None,
    include_candidates: bool,
) -> ResolveResponse:
    winner, candidates, world_warnings, normalized = world.resolve_place(
        text,
        country_bias=country_bias,
        min_granularity=min_granularity,
        include_candidates=include_candidates,
    )
    if winner is None:
        return unresolved(
            text,
            normalized=normalized,
            warnings=[*warnings, *world_warnings],
            elapsed_ms=elapsed(),
        )
    return ResolveResponse(
        query=text,
        normalized=normalized,
        resolved=True,
        granularity=winner.granularity,
        confidence=round(min(1.0, max(0.0, winner.score)), 4),
        match_method=winner.match_method,
        country=world.to_country_schema(winner.country),
        admin1=world.to_admin1_schema(winner),
        locality=winner.locality,
        postcode=winner.postcode,
        geo=winner.geo,
        candidates=candidates,
        warnings=[*warnings, *world_warnings],
        norm_version=NORM_VERSION,
        elapsed_ms=elapsed(),
        resolved_at=datetime.now(UTC),
    )
