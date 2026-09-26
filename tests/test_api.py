"""HTTP API tests.

Most tests replace `resolve_one` and `mysql.health` so they depend on neither
database contents nor reachability. A few end-to-end cases run against the
live ReferenceDB and skip when it is unreachable.

The app is driven through a minimal in-process ASGI client rather than
FastAPI's TestClient, which needs httpx, and httpx is not a dependency.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import pytest
from structlog.testing import capture_logs

from locatron.api import app as api
from locatron.db import mysql
from locatron.resolve.pipeline import unresolved
from locatron.schemas import Country, Granularity, MatchMethod, ResolveResponse

# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


@dataclass
class Resp:
    status: int
    headers: dict[str, str]
    body: bytes
    server_error: BaseException | None = None

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass
class AsgiClient:
    """Just enough of an HTTP client to drive the app in-process.

    Like TestClient(raise_server_exceptions=False): Starlette re-raises an
    unhandled exception after the error response has been sent, and that
    re-raise is recorded rather than propagated so the response can be checked.
    """

    app: Any
    root_path: str = ""

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Resp:
        body = b"" if json_body is None else json.dumps(json_body).encode()
        headers = [(b"host", b"testserver")]
        if json_body is not None:
            headers.append((b"content-type", b"application/json"))
            headers.append((b"content-length", str(len(body)).encode()))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "root_path": self.root_path,
            "query_string": urlencode(params or {}).encode(),
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
        sent: list[dict[str, Any]] = []
        delivered = False

        async def receive() -> dict[str, Any]:
            nonlocal delivered
            if delivered:
                await asyncio.sleep(3600)
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        err: BaseException | None = None
        try:
            asyncio.run(self.app(scope, receive, send))
        except Exception as exc:  # noqa: BLE001 - see class docstring
            err = exc

        start = next(m for m in sent if m["type"] == "http.response.start")
        return Resp(
            status=start["status"],
            headers={k.decode().lower(): v.decode() for k, v in start["headers"]},
            body=b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body"),
            server_error=err,
        )

    def get(self, path: str, **kw: Any) -> Resp:
        return self.request("GET", path, **kw)

    def post(self, path: str, json: Any = None, **kw: Any) -> Resp:
        return self.request("POST", path, json_body=json, **kw)


@pytest.fixture
def client() -> AsgiClient:
    return AsgiClient(api.app)


def _fake_resolved(text: str, **kwargs: Any) -> ResolveResponse:
    return ResolveResponse(
        query=text,
        normalized=text.upper(),
        resolved=True,
        granularity=Granularity.CITY,
        confidence=0.9,
        match_method=MatchMethod.CITY_EXACT,
        country=Country(name="Australia", alpha2="AU", alpha3="AUS"),
        locality="MELBOURNE",
        warnings=[f"kwargs={sorted(kwargs.items())}"],
        norm_version="test",
    )


@pytest.fixture
def fake_resolver(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake(text: str, **kwargs: Any) -> ResolveResponse:
        calls.append((text, kwargs))
        return _fake_resolved(text, **kwargs)

    monkeypatch.setattr(api, "resolve_one", fake)
    return calls


def _stable(body: dict[str, Any]) -> dict[str, Any]:
    """Drop the per-call fields so two responses for one input compare equal."""
    return {k: v for k, v in body.items() if k not in {"elapsed_ms", "resolved_at"}}


def _db_available() -> bool:
    try:
        return bool(mysql.health().get("connected"))
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_available(), reason="ReferenceDB unreachable")


# ---------------------------------------------------------------------------
# /
# ---------------------------------------------------------------------------


def test_index_returns_json(client: AsgiClient) -> None:
    """The bare public URL must answer, not redirect. No database involved."""
    r = client.get("/")
    assert r.status == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["service"] == "locatron"
    assert body["version"]
    # Links carry the public prefix, since nginx strips it before the app.
    assert body["docs"] == "/locatron/docs"
    assert body["healthz"] == "/locatron/healthz"
    assert body["resolve"] == "/locatron/v1/resolve"


# ---------------------------------------------------------------------------
# redirects behind the /locatron prefix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        # Starlette's slash redirect, built from the path nginx already stripped.
        ("http://testserver/v1/resolve?text=x", "http://testserver/locatron/v1/resolve?text=x"),
        ("/v1/resolve", "/locatron/v1/resolve"),
        # Already prefixed, so left alone. Prefixing twice is the worse bug.
        ("/locatron/v1/resolve", "/locatron/v1/resolve"),
        ("/locatron", "/locatron"),
        # A path that merely starts with the same letters is not prefixed yet.
        ("/locatronic", "/locatron/locatronic"),
        # Someone else's host, and a relative target, are not ours to rewrite.
        ("https://elsewhere.example/v1/resolve", "https://elsewhere.example/v1/resolve"),
        ("docs", "docs"),
        ("", ""),
    ],
)
def test_prefix_location(location: str, expected: str) -> None:
    assert api.prefix_location(location, "/locatron", "testserver") == expected


def test_prefix_location_noop_without_root_path() -> None:
    assert api.prefix_location("/v1/resolve", "", "testserver") == "/v1/resolve"


def test_trailing_slash_redirect_keeps_the_prefix(client: AsgiClient, fake_resolver: list) -> None:
    """The edge only knows /locatron/..., so the Location header must carry it."""
    r = client.get("/v1/resolve/", params={"text": "Melbourne"})
    assert r.status == 307
    assert r.headers["location"] == "http://testserver/locatron/v1/resolve?text=Melbourne"
    assert fake_resolver == []


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------


def test_healthz_ok_shape(client: AsgiClient, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"connected": True, "version": "8.0.0", "locatron_locality": 18500}
    monkeypatch.setattr(mysql, "health", lambda: payload)
    r = client.get("/healthz")
    assert r.status == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["mysql"] == payload
    assert "version" in body


def test_healthz_503_when_mysql_unreachable(
    client: AsgiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        mysql, "health", lambda: {"connected": False, "error": "OperationalError: refused"}
    )
    r = client.get("/healthz")
    assert r.status == 503
    assert r.json()["status"] == "degraded"
    assert r.json()["mysql"]["connected"] is False


@pytest.mark.parametrize("warm", [True, False])
def test_healthz_reports_worker_warmth(
    client: AsgiClient, monkeypatch: pytest.MonkeyPatch, warm: bool
) -> None:
    """A cold worker must be visible rather than silent."""
    monkeypatch.setattr(mysql, "health", lambda: {"connected": True})
    monkeypatch.setattr(api, "is_warm", lambda: warm)
    r = client.get("/healthz")
    assert r.json()["warm"] is warm


def test_cold_worker_is_still_healthy(client: AsgiClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Warmth must not gate the status code.

    A cold worker answers correctly, just slowly. Returning 503 would pull a
    working worker out of nginx's rotation over a latency problem.
    """
    monkeypatch.setattr(mysql, "health", lambda: {"connected": True})
    monkeypatch.setattr(api, "is_warm", lambda: False)
    r = client.get("/healthz")
    assert r.status == 200
    assert r.json()["status"] == "ok"
    assert r.json()["warm"] is False


