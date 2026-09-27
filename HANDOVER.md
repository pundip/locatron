Locatron handover
State as of 27 September 2026. Written to start a fresh conversation without
replaying the build history.
---
What Locatron is
An Australian-focused location resolution API. Takes a loose string, returns the
most complete location it can — country at minimum, full G-NAF address at best.
Three use cases:
Loose place strings to at least a country. LinkedIn-style input like
"Greater Melbourne", "Sydney Australia", "Las Vegas". Ambiguous city names
resolve by population, so "Delhi" is India, not California. Built and
deployed.
Australian address to a fully formatted G-NAF record. "65 clifton park
drive 3201 carrum downs" returns every G-NAF field. Australian addresses
only. Built and deployed.
Bulk export feeding Databricks and other consumers, as a separate API
process. Not started.
`CLAUDE.md` at the repo root holds the architecture decisions and invariants.
Claude Code reads it automatically. Read it before anything else.
---
Current state
Live at `https://urlloom.com/locatron/`. Working endpoints:
```
GET  /locatron/healthz
GET  /locatron/v1/resolve?text=Greater+Melbourne
POST /locatron/v1/resolve
POST /locatron/v1/resolve/batch
GET  /locatron/docs
```
Both resolve paths are live. `tests/golden/golden.csv` is 35 rows and passes
35/35; the suite is 1030 tests.

Performance, server-side and in-process, from `scripts/resolve_bench.py`:

| Path | p50 | p95 |
|---|---|---|
| world place | 1.0 ms | 5.7 ms |
| AU locality / postcode | 2.8 ms | 12.5 ms |
| AU address | 15.8 ms | 33.3 ms |

The AU address figure includes a real MySQL round trip, and MySQL is external, so
it moves with where you measure from. Routing costs a world answer about 0.6 ms at
p50. All four gunicorn workers load gazetteers at startup via a
`post_worker_init` hook, so there is no cold-start penalty on the request path.
About 106ms of the user-visible latency is network (Melbourne to Cloudflare to
Sydney to home NAT), which is mostly unavoidable and is the argument for the batch
endpoint.
Repo layout
```
locatron/
├── CLAUDE.md                     invariants, read first
├── README.md
├── pyproject.toml
├── .env.example                  container .env lives at /opt/locatron/.env
├── locatron/
│   ├── normalize.py              THE contract, NORM_VERSION = "1"
│   ├── config.py
│   ├── schemas.py                ResolveResponse envelope
│   ├── cli.py                    check, schema, sample, norm, resolve, golden
│   ├── db/mysql.py
│   ├── gazetteer/                loader, countries, cities, au
│   ├── resolve/                  pipeline, au, world, scoring, unresolved
│   ├── parse/                    tokens, components, locality, street, lookup, scoring
│   ├── build/                    streets.py, the SQLite mirror build
│   ├── db/local.py               the street mirror
│   ├── api/app.py
│   └── bulk/                     EMPTY — phase 3
├── scripts/
│   ├── normalize_pass.py
│   ├── dedupe_locality.py
│   ├── route_probe.py            which path each golden row takes, and why
│   ├── resolve_bench.py          server-side latency per path
│   ├── latency_check.py
│   └── git-hooks/pre-commit      blocks committing secrets
├── sql/                          build scripts for derived tables
├── deploy/                       install.sh, deploy.sh, nginx.conf, systemd
├── prompts/01-gazetteer-world-resolver.md
└── tests/golden/golden.csv       35 rows, the accuracy target
```
Database
MySQL 8 at `pundip.com:3335`, database `ReferenceDB`.
Upstream tables, read-only, never write to them:
Table	Rows	Notes
`address_ref`	15.9M	G-NAF. All columns varchar including LATITUDE/LONGITUDE/POSTCODE. Blank strings, not NULLs.
`AustralianPostcodes`	18.5k	Australia Post plus ABS. NOT G-NAF-derived. Sole source of PO Box postcodes and the SA1/SA2/SA3/SA4 columns.
`Cities`	48k	World cities. `population` is varchar, cast it.
`Countries`	249	
`aus_state_bucket`	65.6k	String variants to AU state. Many are `"<STATE> <LOCALITY>"`.
`country_bucket`	358	String variants to country.
Derived tables Locatron owns:
Table	Rows	Grain
`locatron_street`	532,182	state, locality, postcode, street_key
`locatron_locality`	18,567	state, locality, postcode
`locatron_locality_alias`	61,155	alias_display, locality_id
`locatron_unresolved`	0	feedback loop, empty so far
`locatron_api_key`	0	not wired up yet
Rebuild order: street, then locality (it reads street counts), then
`scripts/normalize_pass.py`. All `norm_key` columns are populated at
NORM_VERSION 1.
Three MySQL accounts, enforcing the read-only invariant at the grant level:
`locatron_ro` (SELECT only, use this for inspection), `locatron` (the service),
`locatron_build` (gazetteer rebuilds, run by hand). Scripts that need the build
account get it by overriding `LOCATRON_MYSQL_USER` and `LOCATRON_MYSQL_PASSWORD`
in the environment; there are no separate build settings.

