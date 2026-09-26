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
# parse
# ---------------------------------------------------------------------------

#: Signal order for the breakdown, so two runs never print the same numbers in a
#: different order. Any signal not listed is appended alphabetically, which keeps
#: a newly added weight visible instead of silently dropped.
_SIGNAL_ORDER = (
    "base",
    "ngram_length",
    "postcode_agree",
    "postcode_padded_agree",
    "postcode_disagree",
    "postcode_unexplained",
    "state_agree",
    "state_disagree",
    "state_hint_agree",
    "ambiguity_prior",
    "postal_only",
    "alias_trust",
    "street_match",
    "street_type_mismatch",
    "unexplained_tokens",
)


def _extract_components(ts, known_postcodes):
    """Run Prompt A's extractors and decide which token plays which role.

    This arbitration is not in the package yet: the pipeline that will own it is
    a later prompt, and until then it lives here and in the parse tests. Two
    rules, both load-bearing:

    A four-digit postcode token is not also offered as a street number, because
    it is a postcode far more often than a house number. A padded three-digit one
    is left available for both, because '810 Stuart Highway Winnellie' means a
    house number even though 0810 is a real postcode.
    """
    from locatron.parse.components import (
        find_po_boxes,
        find_postcodes,
        find_street_numbers,
        find_units_and_levels,
    )

    boxes = find_po_boxes(ts)
    units = find_units_and_levels(ts)
    postcodes = find_postcodes(ts, known_postcodes)

    claimed = [b.span for b in boxes] + [u.span for u in units]
    four_digit_starts = {c.span.start for c in postcodes if not c.padded}
    numbers = [
        n
        for n in find_street_numbers(ts)
        if n.span.start not in four_digit_starts and not any(n.span.overlaps(s) for s in claimed)
    ]
    claimed += [n.span for n in numbers]
    return boxes, units, postcodes, numbers, claimed


def _parse_one(text_in: str, au, known_postcodes) -> list[str]:
    """One input's report, as lines. Pure formatting over the parser stages."""
    from locatron.parse.locality import generate_hypotheses
    from locatron.parse.street import resolve_streets
    from locatron.parse.tokens import tokenize

    ts = tokenize(text_in)
    boxes, units, postcodes, numbers, claimed = _extract_components(ts, known_postcodes)

    out = [f"input      {text_in!r}", f"tokens     {list(ts.texts)}"]

    out.append("components")
    out.append(
        "  postcodes  "
        + (
            ", ".join(
                f"{c.postcode}{' (padded)' if c.padded else ''}@{c.span.start}" for c in postcodes
            )
            or "-"
        )
    )
    out.append(
        "  unit/level "
        + (
            ", ".join(
                f"{u.kind}={u.value}"
                + (f" kw={u.keyword}" if u.keyword else "")
                + (f" street#={u.street_number_hint}" if u.street_number_hint else "")
                for u in units
            )
            or "-"
        )
    )
    out.append(
        "  number     "
        + (
            ", ".join(
                n.number_first
                + (f"-{n.number_last}" if n.number_last else "")
                + (" (from /)" if n.from_slash else "")
                for n in numbers
            )
            or "-"
        )
    )
    out.append("  po box     " + (", ".join(f"{b.kind} BOX {b.number}" for b in boxes) or "-"))

    hyps = generate_hypotheses(
        ts, au, consumed=claimed, postcodes=postcodes, po_box_found=bool(boxes), fuzzy_min=88
    )
    joint = resolve_streets(ts, hyps, consumed=claimed)

    if not joint:
        out.append("hypotheses none")
        return out

    out.append("hypotheses (top 3)")
    for i, h in enumerate(joint[:3], 1):
        c = h.locality.candidate
        if h.street is not None:
            sub = h.street_type_substituted
            street = (
                f"{h.street.row.street_name!r} type={h.street.row.street_type or '-'} "
                f"score={h.street.match_score:.3f}"
            )
            if sub is not None:
                street += f" substituted={sub.written_as}({sub.input_type})->{sub.matched_type}"
        else:
            street = "-"
        left = [ts.text_of(s) for s in h.unexplained]
        out.append(
            f"  {i}. {c.state}/{c.locality}/{c.postcode}  joint={h.score:.3f}  "
            f"granularity={h.granularity}"
        )
        out.append(f"       street       {street}")
        out.append(f"       unexplained  {left or '-'}")

    top = joint[0]
    out.append("breakdown (top)")
    listed = [k for k in _SIGNAL_ORDER if k in top.signals]
    extra = sorted(k for k in top.signals if k not in _SIGNAL_ORDER)
    for k in listed + extra:
        out.append(f"  {k:<24}{top.signals[k]:>9.3f}")
    out.append(f"  {'= joint score':<24}{top.score:>9.3f}")
    return out


@app.command()
def parse(
    texts: list[str] = typer.Argument(  # noqa: B008 - typer reads the default
        ..., help="One or more strings to parse."
    ),
) -> None:
    """Run the phase 2 parser stages on each string, without the pipeline.

    Components, then locality hypotheses, then street matching, printed as one
    block per input. Read-only throughout: nothing here writes to MySQL, to the
    SQLite mirror, or to locatron_unresolved.

    The gazetteers and the street mirror are loaded once and shared across every
    input, so parsing fifty strings costs one warm-up rather than fifty.
    """
    from locatron.db import local
    from locatron.gazetteer.au import load_au

    try:
        local.check_mirror()
        local.connect()
    except local.MirrorError as exc:
        typer.secho(f"{exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    au = load_au()
    known_postcodes = frozenset(au.by_postcode)

    for i, text_in in enumerate(texts):
        if i:
            typer.echo("")
        for line in _parse_one(text_in, au, known_postcodes):
            typer.echo(line)


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

    target = path or str(get_settings().sqlite_file)
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