def test_healthz_is_not_logged(client: AsgiClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mysql, "health", lambda: {"connected": True})
    with capture_logs() as logs:
        client.get("/healthz")
    assert logs == []


# ---------------------------------------------------------------------------
# /v1/resolve
# ---------------------------------------------------------------------------


def test_get_and_post_agree(client: AsgiClient, fake_resolver: list) -> None:
    opts = {"country_bias": "AUS", "min_granularity": "city", "include_candidates": True}
    g = client.get("/v1/resolve", params={"text": "Greater Melbourne", **opts})
    p = client.post("/v1/resolve", json={"text": "Greater Melbourne", **opts})
    assert g.status == p.status == 200
    assert _stable(g.json()) == _stable(p.json())
    # Both forms passed the options through identically.
    assert fake_resolver[0] == fake_resolver[1]
    assert fake_resolver[0][1] == {
        "country_bias": "AUS",
        "min_granularity": Granularity.CITY,
        "include_candidates": True,
    }


def test_get_requires_text(client: AsgiClient, fake_resolver: list) -> None:
    assert client.get("/v1/resolve").status == 422
    assert fake_resolver == []


def test_malformed_body_is_422(client: AsgiClient, fake_resolver: list) -> None:
    assert client.post("/v1/resolve", json={"txt": "oops"}).status == 422
    assert client.post("/v1/resolve", json={"text": "x", "min_granularity": "galaxy"}).status == 422


def test_unresolvable_is_200_not_an_error(
    client: AsgiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "resolve_one", lambda text, **_: unresolved(text))
    for r in (
        client.get("/v1/resolve", params={"text": "qwxzv zzqq"}),
        client.post("/v1/resolve", json={"text": "qwxzv zzqq"}),
    ):
        assert r.status == 200
        body = r.json()
        assert body["granularity"] == "unresolved"
        assert body["confidence"] == 0.0
        assert body["resolved"] is False


def test_empty_text_is_200_unresolved(client: AsgiClient) -> None:
    """The real resolver short-circuits blank input before any database work."""
    r = client.get("/v1/resolve", params={"text": "   "})
    assert r.status == 200
    assert r.json()["granularity"] == "unresolved"
    assert r.json()["confidence"] == 0.0


