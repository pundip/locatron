"""Interactive resolve API, served on :8080 behind nginx.

A thin wrapper over `pipeline.resolve_one()`. No resolution logic lives here.

Endpoints are `def`, not `async def`: FastAPI runs them in a threadpool, which
is what keeps the synchronous SQLAlchemy layer safe. See CLAUDE.md.

nginx strips the /locatron prefix before proxying, so routes are declared
without it. `root_path` exists only so /docs and /openapi.json generate URLs
that work from the public side.

Unresolvable input is HTTP 200 with granularity=unresolved. An unexpected
error is HTTP 500, but still a JSON body in the ResolveResponse shape with the
error in `warnings`, so a Databricks job never has to parse an HTML page.
"""

from __future__ import annotations

import logging
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated, Any
from urllib.parse import urlsplit, urlunsplit

import structlog
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from locatron.config import get_settings
from locatron.db import mysql
from locatron.normalize import normalize
from locatron.resolve.pipeline import resolve_one, unresolved
from locatron.schemas import (
    BatchResolveRequest,
    BatchResolveResponse,
    Granularity,
    ResolveRequest,
    ResolveResponse,
)

# Paths never logged. Health checks fire every few seconds and would drown out
# real traffic in journald.
_UNLOGGED_PATHS = frozenset({"/healthz"})


def configure_logging(level: str) -> None:
    """One JSON object per line on stdout, which journald captures as-is."""
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        # Not cached: a cached logger ignores later reconfiguration, which
        # breaks structlog.testing.capture_logs(). The cost is negligible next
        # to a resolve.
        cache_logger_on_first_use=False,
    )


log = structlog.get_logger("locatron.api")

try:
    __version__ = version("locatron")
except PackageNotFoundError:
    __version__ = "0.0.0"


def prefix_location(location: str, root_path: str, host: str) -> str:
    """Put `root_path` back on a redirect target the app generated.

    Starlette builds its slash redirects from the path it sees, which nginx
    has already stripped the /locatron prefix from, so the Location header
    comes out as /v1/resolve and 404s at the edge.

    Only same-host, absolute-path targets are touched. A redirect to another
    host belongs to whoever wrote it, and a relative one already resolves
    against the request URL.
    """
    if not root_path or not location:
        return location

    parts = urlsplit(location)
    if parts.netloc and parts.netloc != host:
        return location
    if not parts.path.startswith("/"):
        return location
    if parts.path == root_path or parts.path.startswith(f"{root_path}/"):
        return location

    return urlunsplit(parts._replace(path=root_path + parts.path))


class RootPathRedirectMiddleware:
    """Rewrite Location headers so redirects keep working behind the prefix."""

    def __init__(self, app: ASGIApp, root_path: str) -> None:
        self.app = app
        self.root_path = root_path.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.root_path:
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                # Read the host here rather than up front: uvicorn's
                # proxy-header handling rewrites it from X-Forwarded-Host.
                host = next(
                    (v.decode() for k, v in scope["headers"] if k == b"host"),
                    "",
                )
                message["headers"] = [
                    (
                        (k, prefix_location(v.decode(), self.root_path, host).encode())
                        if k.lower() == b"location"
                        else (k, v)
                    )
                    for k, v in message["headers"]
                ]
            await send(message)

        await self.app(scope, receive, send_wrapper)


