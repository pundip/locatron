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
