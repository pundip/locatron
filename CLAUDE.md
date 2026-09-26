# Locatron

Location resolution API. Takes a loose string, returns the most complete
location it can — country at minimum, full G-NAF address at best.
Australian-focused.

## Use cases

1. **Loose place strings → at least a country.** Input like "Greater Melbourne",
   "Sydney Australia", "Las Vegas" arriving from LinkedIn scrapes and similar.
   Ambiguous city names are disambiguated by population (Delhi IN beats Delhi CA).
2. **Australian address → fully formatted G-NAF record.** Input like
   "65 clifton park drive 3201 carrum downs" returns every G-NAF field.
   Australian addresses only.
3. **Bulk export.** Feed resolved data and reference tables into Databricks and
   other downstream consumers. Separate API process from use cases 1 and 2.

## Hard invariants

These are not preferences. Breaking any of them causes bugs that are expensive
to find.

### Upstream tables are read-only

`address_ref`, `AustralianPostcodes`, `Cities`, `Countries`, `aus_state_bucket`,
and `country_bucket` are mirrors of external sources. Never INSERT, UPDATE,
DELETE, or ALTER them from application code. Adding indexes is fine as a manual
DBA operation; changing data is not.

All business logic lives in `locatron_*` derived tables and in Python.

If a derived value seems to need writing back upstream, that is a signal the
derived table is missing a column.

### One normalize() function

`locatron/normalize.py` defines `normalize()` and `NORM_VERSION`. It is the only
place normalisation happens.

- Never reimplement normalisation in SQL. SQL build scripts leave `norm_key`
  columns NULL; `scripts/normalize_pass.py` fills them by importing the same
  function the resolver calls.
- `NORM_VERSION` is baked into every Redis cache key and stored on every derived
  table row.
- Changing `normalize()` requires bumping `NORM_VERSION`, rerunning the
  normalize pass with `--all`, and flushing the Redis cache.

Build-time and query-time normalisation drifting apart produces silent misses
that look like bad data rather than a bug. This is the single most important
rule in the project.

### Resolution never raises for unresolvable input

Return HTTP 200 with `granularity: "unresolved"` and `confidence: 0`. Downstream
Databricks jobs handle a column far more gracefully than an exception.

## Data model

### Upstream (read-only)

| Table | Rows | Notes |
|---|---|---|
| `address_ref` | ~15.4M | G-NAF. All columns varchar, including LATITUDE/LONGITUDE/POSTCODE. Blank strings, not NULLs. |
| `AustralianPostcodes` | ~18.5k | Australia Post + ABS enrichment. NOT G-NAF-derived. Sole source of PO Box postcodes and SA1/SA2/SA3/SA4/PHN/LGA/electorate columns. |
| `Cities` | ~48k | World cities with population. `population` is varchar — cast it. |
| `Countries` | 249 | |
| `aus_state_bucket` | ~65.6k | String variants → AU state. Many are `"<STATE> <LOCALITY>"`. |
| `country_bucket` | 358 | String variants → country. |

### Derived (Locatron owns these)

| Table | Grain | Built by |
|---|---|---|
| `locatron_street` | state, locality, postcode, street_key | `sql/build_locatron_street.sql` |
| `locatron_locality` | state, locality, postcode | `sql/build_locatron_locality.sql` |
| `locatron_locality_alias` | alias_display, locality_id | same |

Rebuild order: street, then locality (locality reads street counts), then
`scripts/normalize_pass.py`, then `scripts/dedupe_locality.py`.

`dedupe_locality.py` must run last, and after the normalize pass rather than
before it: it groups on `norm_key`, which does not exist until that pass fills
it in. That ordering is what lets it collapse punctuation variants without
folding punctuation in SQL.

## Architecture

Single Proxmox LXC. Low container count is a deliberate constraint.

- `nginx` on :8000 — the only forwarded port. Rejects requests missing the
  shared-secret header set by the external edge nginx.
- `locatron-api` on :8080 — interactive resolve. Latency-sensitive.
- `locatron-bulk` on :8081 — exports and batch. Separate process so a large
  export cannot starve interactive traffic.
- `redis` on localhost — result cache only. Losing it must cost latency, never
  correctness.
- Local SQLite — street gazetteer mirror, shared across workers via the OS page
  cache.

MySQL is external at `pundip.com:3335`, database `ReferenceDB`.

