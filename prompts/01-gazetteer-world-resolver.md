# Session 1: gazetteer loaders and the world resolver

Paste everything below the line into Claude Code. Review its plan before letting
it write files.

---

Read `CLAUDE.md` and `README.md` first. They contain the architecture decisions
and hard invariants for this project. Follow them.

## Goal

Implement use case 1 only: resolve a loose place string to a country, and to an
admin1 and city where possible. Examples of what must work:

- `Greater Melbourne` → Australia, Victoria, Melbourne
- `Sydney Australia` → Australia, New South Wales, Sydney
- `New York` → United States, New York
- `Las Vegas` → United States, Nevada
- `Delhi` → India (not Delhi, California)
- `Remote / Work from home` → unresolved, confidence 0, no exception

## Out of scope for this session

Do not build any of these. If you think one is needed, stop and ask.

- Australian street address parsing, or anything touching `address_ref` or
  `locatron_street`. That is session 2.
- FastAPI apps, HTTP endpoints, `locatron/api/`, `locatron/bulk/`.
- Redis caching. `locatron/cache.py` stays unwritten.
- Parquet exports or anything Databricks-related.

The CLI is the interface for this session. Keep the resolver a plain library so
the API can wrap it later without refactoring.

## Step 1: inspect the schema before writing code

Do not guess column names. Connect using the existing `locatron.db.mysql`
helpers and run `SHOW CREATE TABLE` for:

`Cities`, `Countries`, `country_bucket`, `aus_state_bucket`,
`locatron_locality`, `locatron_locality_alias`

Show me the output and your reading of it before you write any resolver code.
I want to confirm the column semantics match what you inferred, particularly
for `country_bucket` and `aus_state_bucket`.

Known quirks, already confirmed:

- `Cities.population` is a varchar. Cast it, and handle empty strings.
- Upstream tables are read-only. Never write to them.
- `locatron_locality` has ~18.5k rows, `locatron_locality_alias` is seeded,
  and `norm_key` / `alias_norm_key` are already populated at NORM_VERSION 1.
- `locatron_locality.address_count` is the natural tiebreak for ambiguous
  Australian localities, the same way population is for world cities.

## Step 2: files to create

```
locatron/gazetteer/loader.py     load-once, cache-in-process helpers
locatron/gazetteer/countries.py  country name/code/bucket lookup
locatron/gazetteer/cities.py     world city lookup with population tiebreak
locatron/gazetteer/au.py         AU locality + alias + state bucket lookup
locatron/resolve/scoring.py      confidence assembly
locatron/resolve/world.py        the place resolver
locatron/resolve/pipeline.py     entry point: resolve_one()
tests/test_gazetteer.py
tests/test_world_resolver.py
```

`resolve_one(text, *, country_bias=None, min_granularity=None,
include_candidates=False) -> ResolveResponse` is the signature `locatron/cli.py`
already calls. Match it.

Gazetteers are small enough to hold in process memory: 249 countries, 358
country buckets, ~48k cities, ~18.5k localities, ~65k state buckets. Load each
once, lazily, and cache. Do not put the street gazetteer in memory — it is not
part of this session anyway.

## Step 3: resolution behaviour

Rough order, but use your judgement and tell me if you think it should differ:

1. `normalize()` the input. Use `locatron.normalize` — do not write another
   normalisation function anywhere.
2. Split on commas into segments. Trailing segments are usually the broadest.
3. Look for an explicit country in any segment via `country_bucket` and
   `Countries`.
4. Look for an explicit AU state via `aus_state_bucket`.
5. Match the remaining text against AU localities, then world cities. Try the
   raw normalised form before `strip_qualifiers()` — some real place names
   legitimately contain words like Central or Greater.
6. Disambiguate: an explicit country or admin1 in the input wins outright. With
   no explicit signal, fall back to population (cities) or address_count (AU
   localities).
7. Apply `country_bias` from config only as a tiebreak between near-equal
   candidates, never to override something stated in the input.
8. Fuzzy match with rapidfuzz as a last resort, above the thresholds already in
   `config.py`.

Non-negotiable: unresolvable input returns a `ResolveResponse` with
`granularity=UNRESOLVED` and `confidence=0.0`. It never raises. Empty strings,
whitespace, and garbage all go down this path.

Set `match_method` accurately on every response — it is the main debugging
signal when a result looks wrong.

## Step 4: acceptance

`tests/golden/golden.csv` is the target. It has a `note` column explaining what
each row probes.

Add a `--filter` option to the `golden` CLI command so a subset can be run, then
make these pass:

- rows 1 through 8 (the world place cases)
- the three unresolvable rows
- `Victoria Australia`, `VIC`, `Australia`
- `London`, `Zurich`, `Sao Paulo`

The AU address rows will still fail. That is expected — they are session 2.

`Springfield` is deliberately ambiguous within one country. I want a low
confidence score and populated `candidates`, not a confident wrong answer.

## Constraints

- No new dependencies without asking. Everything you need is already in
  `pyproject.toml`.
- Scoring weights and thresholds go in `config.py`, not hardcoded, so they can
  be tuned on the box without a redeploy.
- Type hints on public functions. Docstrings explain why, not what.
- `uv run pytest` must pass, including the existing 32 normalisation tests. Do
  not modify `locatron/normalize.py` — a change there requires a NORM_VERSION
  bump and a full database rebuild.

## How I want you to work

Show me your plan and the schema inspection output before writing code. Then
work in this order, pausing after each so I can review:

1. Schema inspection and plan
2. Gazetteer loaders plus their tests
3. Scoring and the world resolver plus tests
4. Wire up `pipeline.resolve_one`, then run the golden set and report the
   pass rate

Commit after each step with a clear message. If something in `CLAUDE.md`
conflicts with what I have asked here, say so rather than picking one.

---

# Template for later sessions

Reuse this shape. What makes it work is the explicit out-of-scope list and the
instruction to inspect the schema rather than guess.

```
Read CLAUDE.md and README.md first.

## Goal
<one paragraph, with three or four concrete input/output examples>

## Out of scope
<explicit list — this is the part that keeps sessions focused>

## Step 1: inspect before writing
<what to look at; show me the output before writing code>

## Step 2: files to create
<exact paths and one-line responsibilities>

## Step 3: behaviour
<the algorithm, framed as "use your judgement and tell me if you disagree">

## Step 4: acceptance
<which golden rows must go green, and what "wrong" looks like>

## Constraints
<dependencies, config over hardcoding, what not to touch>

## How I want you to work
<numbered steps with review points; commit after each>
```

Planned sessions after this one:

2. AU address parser and G-NAF lookup (use case 2). The big one. Review the
   hypothesis-scoring logic closely — that is where subtle wrongness hides.
3. Redis caching, structured logging, and the `locatron_unresolved` table.
4. FastAPI app, `/healthz`, and first real deploy through `deploy.sh`.
5. Bulk API, Parquet snapshots, and the Databricks job.
