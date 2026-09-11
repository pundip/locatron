"""Terminal entry point.

Develop the resolver here, not through HTTP. The feedback loop is far shorter
and this doubles as the golden-set runner.

    locatron check
    locatron schema                        # all known tables
    locatron schema Cities country_bucket  # specific ones
    locatron sample Cities --limit 5
    locatron norm "Greater Melbourne"
    locatron resolve "Greater Melbourne"
    locatron golden
    locatron golden --filter "population tiebreak"

`schema` and `sample` exist so that agents and humans inspect the real database
rather than guessing column names. They are read-only by construction.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import typer
from sqlalchemy import text

from locatron.config import get_settings
from locatron.db import mysql
from locatron.normalize import NORM_VERSION, normalize, strip_qualifiers
from locatron.schemas import ResolveResponse

app = typer.Typer(add_completion=False, help="Locatron CLI")

ALL_TABLES = sorted(mysql.READ_ONLY_TABLES | mysql.DERIVED_TABLES)


def _validate_table(name: str) -> str:
    """Allow only known table names.

    These names are interpolated into SQL because MySQL does not accept a
    bound parameter in that position. Restricting to a fixed allowlist is what
    makes that safe.
    """
    known = {t.lower(): t for t in ALL_TABLES}
    resolved = known.get(name.lower())
    if resolved is None:
        raise typer.BadParameter(f"unknown table {name!r}. Known: {', '.join(ALL_TABLES)}")
    return resolved


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
def schema(
    tables: list[str] = typer.Argument(  # noqa: B008 - typer reads the default
        None, help="Table names. Omit for all known tables."
    ),
    ddl: bool = typer.Option(False, "--ddl", help="Full SHOW CREATE TABLE instead of a summary."),
) -> None:
    """Show the structure of the reference and derived tables.

    Defaults to a column summary, which is usually what you want when working
    out how to query something. Use --ddl for indexes, collations, and engine.
    """
    targets = [_validate_table(t) for t in tables] if tables else ALL_TABLES

    with mysql.session_scope() as s:
        for t in targets:
            typer.secho(f"\n{'=' * 76}\n{t}\n{'=' * 76}", fg=typer.colors.BLUE)

            try:
                rows = s.execute(text(f"SELECT COUNT(*) FROM `{t}`")).scalar()  # noqa: S608
                readonly = " (read-only upstream)" if t in mysql.READ_ONLY_TABLES else ""
                typer.echo(f"{rows:,} rows{readonly}\n")
            except Exception as exc:
                typer.secho(f"  cannot read: {exc}", fg=typer.colors.RED)
                continue

            if ddl:
                create = s.execute(text(f"SHOW CREATE TABLE `{t}`")).first()
                typer.echo(create[1] if create else "(none)")
                continue

            cols = s.execute(text(f"SHOW FULL COLUMNS FROM `{t}`")).mappings().all()
            width = max((len(c["Field"]) for c in cols), default=10)
            for c in cols:
                flags = []
                if c["Key"]:
                    flags.append(c["Key"])
                if c["Null"] == "NO":
                    flags.append("NOT NULL")
                if c["Extra"]:
                    flags.append(c["Extra"])
                suffix = f"  [{' '.join(flags)}]" if flags else ""
                typer.echo(f"  {c['Field']:<{width}}  {c['Type']}{suffix}")

            idx = s.execute(text(f"SHOW INDEX FROM `{t}`")).mappings().all()
            if idx:
                by_name: dict[str, list[str]] = {}
                for i in idx:
                    by_name.setdefault(i["Key_name"], []).append(i["Column_name"])
                typer.echo("")
                for name, cols_in in by_name.items():
                    typer.echo(f"  INDEX {name}({', '.join(cols_in)})")


@app.command()
def sample(
    table: str,
    limit: int = typer.Option(5, "--limit", "-n", min=1, max=50),
    where: str = typer.Option(None, "--where", help="Filter, without the WHERE keyword."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show a few rows so you can see what the values actually look like.

    Column types tell you a lot less than real values do. Cities.population
    being a varchar is only half the story; seeing that some rows are empty
    strings is the other half.
    """
    t = _validate_table(table)
    clause = f"WHERE {where} " if where else ""

    with mysql.session_scope() as s:
        rows = (
            s.execute(
                text(f"SELECT * FROM `{t}` {clause}LIMIT :n"),  # noqa: S608
                {"n": limit},
            )
            .mappings()
            .all()
        )

    if not rows:
        typer.echo("(no rows)")
        return

    if as_json:
        typer.echo(json.dumps([dict(r) for r in rows], indent=2, default=str))
        return

    width = max(len(k) for k in rows[0])
    for i, row in enumerate(rows, 1):
        typer.secho(f"\n--- row {i} ---", fg=typer.colors.BLUE)
        for k, v in row.items():
            shown = "''" if v == "" else ("NULL" if v is None else str(v))
            typer.echo(f"  {k:<{width}}  {shown}")


