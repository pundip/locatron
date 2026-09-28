#!/usr/bin/env python3
"""
Latency check for the Locatron resolve endpoint.

The question this answers: was the 6-second first response a one-off cold
start, or is something querying MySQL on every request?

Those look identical if you only run two requests. With N gunicorn workers and
round-robin dispatch, a lazily-loaded gazetteer means roughly the first N
requests are slow and the rest are fast. A per-request database round trip
means every request is slow. So this runs a burst, reports the first ten
individually, then reports steady-state percentiles separately.

Standard library only, so it runs on Windows, the edge server, or the
container with no install.

Usage:
    # Full chain, through Cloudflare
    python latency_check.py --url https://urlloom.com/locatron

    # Straight to the app on the container, no proxy in the way
    python latency_check.py --url http://127.0.0.1:8080 --no-prefix

    # Container nginx, needs the shared secret
    python latency_check.py --url http://127.0.0.1:8000/locatron \
        --header "X-Locatron-Edge: $SECRET"

    # More samples
    python latency_check.py --url https://urlloom.com/locatron -n 60
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Varied on purpose. A single repeated query could be served from a cache
# somewhere and would hide a real per-request cost.
QUERIES = [
    "Greater Melbourne",
    "Sydney Australia",
    "New York",
    "Las Vegas",
    "Delhi",
    "Perth",
    "Brisbane QLD",
    "Adelaide",
    "London",
    "Zurich",
    "Sao Paulo",
    "Hobart Tasmania",
    "Darwin NT",
    "Canberra ACT",
    "Auckland New Zealand",
    "Singapore",
    "Greater Western Sydney",
    "Geelong Victoria",
    "Newcastle NSW",
    "Wollongong",
]


class Result:
    __slots__ = ("query", "wall_ms", "server_ms", "status", "granularity", "error")

    def __init__(self, query: str) -> None:
        self.query = query
        self.wall_ms = 0.0
        self.server_ms: float | None = None
        self.status = 0
        self.granularity = ""
        self.error = ""


# urllib's default User-Agent is "Python-urllib/3.x", which Cloudflare blocks
# with a 403 before the request ever reaches the origin. Any ordinary browser
# string gets through. Override with --header if needed.
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


def build_request(base: str, path: str, query: str, headers: dict[str, str]):
    url = f"{base.rstrip('/')}{path}?" + urllib.parse.urlencode({"text": query})
    req = urllib.request.Request(url, method="GET")
    if not any(k.lower() == "user-agent" for k in headers):
        req.add_header("User-Agent", DEFAULT_UA)
    req.add_header("Accept", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    return req


def fire(base: str, path: str, query: str, headers: dict[str, str], timeout: float) -> Result:
    r = Result(query)
    req = build_request(base, path, query, headers)
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            r.status = resp.status
        r.wall_ms = (time.perf_counter() - start) * 1000
        try:
            payload = json.loads(body)
            # elapsed_ms is the server's own measure of resolution work,
            # excluding network. The gap between it and wall_ms is transport.
            r.server_ms = payload.get("elapsed_ms")
            r.granularity = payload.get("granularity", "")
        except (ValueError, AttributeError):
            r.error = "response was not JSON"
    except urllib.error.HTTPError as exc:
        r.wall_ms = (time.perf_counter() - start) * 1000
        r.status = exc.code
        r.error = f"HTTP {exc.code}"
        if exc.code == 403:
            r.error += " (Cloudflare bot block? try --header 'User-Agent: ...' "
            r.error += "or run against the container directly)"
    except Exception as exc:  # noqa: BLE001 - any failure is worth reporting
        r.wall_ms = (time.perf_counter() - start) * 1000
        r.error = f"{type(exc).__name__}: {exc}"
    return r


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(int(round(p / 100 * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[idx]


def summarise(label: str, results: list[Result]) -> None:
    wall = [r.wall_ms for r in results if not r.error]
    server = [r.server_ms for r in results if r.server_ms is not None]

    print(f"\n{label}  ({len(wall)} ok, {len(results) - len(wall)} failed)")
    if not wall:
        print("  no successful requests")
        return

    print(f"  {'':<10} {'min':>9} {'median':>9} {'p95':>9} {'max':>9}")
    print(
        f"  {'wall':<10} {min(wall):>8.1f}ms {statistics.median(wall):>8.1f}ms "
        f"{pct(wall, 95):>8.1f}ms {max(wall):>8.1f}ms"
    )
    if server:
        print(
            f"  {'server':<10} {min(server):>8.1f}ms {statistics.median(server):>8.1f}ms "
            f"{pct(server, 95):>8.1f}ms {max(server):>8.1f}ms"
        )
        overhead = statistics.median(wall) - statistics.median(server)
        print(f"  {'transport':<10} {overhead:>8.1f}ms (median wall minus median server)")


def verdict(early: list[Result], steady: list[Result], workers: int) -> None:
    print("\n" + "=" * 62)

    early_ok = [r.server_ms for r in early if r.server_ms is not None]
    steady_ok = [r.server_ms for r in steady if r.server_ms is not None]

    if not steady_ok:
        print("VERDICT: not enough successful requests to judge.")
        return

    steady_median = statistics.median(steady_ok)
    early_max = max(early_ok) if early_ok else 0.0

    if steady_median > 1000:
        print("VERDICT: every request is slow.")
        print(f"  Steady-state median is {steady_median:.0f}ms, not a cold start.")
        print("  Something is doing real work per request, most likely a MySQL")
        print("  round trip that should be a cached in-process lookup. Check")
        print("  whether the gazetteer loaders are memoised.")
    elif early_max > 1000 and steady_median < 200:
        print("VERDICT: cold start only, now warm.")
        print(f"  Worst early request {early_max:.0f}ms, steady median {steady_median:.0f}ms.")
        print(f"  Consistent with lazy gazetteer loading across {workers} workers.")
        print("  Acceptable, but each worker restart pays it again, and")
        print("  --max-requests recycles workers periodically. Loading the")
        print("  gazetteers at startup would move the cost off the request path.")
    elif steady_median > 200:
        print("VERDICT: warm but slower than expected.")
        print(f"  Steady-state median {steady_median:.0f}ms. Gazetteer lookups")
        print("  should be well under this once in memory. Worth profiling")
        print("  before the batch endpoint multiplies it by 1000.")
    else:
        print("VERDICT: healthy.")
        print(f"  Steady-state median {steady_median:.0f}ms, no cold-start penalty")
        print("  visible. The 6-second response was a one-off.")

    slow = [r for r in steady if r.server_ms is not None and r.server_ms > 3 * steady_median]
    if slow:
        print(f"\n  Note: {len(slow)} steady-state request(s) over 3x the median.")
        print("  If that count is near the worker count, workers are still")
        print("  warming individually rather than all being warm.")
    print("=" * 62)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="Base URL, e.g. https://urlloom.com/locatron")
    ap.add_argument("-n", "--count", type=int, default=40, help="Total requests (default 40)")
    ap.add_argument("--workers", type=int, default=4, help="Gunicorn worker count (default 4)")
    ap.add_argument("--header", action="append", default=[], help='Extra header, "Name: value". Repeatable.')
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument(
        "--no-prefix",
        action="store_true",
        help="Hit /v1/resolve rather than the default, for going straight to the app",
    )
    args = ap.parse_args()

    headers: dict[str, str] = {}
    for h in args.header:
        if ":" not in h:
            print(f"bad header, expected 'Name: value': {h}", file=sys.stderr)
            return 2
        name, _, value = h.partition(":")
        headers[name.strip()] = value.strip()

    path = "/v1/resolve"

    print(f"Target   {args.url.rstrip('/')}{path}")
    print(f"Requests {args.count}, sequential")
    print(f"Assuming {args.workers} gunicorn workers")
    if headers:
        print(f"Headers  {', '.join(headers)}")

    results: list[Result] = []
    for i in range(args.count):
        q = QUERIES[i % len(QUERIES)]
        r = fire(args.url, path, q, headers, args.timeout)
        results.append(r)

        if i < 10:
            if i == 0:
                print(f"\n  {'#':>3} {'wall':>10} {'server':>10}  query")
            server_txt = f"{r.server_ms:.1f}ms" if r.server_ms is not None else "-"
            note = f"  [{r.error}]" if r.error else f"  ({r.granularity})"
            print(f"  {i + 1:>3} {r.wall_ms:>8.1f}ms {server_txt:>10}  {r.query}{note}")
        elif i == 10:
            print(f"\n  ... running {args.count - 10} more")

    # Early window covers every worker at least once, so a per-worker cold
    # start lands entirely inside it.
    split = min(args.workers * 2, max(4, args.count // 4))
    early, steady = results[:split], results[split:]

    summarise(f"First {len(early)} requests (cold window)", early)
    summarise(f"Remaining {len(steady)} requests (steady state)", steady)

    failed = [r for r in results if r.error]
    if failed:
        print(f"\nFailures ({len(failed)}):")
        seen: set[str] = set()
        for r in failed:
            if r.error not in seen:
                seen.add(r.error)
                print(f"  {r.error}  (first seen on {r.query!r})")

    verdict(early, steady, args.workers)
    return 1 if len(failed) == len(results) else 0


if __name__ == "__main__":
    sys.exit(main())