The service account's grants, verbatim from `SHOW GRANTS`, because the details
matter and the earlier summary here was wrong:

```
GRANT SELECT ON `ReferenceDB`.* TO `locatron`@`%`
GRANT INSERT, UPDATE ON `ReferenceDB`.`locatron_unresolved` TO `locatron`@`%`
GRANT UPDATE ON `ReferenceDB`.`locatron_api_key` TO `locatron`@`%`
```

So on `locatron_unresolved` it has **INSERT and UPDATE but not DELETE**: the
resolver can file a row and bump its `hit_count`, and nothing in the application
can remove one. Clearing a row needs `locatron_build` or a DBA. That is the right
shape for a feedback table, and it is also why no test may write to it — see
`tests/conftest.py`, which blocks that for every test rather than relying on each
call site to opt out. On `locatron_api_key` it has UPDATE only, not INSERT, so
creating a key is a manual operation too.
---
Phase 2: the AU address parser — built
`CLAUDE.md` carries the design in full: the Routing section says how an input is
sent down one path or the other, the Granularity mapping section says how the
parser's five rungs become the nine response values, and the response envelope
section says what comes back. What follows is only what a newcomer needs to find
their way around.
The shape of it
Not a positional regex. The parser generates multiple readings and validates each
against the gazetteer, keeping the best. `65 clifton park drive 3201 carrum downs`
puts the postcode before the locality, which is why.
Stages, each its own module under `locatron/parse/`: `tokens` splits, `components`
pulls out postcodes, units, street numbers and PO boxes, `locality` proposes
localities from token n-grams, `street` matches streets from the SQLite mirror,
and `lookup` does the one exact dive into `address_ref`. `locatron/resolve/au.py`
runs them and shapes an answer; `resolve/pipeline.py` chooses between that and the
world path.
Two commands make it inspectable without deploying anything:
```bash
uv run locatron parse "65 clifton park drive 3201 carrum downs"   # every stage
uv run python scripts/route_probe.py                              # route per golden row
uv run python scripts/route_probe.py --confidence                 # the calibration table
```
Where to be careful
The hypothesis scoring is where subtle wrongness hides, and every weight in
`parse/scoring.py` carries a comment saying what real case fixed its value. Change
one and run `route_probe.py` before and after: it prints the route and landing
granularity for all 35 golden rows and 19 probe cases, and a rule that looks
reasonable in isolation regularly moves something else.
---
What the build taught us
Four things cost real time. They are here because each of them looked like
something else at first.
A deploy that fetches is not a deploy that happened. `deploy.sh` compared the
checkout against origin to decide there was nothing to do. A deploy that fetched
and then failed before the restart left the checkout matching origin with the
services on the previous commit, so every later run reported nothing to do and
changed nothing — production ran a day stale, and nothing in the repo could tell
you, because the checkout looked perfect. Now `/opt/locatron/.deployed-sha`
records what actually restarted, and `--status` answers the question without
deploying.
Then the fix for that got deployed by a run that half-applied it: bash reads a
script incrementally, so the fetch that brought in the recording code was followed
by the tail bash had already buffered, which had none of it. deploy.sh now
re-execs itself when a fetch changes it.
`SET SESSION TRANSACTION READ ONLY` outlives the query. The G-NAF lookups issued
it on a connection borrowed from the shared pool, so it stayed set when the
connection went back, and the next `locatron_unresolved` write on that connection
failed. The write is best effort, so it was logged and swallowed and the feedback
table simply stayed empty — intermittently, depending on pool checkout order.
There are two pools now. The general lesson: a session-scoped setting on a pooled
connection is a side effect on every later borrower.
Golden rows have to come from the data. Three of the AU addresses in `golden.csv`
were written from imagination and did not exist in G-NAF — `12 Smith St Fitzroy`
is not a thing; that street runs 1, 3, 5, 7, 7A, 9, 11, 11A, 13, 15 through that
stretch. The parser was degrading to `street` entirely correctly and it read as a
parser bug. They were replaced with rows verified against `address_ref`, and each
note now says why. `CLAUDE.md` already said to grow the set from
`locatron_unresolved` rather than from imagination; this is what it costs when you
do not.
A test that writes to a real table will. Before the guard in `tests/conftest.py`,
suite runs filed nine synthetic rows into `locatron_unresolved` — and the service
account has no DELETE on it, so they needed a DBA to remove. Per-call-site opt-out
was not enough: 35 `resolve_one()` calls did not pass the flag and the API tests
could not. The guard is autouse now.
---
---
Machines and conventions
Three machines. State which one a command runs on, because the same command
means different things on each.
Windows (`C:\dev\locatron`) — all git operations, all editing. Branch is
`master`, not `main`.
Container (`root@locatron`, LXC on Proxmox) — app, container nginx,
systemd. Pulls only; nothing is committed from here.
Edge server (`maneesh@ubuntu-s-1vcpu-1gb-syd1-01`) — edge nginx only.
Deploy:
```bash
# On the container, as root
bash /opt/locatron/app/deploy/deploy.sh --branch master
```
It fetches, installs dependencies, builds the SQLite street mirror if it is
missing or stale, runs tests, runs `locatron check`, installs changed systemd and
nginx files, then restarts services. `deploy/install.sh` is for first-time setup
or repair only.

