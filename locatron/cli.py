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

from locatron.config import env_files, get_settings
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


def _check_street_mirror(*, deep: bool) -> list[str]:
    """Report on the SQLite street mirror. Returns problems, if any."""
    from locatron.db import local

    try:
        meta = local.read_meta()
    except local.MirrorError as exc:
        typer.echo(f"  {'street mirror':<32} MISSING")
        return [str(exc)]

    typer.echo(f"  {'street mirror':<32} {meta.path}")
    typer.echo(f"  {'  rows':<32} {meta.row_count}")
    typer.echo(f"  {'  norm_version':<32} {meta.norm_version}")
    typer.echo(f"  {'  snapshot_id':<32} {meta.snapshot_id}")
    typer.echo(f"  {'  built_at':<32} {meta.built_at}")

    problems: list[str] = []
    if not meta.is_current:
        problems.append(
            f"street mirror built with NORM_VERSION {meta.norm_version!r}, "
            f"code is {NORM_VERSION!r} - run `locatron build streets`"
        )

    # Row count is cheap and catches a truncated source without a full scan.
    try:
        with mysql.session_scope() as sess:
            source_rows = sess.execute(text("SELECT COUNT(*) FROM locatron_street")).scalar()
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        typer.echo(f"  {'  source rows':<32} unavailable ({type(exc).__name__})")
        return problems

    typer.echo(f"  {'  source rows':<32} {source_rows}")
    if source_rows != meta.row_count:
        problems.append(
            f"street mirror has {meta.row_count} rows, locatron_street has "
            f"{source_rows} - run `locatron build streets`"
        )

    if deep:
        from locatron.build import streets as build_streets_mod

        with mysql.session_scope() as sess:
            digest = build_streets_mod.source_digest(sess)
        typer.echo(f"  {'  mirror digest':<32} {meta.source_digest}")
        typer.echo(f"  {'  source digest':<32} {digest.digest}")
        if digest.digest != meta.source_digest:
            problems.append(
                "street mirror digest does not match locatron_street - "
                "the source changed, run `locatron build streets`"
            )

    return problems


@app.command()
def check(
    deep: bool = typer.Option(
        False, "--deep", help="Also digest locatron_street to detect source drift (~1.4s)."
    ),
) -> None:
    """Verify config, database connectivity and the street mirror.

    The mirror check defaults to norm_version and row count, which are local and
    instant. --deep adds the full content digest, a scan of all 532k rows, which
    is the only thing that detects locatron_street changing underneath a mirror
    whose NORM_VERSION still matches.
    """
    s = get_settings()
    typer.echo(f"MySQL   {s.mysql_user}@{s.mysql_host}:{s.mysql_port}/{s.mysql_database}")
    files = env_files()
    if files:
        # Listed lowest precedence first; later files override earlier ones.
        for i, f in enumerate(files):
            typer.echo(f"{'Env' if i == 0 else '':<8}{f}")
    else:
        typer.secho(
            "Env     NO .env FILE FOUND - using environment variables and defaults only",
            fg=typer.colors.YELLOW,
        )
    typer.echo(f"Redis   {s.redis_url}")
    typer.echo(f"Norm    v{NORM_VERSION}")
    typer.echo("")

    h = mysql.health()
    for k, v in h.items():
        typer.echo(f"  {k:<32} {v}")

    problems = []
    if not h.get("connected"):
        problems.append("cannot reach MySQL")

    typer.echo("")
    problems += _check_street_mirror(deep=deep)

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


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

build_app = typer.Typer(add_completion=False, help="Build the derived stores Locatron owns.")
app.add_typer(build_app, name="build")


@build_app.command("streets")
def build_streets(
    path: str = typer.Option(None, help="Override the configured sqlite_path."),
    quiet: bool = typer.Option(False, "--quiet", help="Only print the result."),
) -> None:
    """Build the local SQLite street mirror from locatron_street.

    Reads only: the session is set TRANSACTION READ ONLY before any query, so
    SELECT is the only grant required. Writes into a temporary file beside the
    target and moves it into place, so a reader never sees a half-built mirror.
    """
    from locatron.build import streets as build_streets_mod

    target = path or get_settings().sqlite_path
    typer.echo(f"Source  locatron_street @ {get_settings().mysql_host}")
    typer.echo(f"Target  {target}")

    def progress(done: int, total: int) -> None:
        if not quiet:
            typer.echo(f"  {done}/{total} rows")

    try:
        meta, seconds = build_streets_mod.build_with_timing(target, progress=progress)
    except build_streets_mod.BuildError as exc:
        typer.secho(f"BUILD REFUSED: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    size_mb = Path(target).stat().st_size / 1e6
    typer.echo(f"  rows           {meta['row_count']}")
    typer.echo(f"  norm_version   {meta['norm_version']}")
    typer.echo(f"  snapshot_id    {meta['snapshot_id']}")
    typer.echo(f"  source_digest  {meta['source_digest']}")
    typer.echo(f"  built_at       {meta['built_at']}")
    typer.echo(f"  file           {size_mb:.1f} MB")
    typer.secho(f"Built in {seconds:.1f}s", fg=typer.colors.GREEN)


if __name__ == "__main__":
    sys.exit(app())
