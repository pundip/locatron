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
- `NORM_VERSION` is stored on every derived table row.
- Changing `normalize()` requires bumping `NORM_VERSION` and rerunning the
  normalize pass with `--all`.

Build-time and query-time normalisation drifting apart produces silent misses
that look like bad data rather than a bug. This is the single most important
rule in the project.

#### The one exception: street type spellings

`locatron/parse/street.py` holds `TYPE_SPELLINGS`, a hand-written table mapping
G-NAF street type codes to the spellings an input might use (`DR` → `DRIVE`,
`CR` → `CRESCENT`, `AV` → `AVENUE`/`AVE`).

It exists because G-NAF ships street types as codes and
`sql/build_locatron_street.sql` stores them unchanged, so the key for Clifton
Park Drive is `CLIFTON PARK DR` and the spelled form is absent from the data.
`normalize()` cannot bridge that, and fuzzy matching must not: Jaro-Winkler
scores `HAMILTON CRESCENT` at 92.94 against `HAMILTON CR`, `HAMILTON CT` and
`HAMILTON ST` alike. ReferenceDB has no street-type authority table to derive it
from, and the build SQL applies no mapping, so it is written by hand.

This is not a second normalisation path, and the distinction is what keeps it
safe:

- It is consulted only for the **trailing token** of a street candidate.
- It **generates alternative readings**, never rewrites a string. A destructive
  `DRIVE` → `DR` would corrupt `THE HORSLEY DRIVE`, whose *name* is
  `THE HORSLEY DRIVE` with a blank type — 46 keys end in ` DRIVE`, 319 in
  ` ROAD`, 127 in ` AVENUE`, and those are names.
- It is **never applied to a stored key or a norm_key**. No column in the
  database depends on it, so changing it needs no `NORM_VERSION` bump, no
  normalize pass and no cache flush.
- `normalize()` is untouched.

`tests/parse/test_street.py` asserts the table covers every distinct
`street_type` in `locatron_street`, so a new code from an upstream refresh fails
loudly rather than silently mismatching. Seven codes (`BA`, `BIDI`, `CLR`,
`CNTN`, `CNWY`, `CRF`, `VLLA`) are accepted as written only, because their
expansion is not derivable from anything in ReferenceDB.

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
`scripts/normalize_pass.py`, then `scripts/dedupe_locality.py`, then
`locatron build streets`.

`dedupe_locality.py` must run after the normalize pass rather than before it: it
groups on `norm_key`, which does not exist until that pass fills it in. That
ordering is what lets it collapse punctuation variants without folding
punctuation in SQL.

Both `locatron_locality` and `locatron_street` carry a precomputed centroid, with
100% coverage (18,551 and 532,182 rows). Street and locality fallbacks read those
and **never aggregate `address_ref` at request time** — Melbourne 3000 alone is
119,273 rows. The SQLite mirror carries the street centroid too.

`locatron build streets` is genuinely last. It mirrors `locatron_street` into the
local SQLite file and refuses to run if any `norm_key` is still NULL, which is how
it proves the normalize pass happened — `locatron_street` has no `norm_key` of its
own, so the evidence lives in `locatron_locality` and `locatron_locality_alias`.
The build reads only: it issues `SET SESSION TRANSACTION READ ONLY` before any
query, so it cannot write even when run as the service account, and it needs no
grant beyond `SELECT`.

## Architecture

Single Proxmox LXC. Low container count is a deliberate constraint.

- `nginx` on :8000 — the only forwarded port. Rejects requests missing the
  shared-secret header set by the external edge nginx.
- `locatron-api` on :8080 — interactive resolve. Latency-sensitive.
- `locatron-bulk` on :8081 — exports and batch. Separate process so a large
  export cannot starve interactive traffic.
- `redis` on localhost — intended as a result cache only, and **not built yet**.
  There is no `cache.py`, nothing imports redis, and nothing has ever been
  written to it: the settings in `config.py`, the `LOCATRON_REDIS_URL` in
  `.env.example` and `MatchMethod.CACHE` are all placeholders for it. So there
  are no cached answers to invalidate, and nothing to flush when `NORM_VERSION`
  changes.

  When a cache is added, two rules come with it. Losing it must cost latency,
  never correctness. And its key must include **both** `NORM_VERSION` and a
  `PARSER_VERSION`, so that changing normalisation *or* changing parsing,
  routing or scoring invalidates stale answers on deploy rather than needing a
  manual flush somebody will forget. An input's answer depends on both, and only
  one of them is currently versioned at all.
- Local SQLite at `config.sqlite_path` — the street gazetteer mirror, 532k rows
  and about 55 MB, shared across workers via the OS page cache. Built by
  `locatron build streets`; opened read-only, once per worker, in
  `post_worker_init` and never before the fork. A missing file or a
  `norm_version` that disagrees with the code fails worker startup loudly: there
  is no fallback to MySQL, because that is the silent-miss failure the mirror
  exists to prevent.

MySQL is external at `pundip.com:3335`, database `ReferenceDB`.

Public URL is `urlloom.com/locatron`, so FastAPI apps use
`root_path="/locatron"`.

## Routing: which path resolves an input

`resolve_one()` picks between the AU address path and the world place path. The
world path is the default and the fallback; the AU path has to be earned.

