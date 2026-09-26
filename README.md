# Locatron

Location resolution API. See `CLAUDE.md` for architecture decisions and
invariants, which is the file to read first.

## Setup

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
cp .env.example .env      # then fill in LOCATRON_MYSQL_PASSWORD
```

## Verify

```bash
uv run locatron check
```

Expect `locality_norm_key_missing` and `alias_norm_key_missing` to be 0. If
they are not, run:

```bash
uv run python scripts/normalize_pass.py
```

## Develop

```bash
uv run pytest
uv run locatron norm "Greater Melbourne"
uv run locatron resolve "Greater Melbourne"     # once the resolver exists
uv run locatron golden                          # accuracy against the golden set
```

## Build order for derived tables

1. `sql/build_locatron_street.sql`
2. `sql/build_locatron_locality.sql`
3. `scripts/normalize_pass.py`
4. `scripts/dedupe_locality.py`

Steps 3 and 4 need the `locatron_build` credentials; the service user has no
write grant on the gazetteer tables. Step 4 groups on the `norm_key` that step
3 populates, so the order between them is not interchangeable. Step 4 reports by
default and writes only with `--apply`:

```bash
uv run python scripts/dedupe_locality.py           # report, writes nothing
uv run python scripts/dedupe_locality.py --apply   # make the changes
```
