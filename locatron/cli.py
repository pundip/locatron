"""Terminal entry point.

Develop the resolver here, not through HTTP. The feedback loop is far shorter
and this doubles as the golden-set runner.

    locatron check
    locatron resolve "Greater Melbourne"
    locatron golden tests/golden/golden.csv
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import typer

from locatron.config import get_settings
from locatron.db import mysql
from locatron.normalize import NORM_VERSION, normalize, strip_qualifiers

app = typer.Typer(add_completion=False, help="Locatron CLI")


@app.command()
def check() -> None:
    """Verify config and database connectivity."""
    s = get_settings()
    typer.echo(f"MySQL   {s.mysql_user}@{s.mysql_host}:{s.mysql_port}/{s.mysql_database}")
    typer.echo(f"Redis   {s.redis_url}")
    typer.echo(f"Norm    v{NORM_VERSION}")
    typer.echo("")

    h = mysql.health()
    for k, v in h.items():
        typer.echo(f"  {k:<32} {v}")

    problems = []
    if not h.get("connected"):
        problems.append("cannot reach MySQL")
    if h.get("locality_norm_key_missing"):
        problems.append("locatron_locality has unpopulated norm_key, run normalize_pass.py")
    if h.get("alias_norm_key_missing"):
        problems.append("locatron_locality_alias has unpopulated alias_norm_key")

    typer.echo("")
    if problems:
        for p in problems:
            typer.secho(f"  FAIL  {p}", fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.secho("  OK", fg=typer.colors.GREEN)


@app.command()
def norm(text: str) -> None:
    """Show what normalisation does to a string."""
    n = normalize(text)
    typer.echo(f"raw        {text!r}")
    typer.echo(f"normalized {n!r}")
    typer.echo(f"stripped   {strip_qualifiers(n)!r}")


@app.command()
def resolve(
    text: str,
    country_bias: str = typer.Option(None, "--bias"),
    candidates: bool = typer.Option(False, "--candidates"),
) -> None:
    """Resolve a single string."""
    from locatron.resolve.pipeline import resolve_one  # not yet implemented

    result = resolve_one(text, country_bias=country_bias, include_candidates=candidates)
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))


@app.command()
def golden(path: Path = Path("tests/golden/golden.csv")) -> None:
    """Run the golden set and report accuracy per granularity."""
    from locatron.resolve.pipeline import resolve_one

    rows = list(csv.DictReader(path.open()))
    passed = failed = 0
    failures = []

    for row in rows:
        got = resolve_one(row["input"])
        ok = got.granularity.value == row["expected_granularity"]
        if ok and row.get("expected_country"):
            ok = (got.country.alpha3 if got.country else None) == row["expected_country"]
        if ok and row.get("expected_locality"):
            ok = (got.locality or "") == row["expected_locality"]

        if ok:
            passed += 1
        else:
            failed += 1
            failures.append((row["input"], row["expected_granularity"], got.granularity.value))

    for inp, want, gotv in failures[:30]:
        typer.echo(f"  FAIL  {inp!r}  want={want} got={gotv}")

    total = passed + failed
    pct = 100.0 * passed / total if total else 0.0
    typer.echo(f"\n{passed}/{total} passed ({pct:.1f}%)")
    if failed:
        raise typer.Exit(1)


if __name__ == "__main__":
    sys.exit(app())