class RequestLogMiddleware:
    """Pure ASGI middleware: one log line per request.

    Not BaseHTTPMiddleware, which runs the endpoint in a separate task and
    complicates both exception propagation and shared request state. Endpoints
    add resolve details to `request.state.log_fields`, which lives in the ASGI
    scope and so is visible here after the endpoint's threadpool call returns.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in _UNLOGGED_PATHS:
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = 500
        state = scope.setdefault("state", {})
        state["log_fields"] = {}

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # Query string is deliberately left out: only the normalized input
            # is logged, never raw parameters or headers.
            log.info(
                "request",
                method=scope["method"],
                path=scope["path"],
                status=status,
                latency_ms=round((time.perf_counter() - started) * 1000.0, 3),
                **state.get("log_fields", {}),
            )


def _record(request: Request, resp: ResolveResponse) -> None:
    request.state.log_fields = {
        "normalized": resp.normalized,
        "granularity": resp.granularity.value,
        "confidence": resp.confidence,
        "match_method": resp.match_method.value,
    }


def _safe_normalize(text: str) -> str:
    # The error envelope must not itself fail if normalize() is what broke.
    try:
        return normalize(text)
    except Exception:
        return ""


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="Locatron",
        version=__version__,
        description="Resolve a loose location string to the most complete location possible.",
        root_path=settings.root_path,
    )
    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(RootPathRedirectMiddleware, root_path=settings.root_path)

    @app.exception_handler(Exception)
    def unhandled(request: Request, exc: Exception) -> JSONResponse:
        text = getattr(request.state, "query_text", "") or ""
        log.error(
            "unhandled_error",
            path=request.url.path,
            error=type(exc).__name__,
            exc_info=exc,
        )
        body = unresolved(
            text,
            normalized=_safe_normalize(text),
            warnings=[f"internal error: {type(exc).__name__}: {exc}"],
        )
        return JSONResponse(status_code=500, content=body.model_dump(mode="json"))

    @app.get("/", tags=["ops"])
    def index() -> dict[str, Any]:
        """What the bare public URL lands on.

        Without a route here, /locatron/ resolves to no route at all. Links are
        prefixed with root_path so they work from the public side, where nginx
        has stripped /locatron before the app sees the path.
        """
        base = settings.root_path.rstrip("/")
        return {
            "service": "locatron",
            "version": __version__,
            "docs": f"{base}/docs",
            "openapi": f"{base}/openapi.json",
            "healthz": f"{base}/healthz",
            "resolve": f"{base}/v1/resolve",
        }

    @app.get("/healthz", tags=["ops"])
    def healthz() -> JSONResponse:
        """Liveness plus MySQL reachability. 503 when MySQL is unreachable."""
        db = mysql.health()
        ok = bool(db.get("connected"))
        return JSONResponse(
            status_code=200 if ok else 503,
            content={"status": "ok" if ok else "degraded", "version": __version__, "mysql": db},
        )

    @app.get("/v1/resolve", response_model=ResolveResponse, tags=["resolve"])
    def resolve_get(
        request: Request,
        text: Annotated[str, Query(description="The location string to resolve.")],
        country_bias: str | None = None,
        min_granularity: Granularity | None = None,
        include_candidates: bool = False,
    ) -> ResolveResponse:
        """Query-parameter form of POST /v1/resolve, for curl and browsers."""
        request.state.query_text = text
        resp = resolve_one(
            text,
            country_bias=country_bias,
            min_granularity=min_granularity,
            include_candidates=include_candidates,
        )
        _record(request, resp)
        return resp

    @app.post("/v1/resolve", response_model=ResolveResponse, tags=["resolve"])
    def resolve_post(request: Request, body: ResolveRequest) -> ResolveResponse:
        """Resolve one string. Unresolvable input is 200 with granularity=unresolved."""
        request.state.query_text = body.text
        resp = resolve_one(
            body.text,
            country_bias=body.country_bias,
            min_granularity=body.min_granularity,
            include_candidates=body.include_candidates,
        )
        _record(request, resp)
        return resp

    @app.post("/v1/resolve/batch", response_model=BatchResolveResponse, tags=["resolve"])
    def resolve_batch(request: Request, body: BatchResolveRequest) -> BatchResolveResponse:
        """Resolve up to 1000 strings, sequentially for now."""
        started = time.perf_counter()
        results = [
            resolve_one(
                item,
                country_bias=body.country_bias,
                min_granularity=body.min_granularity,
            )
            for item in body.items
        ]
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
        by_granularity: dict[str, int] = {}
        for r in results:
            by_granularity[r.granularity.value] = by_granularity.get(r.granularity.value, 0) + 1
        request.state.log_fields = {"count": len(results), "by_granularity": by_granularity}
        return BatchResolveResponse(results=results, count=len(results), elapsed_ms=elapsed_ms)

    return app


app = create_app()
