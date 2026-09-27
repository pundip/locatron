Locatron handover
State as of 26 September 2026. Written to start a fresh conversation without
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
only. Not started. This is the next phase.
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
Performance: 0.6ms median server-side resolve once warm. All four gunicorn
workers load gazetteers at startup via a `post_worker_init` hook, so there is
no cold-start penalty on the request path. About 106ms of the user-visible
latency is network (Melbourne to Cloudflare to Sydney to home NAT), which is
mostly unavoidable and is the argument for the batch endpoint.
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
│   ├── resolve/                  pipeline, scoring, world
│   ├── parse/                    EMPTY — phase 2 goes here
│   ├── api/app.py
│   └── bulk/                     EMPTY — phase 3
├── scripts/
│   ├── normalize_pass.py
│   ├── latency_check.py
│   └── git-hooks/pre-commit      blocks committing secrets
├── sql/                          build scripts for derived tables
├── deploy/                       install.sh, deploy.sh, nginx.conf, systemd
├── prompts/01-gazetteer-world-resolver.md
└── tests/golden/golden.csv       30 rows, the accuracy target
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
Phase 2: the AU address parser
The remaining substantial work. Everything below is design already settled.
The approach
Not a positional regex. The parser generates multiple parse hypotheses and
validates each against the gazetteer, keeping the best.
Worked example, `65 clifton park drive 3201 carrum downs`. Tokens:
`[65, CLIFTON, PARK, DRIVE, 3201, CARRUM, DOWNS]`. Note the postcode precedes
the locality, so a positional parser breaks here.
Find 4-digit tokens that validate against known postcodes. `3201` does,
`65` does not.
Match locality by testing token n-grams against `locatron_locality`.
`CARRUM DOWNS` hits, and it agrees with postcode 3201, which is strong
evidence the parse is right.
State falls out of the locality, cross-checked against `aus_state_bucket`.
Extract unit and level (`5/12`, `UNIT 5`, `L 3`) and street number,
including ranges (`14-40`) and alpha suffixes (`6C` — note G-NAF stores the
suffix inside `NUMBER_FIRST` in this table).
Remaining tokens are the street. This is where the gazetteer earns its keep:
`PARK` is itself a valid street type, so a naive right-to-left type match
gives street name `CLIFTON`, type `PARK`, leftover `DRIVE`. But the locality
is already known, so pull that locality's streets from `locatron_street` and
fuzzy match. `CLIFTON PARK DRIVE` wins outright.
Hit `address_ref` on the composite index for the full record.
Tie-breaking on the final lookup: prefer `ALIAS_PRINCIPAL = 'PRINCIPAL'`; if a
unit was supplied match `FLAT_NUMBER`, otherwise prefer the `PRIMARY` row.
Graceful degradation matters as much as exact matching. No number match falls
back to the street centroid (`granularity: street`); no street falls back to the
locality centroid (`granularity: locality`). PO boxes never match G-NAF at all
— `locatron_locality.is_postal_only = 1` is the signal, and they return
`granularity: postal`.
Fuzzy matching rule
Fuzzy match at gazetteer level, exact match at address level. Localities (~18k)
and cities (~48k) are small enough for rapidfuzz in memory. The 15.9M-row
address table is only ever hit with exact, index-backed lookups. Never fuzzy
match across `address_ref`.
Acceptance
`tests/golden/golden.csv` has the AU rows already written with a `note` column
explaining what each probes. Phase 2 makes these pass:
the two `65 Clifton Park Dr` orderings
`5/12 Smith Street Fitzroy VIC 3065` and its spelled-unit variant
`14-40 Wills Street Melbourne VIC 3000` (number range)
`Clifton Park Drive Carrum Downs` (street fallback)
`Carrum Downs VIC` (locality only)
`3201` (bare postcode)
`PO Box 45 World Square NSW 2002` (postal only)
`Hamilton Crescent Ryde NSW 2112` (exercises a merged street variant)
The world-place rows already pass. Do not regress them.
Prompt template
`prompts/01-gazetteer-world-resolver.md` has both the phase 1 prompt and a
reusable template at the bottom. The parts that make it work: an explicit
out-of-scope list, an instruction to inspect the schema rather than guess, and
numbered review points so you get four small diffs instead of one huge one.
For phase 2, review the hypothesis-scoring logic closely. That is where subtle
wrongness hides.
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
`LPAD(TRIM(POSTCODE),4,'0')` everywhere.
G-NAF has no PO Boxes. Postal addresses resolve via
`locatron_locality.is_postal_only = 1`.
`address_ref` uses blank strings, not NULLs. `NULLIF(TRIM(col),'')` before any
NULL check.
`STATE` includes `OT` for external territories. Exclude it from Australian
bounding-box sanity checks.
Alias rows in G-NAF (`ALIAS_PRINCIPAL = 'ALIAS'`) point at their principal via
`PRINCIPAL_PID`. Exclude them from canonical aggregates; use them to seed
aliases.
---
Outstanding
Not blocking phase 2.
Shared secret not rotated. The `X-Locatron-Edge` value was pasted into a
chat. Deliberately deferred until the build settles. Rotate on the container
in `/etc/nginx/sites-available/locatron` and in both edge nginx blocks.
nginx.conf reinstalls on every deploy. The comparison happens before the
secret substitution so the files always differ. Fix in progress.
AU gazetteer load is slow. 3.4s for 16,228 entries against 1.8s for
44,342 cities, roughly seven times slower per entry. And 16,228 looks low
against 18,567 localities plus 61,155 aliases. Probably more round trips than
needed. Off the request path now, so not urgent, but it is 60% of a 5.5s
startup.
`locatron_unresolved` is empty. Nothing has run through it yet. Once
phase 2 lands, push a few thousand real LinkedIn strings through the CLI and
review the top entries by `hit_count`. Promoting those into
`locatron_locality_alias` is the flywheel that makes this good over months.
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