@app.command()
def norm(text_in: str) -> None:
    """Show what normalisation does to a string."""
    n = normalize(text_in)
    typer.echo(f"raw        {text_in!r}")
    typer.echo(f"normalized {n!r}")
    typer.echo(f"stripped   {strip_qualifiers(n)!r}")


@app.command()
def resolve(
    text_in: str,
    country_bias: str = typer.Option(None, "--bias"),
    candidates: bool = typer.Option(False, "--candidates"),
) -> None:
    """Resolve a single string."""
    from locatron.resolve.pipeline import resolve_one

    result = resolve_one(text_in, country_bias=country_bias, include_candidates=candidates)
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2, default=str))


def _golden_mismatch(row: dict[str, str], got: ResolveResponse) -> str | None:
    """Return a readable reason the row failed, or None if it passed.

    admin1 is checked against either the code or the name, because the golden
    set uses whichever form is idiomatic for the country: VIC for Australia,
    Nevada for the United States. A blank expected column is not checked.
    """
    checks: list[tuple[str, str, str | None]] = [
        ("granularity", row["expected_granularity"], got.granularity.value),
        ("country", row.get("expected_country") or "", got.country.alpha3 if got.country else None),
        ("locality", row.get("expected_locality") or "", got.locality or None),
    ]
    for field, want, have in checks:
        if want.strip() and have != want.strip():
            return f"{field}: want={want.strip()} got={have}"

    want_admin1 = (row.get("expected_admin1") or "").strip()
    if want_admin1:
        a = got.admin1
        if not a or want_admin1 not in {a.code, a.name}:
            shown = f"{a.code}/{a.name}" if a else None
            return f"admin1: want={want_admin1} got={shown}"
    return None


@app.command()
def golden(
    path: Path = Path("tests/golden/golden.csv"),
    filter_note: str = typer.Option(
        None, "--filter", help="Only rows whose note or input contains this."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show every failure, not just 30."),
) -> None:
    """Run the golden set and report accuracy."""
    from locatron.resolve.pipeline import resolve_one

    rows = list(csv.DictReader(path.open()))
    if filter_note:
        needle = filter_note.lower()
        rows = [
            r
            for r in rows
            if needle in (r.get("note") or "").lower() or needle in r["input"].lower()
        ]
        if not rows:
            typer.secho(f"no golden rows match --filter {filter_note!r}", fg=typer.colors.YELLOW)
            raise typer.Exit(1)

    passed = 0
    failures: list[tuple[str, str]] = []

    for row in rows:
        got = resolve_one(row["input"])
        why = _golden_mismatch(row, got)
        if why is None:
            passed += 1
        else:
            failures.append((row["input"], why))

    for inp, why in failures if verbose else failures[:30]:
        typer.echo(f"  FAIL  {inp!r}  {why}")

    total = passed + len(failures)
    pct = 100.0 * passed / total if total else 0.0
    typer.echo(f"\n{passed}/{total} passed ({pct:.1f}%)")
    if failures:
        raise typer.Exit(1)


if __name__ == "__main__":
    sys.exit(app())