An input takes the AU path when **at least one trigger** fires and **the gate**
holds.

Triggers — each is something a loose world place string does not carry:

| Trigger | Fires on |
|---|---|
| `postcode` | a postcode candidate that validates against `locatron_locality` |
| `state` | a **strong** state token, i.e. `aus_state_bucket`'s `state_tokens`, never a `state_hint` |
| `pobox` | a PO box was found |
| `street+type` | a street match whose `reading` is `name+type`, so the input supplied a street-type word |
| `number+street` | a street number and any street match |

The two street triggers are narrow on purpose. A bare street match is **not** a
trigger: `New York` matches street `NEW ST` in locality `YORK` at score 2.555
with nothing unexplained, and routing it to the AU path would answer a New York
query with a street in York, WA. What distinguishes a real Australian street is
an explicit type word in the input (`Clifton Park **Drive** Carrum Downs`) or a
number in front of it.

Gate — all must hold:

- a locality hypothesis exists at all. `VIC` alone fires the `state` trigger and
  produces no hypothesis, so it stays on the world path and keeps `admin1`.
- the winning joint score is not negative. A floor, not a tuning knob: a
  negative score means the parse explains less than it fails to.
- if the winning locality matched **fuzzy**, nothing is left unexplained.
  `Victoria Australia` fires `state`, then fuzzy-matches locality `TORRITA` and
  leaves `AUSTRALIA` unexplained — a state name being read as a suburb. It keeps
  `admin1`. `Ku-ring-gai NSW` is also fuzzy, explains every token, and takes the
  AU path. This replaces a score threshold, which would have needed a magic
  number between 0.893 and 1.627 and nothing to justify it.

`scripts/route_probe.py` prints the route and landing granularity for every
golden row and probe case. Run it after touching the triggers, the gate or the
mapping.

### Granularity mapping

The response vocabulary (`schemas.Granularity`) is fixed: `unit`, `address`,
`street`, `postcode`, `locality`, `admin1`, `city`, `country`, `unresolved`. The
parser ladder has its own five rungs, and they are not the same list.

| Ladder rung | Response granularity | Why |
|---|---|---|
| `unit` | `unit` | |
| `address` | `address` | |
| `street` | `street` | |
| `locality` | `locality` **or** `postcode` | `postcode` when the winning hypothesis's `locality_span` is empty |
| `postal` | `postcode` | G-NAF holds no PO boxes, so the postcode is the finest truth available. There is no `postal` member and adding one would break consumers. |

The `locality`/`postcode` split is exact, not a heuristic. A candidate that came
from `candidates_for_postcode()` carries `Span(0, 0)`, because the input never
named the locality — only the postcode, which can span several localities. So
`3201` answers `postcode` with `VIC`, and the localities it could mean go in
`candidates`. `Carrum Downs VIC` names its locality and answers `locality`.

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
- G-NAF postcodes lose leading zeros on careless loads. NT is 0800–0899. Pad
  when **building** a derived table: `LPAD(TRIM(POSTCODE),4,'0')`.
  Never pad in a **WHERE** clause against `address_ref`. Its `POSTCODE` is
  already four characters on all 15,949,543 rows, so padding buys nothing and
  wrapping the indexed column kills the index:
  `WHERE POSTCODE = '0800'` is `type=ref`, 1 row, 1 ms;
  `WHERE LPAD(POSTCODE,4,'0') = '0800'` is `type=ALL`, 15,886,279 rows, 10.6 s.
- `address_ref` values carry **no** leading or trailing whitespace. Zero rows have
  `col <> TRIM(col)` for `STREET_NAME`, `LOCALITY_NAME`, `STREET_TYPE`, `STATE` or
  `NUMBER_FIRST`, so a `TRIM()` in a WHERE clause is the same self-inflicted
  full scan as `LPAD`. Trim on the way out if you like, never on the way in.
- `LATITUDE` and `LONGITUDE` are populated on every `address_ref` row — zero
  blanks. The `CAST('' AS DECIMAL)` trap above still applies to other tables, and
  guarding costs nothing, but it does not bite here.
- G-NAF has **no PO Boxes**. Postal addresses resolve via `locatron_locality`
  rows with `is_postal_only = 1`.
- `address_ref` uses blank strings, not NULLs. `NULLIF(TRIM(col),'')` before any
  NULL check.
- `STATE` includes `OT` for external territories — exclude it from Australian
  bounding-box sanity checks.
- Alias rows in G-NAF point at their principal via `PRINCIPAL_PID`. Exclude them
  from canonical aggregates; use them to seed aliases. **The indicator values are
  single letters, not words**: `ALIAS_PRINCIPAL` is `'P'` (15,108,510 rows) or
  `'A'` (841,033). Querying `= 'PRINCIPAL'` matches nothing and returns silently.
- `PRIMARY_SECONDARY` is `''`, `'S'` or `'P'`, and blank is the common case —
  10,414,829 rows, against 4,966,618 `'S'` and 568,096 `'P'`. A blank means the
  address is neither part of a group nor a group's head, so treat it as ordinary
  rather than as missing data. A unit is an `'S'` row carrying `FLAT_NUMBER`.

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
│   └── build/                # streets.py, the SQLite mirror build
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
