#!/usr/bin/env python3
"""Server-side latency per resolve path, in-process.

`latency_check.py` measures the whole chain -- Cloudflare, nginx, NAT, gunicorn --
which is the right tool for "why is this slow from my laptop" and the wrong one
for "which path costs what". This calls `resolve_one()` directly, so the numbers
are the resolver's own work with no network in front of it.

Three paths, because they do different amounts of work:

    world     the loose place path: in-memory gazetteers only
    AU loc    locality or postcode: in-memory gazetteers plus one SQLite query
    AU addr   street or better: the above plus one or two MySQL round trips

It also reports what routing costs the world path, which is the thing wiring the
parser in could plausibly have made worse: every world input now runs the
extractors and the street stage before being sent to the world resolver.

    uv run python scripts/resolve_bench.py
    uv run python scripts/resolve_bench.py -n 300

Caveat on the AU address numbers: MySQL is external, so they include a real
round trip whose time depends on where this runs. Compare like with like.
"""

from __future__ import annotations

import argparse
import statistics
import time

from locatron.resolve import world
from locatron.resolve.pipeline import resolve_one

#: Representative inputs per path. Real shapes from the golden set and the probe
#: cases, not synthetic ones, so the numbers describe traffic we expect.
CASES: dict[str, list[str]] = {
    "world": [
        "Greater Melbourne",
        "Sydney Australia",
        "Las Vegas",
        "Delhi",
        "Springfield",
        "London",
    ],
    "AU loc": [
        "Carrum Downs VIC",
        "Ryde NSW 2112",
        "St Kilda East VIC",
        "3201",
        "2000",
        "PO Box 45 World Square NSW 2002",
    ],
    "AU addr": [
        "65 Clifton Park Dr Carrum Downs VIC 3201",
        "65 clifton park drive 3201 carrum downs",
        "5/1 Smith Street Fitzroy VIC 3065",
        "17-23 Wills Street Melbourne VIC 3000",
        "Clifton Park Drive Carrum Downs",
        "Hamilton Crescent Ryde NSW 2112",
    ],
}


def pct(values: list[float], p: float) -> float:
    """Nearest-rank percentile. No interpolation, so a reported number is a
    measurement that actually happened."""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(p / 100.0 * len(ordered) + 0.5)) - 1))
    return ordered[k]


def time_calls(fn, inputs: list[str], n: int) -> list[float]:
    """Milliseconds per call, cycling through `inputs` so no single one dominates."""
    out: list[float] = []
    for i in range(n):
        text = inputs[i % len(inputs)]
        started = time.perf_counter()
        fn(text)
        out.append((time.perf_counter() - started) * 1000.0)
    return out


def summarise(label: str, samples: list[float]) -> str:
    return (
        f"  {label:<10} n={len(samples):<5} "
        f"p50 {pct(samples, 50):7.2f}  p95 {pct(samples, 95):7.2f}  "
        f"min {min(samples):7.2f}  max {max(samples):7.2f}  "
        f"mean {statistics.fmean(samples):7.2f}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=200, help="samples per path (default 200)")
    args = ap.parse_args()

    resolve = lambda text: resolve_one(text, record_unresolved=False)  # noqa: E731

    # Warm every gazetteer and open the SQLite handle before timing anything. A
    # cold first call is a startup cost the workers pay in post_worker_init, not
    # something a request ever sees.
    print("warming...")
    for inputs in CASES.values():
        for text in inputs:
            resolve(text)

    print(f"\nserver-side resolve latency, milliseconds ({args.n} samples per path)")
    for label, inputs in CASES.items():
        print(summarise(label, time_calls(resolve, inputs, args.n)))

    # What routing costs an input that ends up on the world path anyway.
    print("\nwhat routing costs the world path")
    world_inputs = CASES["world"]
    through_pipeline = time_calls(resolve, world_inputs, args.n)
    direct = time_calls(lambda text: world.resolve_place(text), world_inputs, args.n)
    print(summarise("pipeline", through_pipeline))
    print(summarise("world only", direct))
    added = pct(through_pipeline, 50) - pct(direct, 50)
    print(
        f"\n  routing adds {added:+.2f} ms at p50 "
        f"({pct(through_pipeline, 95) - pct(direct, 95):+.2f} ms at p95): the "
        f"extractors, the locality hypotheses and one SQLite street query, run "
        f"before the world resolver is reached."
    )
    print(
        "\nnote: MySQL is external, so the AU addr numbers include a real round "
        "trip.\n      Compare runs from the same machine."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