def test_resolve_logs_one_line_with_resolve_fields(client: AsgiClient, fake_resolver: list) -> None:
    with capture_logs() as logs:
        client.get("/v1/resolve", params={"text": "Greater Melbourne"})
    assert len(logs) == 1
    line = logs[0]
    assert line["path"] == "/v1/resolve"
    assert line["status"] == 200
    assert isinstance(line["latency_ms"], float)
    assert line["normalized"] == "GREATER MELBOURNE"
    assert line["granularity"] == "city"
    assert line["confidence"] == 0.9
    assert line["match_method"] == "city_exact"


# ---------------------------------------------------------------------------
# /v1/resolve/batch
# ---------------------------------------------------------------------------


def test_batch(client: AsgiClient, fake_resolver: list) -> None:
    items = ["Sydney Australia", "Las Vegas", "Delhi"]
    r = client.post("/v1/resolve/batch", json={"items": items, "country_bias": "AUS"})
    assert r.status == 200
    body = r.json()
    assert body["count"] == 3
    assert [x["query"] for x in body["results"]] == items
    assert isinstance(body["elapsed_ms"], float)
    assert all(kw["country_bias"] == "AUS" for _, kw in fake_resolver)


def test_batch_accepts_1000(client: AsgiClient, fake_resolver: list) -> None:
    r = client.post("/v1/resolve/batch", json={"items": ["x"] * 1000})
    assert r.status == 200
    assert r.json()["count"] == 1000


def test_batch_rejects_1001(client: AsgiClient, fake_resolver: list) -> None:
    r = client.post("/v1/resolve/batch", json={"items": ["x"] * 1001})
    assert r.status == 422
    assert fake_resolver == []


# ---------------------------------------------------------------------------
# exception handler
# ---------------------------------------------------------------------------


def test_unexpected_error_returns_json_envelope(
    client: AsgiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(text: str, **_: Any) -> ResolveResponse:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(api, "resolve_one", boom)
    with capture_logs() as logs:
        r = client.post("/v1/resolve", json={"text": "Las Vegas"})

    assert r.status == 500
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()  # parses, so not HTML
    assert body["query"] == "Las Vegas"
    assert body["granularity"] == "unresolved"
    assert body["confidence"] == 0.0
    assert any("RuntimeError" in w and "kaboom" in w for w in body["warnings"])
    # Same envelope as a normal response.
    ResolveResponse.model_validate(body)

    request_lines = [x for x in logs if x["event"] == "request"]
    assert len(request_lines) == 1
    assert request_lines[0]["status"] == 500


# ---------------------------------------------------------------------------
# OpenAPI behind the /locatron prefix
# ---------------------------------------------------------------------------


def test_openapi_uses_root_path(client: AsgiClient) -> None:
    spec = client.get("/openapi.json").json()
    assert {"url": "/locatron"} in spec.get("servers", [])
    assert "/v1/resolve" in spec["paths"]
    assert "/v1/resolve/batch" in spec["paths"]
    get_params = {p["name"] for p in spec["paths"]["/v1/resolve"]["get"]["parameters"]}
    assert get_params == {"text", "country_bias", "min_granularity", "include_candidates"}


# ---------------------------------------------------------------------------
# end to end, live database
# ---------------------------------------------------------------------------


@needs_db
def test_e2e_healthz(client: AsgiClient) -> None:
    r = client.get("/healthz")
    assert r.status == 200
    assert r.json()["mysql"]["connected"] is True


@needs_db
def test_e2e_get_and_post_agree(client: AsgiClient) -> None:
    g = client.get("/v1/resolve", params={"text": "Greater Melbourne"})
    p = client.post("/v1/resolve", json={"text": "Greater Melbourne"})
    assert g.status == p.status == 200
    assert _stable(g.json()) == _stable(p.json())
    assert g.json()["country"]["alpha3"] == "AUS"


@needs_db
def test_e2e_unresolvable_is_200(client: AsgiClient) -> None:
    r = client.get("/v1/resolve", params={"text": "qwxzv zzqq plorb"})
    assert r.status == 200
    assert r.json()["granularity"] == "unresolved"
    assert r.json()["confidence"] == 0.0


@needs_db
def test_e2e_batch(client: AsgiClient) -> None:
    items = ["Sydney Australia", "Las Vegas", "qwxzv zzqq plorb"]
    r = client.post("/v1/resolve/batch", json={"items": items})
    assert r.status == 200
    body = r.json()
    assert body["count"] == 3
    assert body["results"][0]["country"]["alpha3"] == "AUS"
    assert body["results"][1]["country"]["alpha3"] == "USA"
    assert body["results"][2]["granularity"] == "unresolved"