Public URL is `urlloom.com/locatron`, so FastAPI apps use
`root_path="/locatron"`.

## Technical decisions

- **Synchronous SQLAlchemy with `def` endpoints**, not `async def`. FastAPI runs
  sync endpoints in a threadpool. This avoids async-MySQL driver complexity for
  no meaningful throughput loss at our scale.
- **Fuzzy matching happens at gazetteer level, exact matching at address level.**
  Localities (~20k) and cities (~48k) are small enough to fuzzy-match in memory
  with rapidfuzz. The 15M-row address table is only ever hit with exact,
  index-backed lookups. Never fuzzy-match across `address_ref`.
- **Street gazetteer lives in local SQLite, not Python memory.** Python objects
  are duplicated per gunicorn worker and refcounting defeats copy-on-write, so
  `--preload` does not help. SQLite lets workers share pages. Only keep small
  gazetteers (countries, buckets, localities, cities) in process memory.
- **The AU parser generates multiple hypotheses and validates each against the
  gazetteer.** It is not a positional regex. Input order varies — postcode can
  precede the locality. Ambiguities like "CLIFTON PARK DRIVE" (PARK is itself a
  valid street type) are only resolvable by checking candidate streets against
  the known streets in the matched locality.

## Known data traps

Learned the hard way. Do not rediscover these.

- `CAST('' AS DECIMAL)` returns **0, not NULL**. Averaging blank coordinates
  drags centroids toward (0,0). Always `CAST(NULLIF(TRIM(col),'') AS DECIMAL(...))`.
- Derived keys are not injective. `street_key` is built from three columns, but
  the primary key covers fewer. Multiple decompositions collapse to one key —
  `('HAMILTON','CR')` and `('HAMILTON CR','')` both give `HAMILTON CR`. Aggregate
  to variant level first, then collapse with an explicit tiebreak.
- G-NAF postcodes lose leading zeros on careless loads. NT is 0800–0899.
  `LPAD(TRIM(POSTCODE),4,'0')` everywhere.
- G-NAF has **no PO Boxes**. Postal addresses resolve via `locatron_locality`
  rows with `is_postal_only = 1`.
- `address_ref` uses blank strings, not NULLs. `NULLIF(TRIM(col),'')` before any
  NULL check.
- `STATE` includes `OT` for external territories — exclude it from Australian
  bounding-box sanity checks.
- Alias rows in G-NAF (`ALIAS_PRINCIPAL = 'ALIAS'`) point at their principal via
  `PRINCIPAL_PID`. Exclude them from canonical aggregates; use them to seed
  aliases.

## Repo layout

```
locatron/
├── CLAUDE.md
├── pyproject.toml
├── .env.example              # never commit .env
├── locatron/
│   ├── config.py
│   ├── normalize.py          # THE contract
│   ├── db/                   # mysql.py, local.py (sqlite)
│   ├── gazetteer/            # loaders for countries, cities, localities, streets
│   ├── parse/                # tokenizer.py, au_address.py, place.py
│   ├── resolve/              # pipeline.py, au.py, world.py, scoring.py
│   ├── cache.py
│   ├── schemas.py
│   ├── cli.py                # resolve from the terminal, no HTTP needed
│   ├── api/app.py            # :8080
│   └── bulk/app.py           # :8081
├── scripts/
│   ├── normalize_pass.py
│   └── dedupe_locality.py    # collapses punctuation-variant localities
├── sql/
├── tests/
│   └── golden/golden.csv     # input → expected output
└── deploy/                   # nginx.conf, systemd units
```

## Conventions

- Python 3.12, `uv` for dependency management.
- Pydantic v2 for all request/response schemas.
- Type hints on public functions.
- Scoring weights live in config, not hardcoded, so they can be tuned without a
  redeploy.
- Secrets come from environment variables. Never commit credentials, never pass
  a password as a command-line argument.
- Every unresolved or low-confidence input gets logged to `locatron_unresolved`
  with a count. Reviewing that table and promoting entries into
  `locatron_locality_alias` is what makes the service good over time.

## Testing

`tests/golden/golden.csv` holds real inputs with expected outputs. Run it in CI
and track precision and recall per granularity level. Without it there is no way
to know whether a scoring change helped or quietly broke fifty other cases.

Grow it from `locatron_unresolved`, not from imagination.
