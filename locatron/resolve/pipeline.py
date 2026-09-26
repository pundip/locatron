"""The resolver entry point.

`resolve_one()` is the single call every consumer goes through: the CLI now, the
FastAPI app later, the bulk exporter after that. Keeping it a plain library
function — no request object, no session parameter, no HTTP types — is what lets
the API wrap it later without a refactor.

The one invariant that matters here: this never raises for unresolvable input.
Empty strings, whitespace, and garbage all come back as granularity=UNRESOLVED
with confidence 0.0, because a Databricks job handles a column far more
gracefully than an exception. See CLAUDE.md.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from locatron.normalize import NORM_VERSION, normalize
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


def resolve_one(
    text: str,
    *,
    country_bias: str | None = None,
    min_granularity: Granularity | None = None,
    include_candidates: bool = False,
) -> ResolveResponse:
    """Resolve one loose location string.

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

    try:
        winner, candidates, warnings, normalized = world.resolve_place(
            text,
            country_bias=country_bias,
            min_granularity=min_granularity,
            include_candidates=include_candidates,
        )
    except Exception as exc:  # noqa: BLE001 - the no-raise invariant outranks it
        return unresolved(
            text,
            warnings=[f"resolver error: {type(exc).__name__}: {exc}"],
            elapsed_ms=elapsed(),
        )

    if winner is None:
        return unresolved(text, normalized=normalized, warnings=warnings, elapsed_ms=elapsed())

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
        warnings=warnings,
        norm_version=NORM_VERSION,
        elapsed_ms=elapsed(),
        resolved_at=datetime.now(UTC),
    )