What is *deployed* is recorded in `/opt/locatron/.deployed-sha`, owned by root and
deliberately outside the checkout so `git reset --hard` cannot touch it. It is
written only after the services restart and are verified up, and the "nothing to
do" shortcut now requires origin, the checkout **and** that file to agree. Before
this, the shortcut compared the checkout against origin only, so a deploy that
fetched and then failed before the restart left the checkout looking perfect while
production kept serving the previous commit — and every later run reported nothing
to do. Production was a day stale that way once.

If the fetch changes `deploy.sh` itself, the run hands over to the new copy with
`exec` before doing anything else. bash reads a script incrementally rather than
all at once, so without that a deploy that updates deploy.sh finishes as a mix of
old and new lines — which is how the first live deploy of `.deployed-sha` fetched
the code that writes the record and then ran the tail that does not.

To ask what is running without deploying anything:

```bash
/opt/locatron/app/deploy/deploy.sh --status
```

It prints the checkout SHA, the deployed SHA and each service's start time, warns
when a service started before the commit it is supposed to be running, and exits
non-zero on any drift — so it works from cron or a health check, not just by eye.
On the container, CLI commands go through `deploy/lc.sh`. Never a bare `uv run` as
root:
```bash
/opt/locatron/app/deploy/lc.sh check
/opt/locatron/app/deploy/lc.sh check --deep     # adds the full mirror digest
/opt/locatron/app/deploy/lc.sh build streets
/opt/locatron/app/deploy/lc.sh parse "65 clifton park drive 3201 carrum downs"
```
`lc.sh` reproduces exactly the environment deploy.sh uses — same venv, same env
file, same user, same working directory — so a command run by hand behaves the way
it behaves during a deploy. Running `uv run locatron ...` as root in
`/opt/locatron/app` instead leaves root-owned files behind: uv creates a `.venv`
next to the project when it cannot find the configured interpreter, and the next
deploy runs as `locatron`, cannot write it, and fails somewhere unrelated to the
cause. That has happened once already.
On Windows, from the repo root, the bare CLI is fine:
```bash
uv run locatron check                  # config and database health
uv run locatron schema                 # inspect table structure
uv run locatron sample Cities -n 5     # see real values, not just types
uv run locatron norm "Greater Melbourne"
uv run locatron resolve "Greater Melbourne"
uv run locatron resolve --file inputs.txt --out results.csv   # batch, one per line
uv run locatron parse "12 Clifton Street 3201"
uv run locatron golden                 # accuracy against the golden set
```
`locatron schema` and `locatron sample` exist specifically so Claude Code
inspects the real database rather than guessing column names.
---
Invariants
These are in `CLAUDE.md` in full. The two that matter most:
Upstream tables are read-only. Never INSERT, UPDATE, DELETE or ALTER
`address_ref`, `AustralianPostcodes`, `Cities`, `Countries`,
`aus_state_bucket`, `country_bucket` from application code. Enforced by MySQL
grants as well as convention.
One normalize() function. `locatron/normalize.py` is the only place
normalisation happens. Never reimplement it in SQL — build scripts leave
`norm_key` NULL and `scripts/normalize_pass.py` fills them by importing the same
function the resolver calls. Changing it requires bumping `NORM_VERSION`,
rerunning the normalize pass with `--all`. (There is no resolve cache yet, so
there is nothing to flush; see the Architecture section of CLAUDE.md for the
rule that applies when one is added.) Build-time and
query-time normalisation drifting apart produces silent misses that look like
bad data rather than a bug.
---
Known data traps
Learned the hard way. Do not rediscover these.
`CAST('' AS DECIMAL)` returns 0, not NULL. Averaging blank coordinates
drags centroids toward (0,0). Always `CAST(NULLIF(TRIM(col),'') AS DECIMAL(...))`.
Derived keys are not injective. `street_key` is built from three columns but
the primary key covers fewer, so `('HAMILTON','CR')` and `('HAMILTON CR','')`
both produce `HAMILTON CR`. Aggregate to variant level first, then collapse
with an explicit tiebreak.
G-NAF postcodes lose leading zeros on careless loads. NT is 0800 to 0899.
`LPAD(TRIM(POSTCODE),4,'0')` when **building** a derived table, and never in a
WHERE against `address_ref`: its POSTCODE is already four characters on every
row, and wrapping the indexed column turns a 1 ms index dive into a 10.6 s full
scan.
G-NAF has no PO Boxes. Postal addresses resolve via
`locatron_locality.is_postal_only = 1`.
`address_ref` uses blank strings, not NULLs. `NULLIF(TRIM(col),'')` before any
NULL check.
`STATE` includes `OT` for external territories. Exclude it from Australian
bounding-box sanity checks.
Alias rows in G-NAF point at their principal via `PRINCIPAL_PID`. The indicator
values are single letters, not words: `ALIAS_PRINCIPAL` is `'P'` or `'A'`, so
querying `= 'ALIAS'` matches nothing and returns silently. Exclude them from
canonical aggregates; use them to seed aliases. A resolve that lands on one
returns the alias row itself — it carries the street and number the input used —
with `canonical_pid` and `principal` naming the row G-NAF considers canonical.
---
Outstanding
Phase 3 (bulk export) is the next substantial piece; none of these block it.
Shared secret not rotated. The `X-Locatron-Edge` value was pasted into a
chat. Deliberately deferred until the build settles. Rotate on the container
in `/etc/nginx/sites-available/locatron` and in both edge nginx blocks.
AU gazetteer load is slow. 3.4s for 16,228 entries against 1.8s for
44,342 cities, roughly seven times slower per entry. And 16,228 looks low
against 18,567 localities plus 61,155 aliases. Probably more round trips than
needed. Off the request path now, so not urgent, but it is 60% of a 5.5s
startup.
`locatron_unresolved` has no real traffic in it. It holds nine synthetic rows
written by test runs before `tests/conftest.py` blocked that; all nine are marked
`reviewed = 1` so they are out of the triage queue, and removing them needs
`locatron_build` or a DBA, because the service account has INSERT and UPDATE on
that table but not DELETE. The flywheel is the next thing worth doing:

```bash
uv run locatron resolve --file linkedin-strings.txt --out results.csv
```

Push a few thousand real scraped strings through, review the table by
`hit_count`, and promote the genuine entries into `locatron_locality_alias`. That
loop is what makes this good over months, and nothing has turned it yet.
No resolve cache. `config.py`, `.env.example` and `MatchMethod.CACHE` are all
placeholders; there is no `cache.py` and nothing has ever been written to Redis.
When one is added its key must carry both `NORM_VERSION` and a `PARSER_VERSION` —
see the Architecture section of `CLAUDE.md`. It is also the obvious answer to the
23 ms unresolvable-input cost below.
Sub-dwelling vocabulary is deliberately narrow. `parse/components.py` knows
UNIT, FLAT, APARTMENT and the slash form; G-NAF's `FLAT_TYPE` has about thirty
more. Each keyword added is a token taken away from the street name, so they go in
on evidence from `locatron_unresolved`, not on guesswork.
API keys not wired up. Table exists, no code. Access is currently gated
only by the shared-secret header at nginx. Planned as a table plus three CLI
commands, no web UI.
Cloudflare bot protection returns 403 to non-browser user agents on
`/locatron`. Fine now, will block a Databricks job later. Fix is a WAF skip
rule scoped to a source IP or an API key header.
Edge access log shows Cloudflare IPs rather than real clients. Add
Cloudflare `real_ip` config.
A full state name can fuzzy-match an unrelated suburb on the world path.
`Western Australia` answers locality `WESTMERE` with admin1 `VIC` at confidence
0.61 — a Victorian suburb for a Western Australian state name, and the wrong
state into the bargain. `South Australia` behaves the same way. Pre-existing
phase-1 behaviour, unrelated to routing: these inputs carry no address signal, so
they never reach the AU path, and the AU path's own gate already refuses this
class of match (see the Routing section of `CLAUDE.md`). The world path has no
equivalent rule. The fix is probably to stop a fuzzy locality outranking a
`state_bucket` hit when the input is exactly a state name, which is the world-path
analogue of `Span.within()`. Not urgent — `VIC`, `Victoria Australia`,
`New South Wales`, `Queensland`, `Tasmania` and `Northern Territory` all answer
`admin1` correctly, and `golden.csv` covers those — but it is wrong, and someone
will report it.
An unresolvable input costs about 23 ms in the world fuzzy sweep. `asdfghjkl`
measures p50 23.4 ms, p95 29.1 ms, against 0.10 ms for `Delhi`; any string that
matches nothing exactly pays the same, because the sweep runs rapidfuzz across
~48k cities and only runs when nothing matched. That is the wrong way round for
bulk: a LinkedIn scrape is full of `Remote`, `Work from home` and worse, so the
slowest inputs are the ones a batch has most of. 200k junk rows is about 80
minutes of fuzzy matching. The AU path had the same shape and was fixed by
skipping the sweep unless the input looks like an address (see
`locatron/resolve/au.py`); the world path needs its own version of that
reasoning, plus the resolve cache, which does not exist yet.
---
Companion documents
`CLAUDE.md` in the repo — invariants and data traps, read by Claude Code
automatically
`EDGE-PROXY-PATTERN.md` — the nginx, Cloudflare and NAT setup, with a
symptom-first trap list and a five-hop diagnostic sequence. Generic across
projects, not Locatron-specific. Worth attaching to any prompt that touches
the edge server.