"""Street matching for a locality hypothesis.

TEMPORARY DEPARTURE FROM CLAUDE.md, approved deliberately.

CLAUDE.md:114-117 says the street gazetteer lives in local SQLite, not Python
memory and not MySQL on the request path, because per-worker Python objects are
duplicated and refcounting defeats copy-on-write. That is still the target. None
of it exists yet: there is no `db/local.py`, no streets loader, no
`locatron.build.refresh` (the module `locatron-build.service` already points at),
and `config.sqlite_path` is declared and unread.

So street rows come from `locatron_street` in MySQL for now, through exactly one
function. The rules that make that swappable rather than load-bearing:

- `streets_for_many()` is the only place street rows are read. Nothing outside
  this module queries `locatron_street`.
- The matcher goes through the batch form, so resolving costs one round trip for
  all three locality hypotheses rather than three.
- The service account's existing SELECT grant is enough. No new grants.
- No in-process cache. The SQLite mirror is the fix; a stopgap cache would
  become the thing nobody removes.
- The matcher takes its source as an argument, so tests inject a fixture and run
  without a database, and the swap to SQLite changes one function.

`address_ref` is never touched here. Fuzzy matching happens against the few
dozen streets of one locality, never across the 532,182-row table and never
across the 15.9M address rows.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from rapidfuzz.distance import Levenshtein
from sqlalchemy import text

from locatron.db import mysql
from locatron.parse.locality import Hypothesis as LocalityHypothesis
from locatron.parse.locality import ngram_spans
from locatron.parse.scoring import (
    DEFAULT_WEIGHTS,
    NAME_SIMILARITY_MIN,
    STREET_HYPOTHESES,
    TYPE_MISMATCH_PENALTY,
    Weights,
)
from locatron.parse.tokens import Span, TokenStream

#: (state, locality, postcode) — the grain of `locatron_street`, and what a
#: locality hypothesis supplies.
LocalityKey = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class StreetRow:
    """One street in one locality, as `locatron_street` stores it.

    `street_name` and `street_type` are kept apart rather than only as the
    collapsed `street_key`, because matching scores them separately: the input's
    trailing token against the type, the rest against the name. The key is still
    carried, since it is what the build script already collapsed
    ('HAMILTON','CR') and ('HAMILTON CR','') onto and what a caller reports.
    """

    state: str
    locality: str
    postcode: str
    street_key: str
    street_name: str
    street_type: str
    street_suffix: str
    address_count: int
    lat: float | None = None
    lng: float | None = None

    @property
    def key(self) -> LocalityKey:
        return (self.state, self.locality, self.postcode)


#: What the matcher needs from a street store. Satisfied by
#: `streets_for_many()` and by a test fixture, which is the point.
StreetSource = Callable[[Sequence[LocalityKey]], Mapping[LocalityKey, tuple[StreetRow, ...]]]

_COLUMNS = (
    "state, locality, postcode, street_key, street_name, street_type, "
    "street_suffix, address_count, lat, lng"
)


def _normalise_key(key: LocalityKey) -> LocalityKey:
    """Pad the postcode so a key built from an int still matches."""
    state, locality, postcode = key
    return (state, locality, postcode.rjust(4, "0"))


def streets_for_many(
    keys: Sequence[LocalityKey],
) -> Mapping[LocalityKey, tuple[StreetRow, ...]]:
    """Every street of each locality, in one query.

    The only place street rows are read. One statement for all the keys, because
    the matcher runs against the top three locality hypotheses and three round
    trips on a latency-sensitive path would be three times the cost of one.

    Keys are matched on (state, locality, postcode), which is the leading part
    of the table's primary key, so each disjunct is index-backed.
    """
    # Normalise on the way in, and key the result by the normalised form. The
    # column is char(4) and NT is 0800-0899, so a caller that went through an int
    # anywhere arrives with '800'; keying the output by the raw input instead
    # returned nothing, because the rows come back padded and did not match.
    unique = list(dict.fromkeys(_normalise_key(k) for k in keys))
    if not unique:
        return {}

    clauses: list[str] = []
    params: dict[str, str] = {}
    for i, (state, locality, postcode) in enumerate(unique):
        clauses.append(f"(state = :s{i} AND locality = :l{i} AND postcode = :p{i})")
        params[f"s{i}"] = state
        params[f"l{i}"] = locality
        params[f"p{i}"] = postcode

    sql = f"SELECT {_COLUMNS} FROM locatron_street WHERE {' OR '.join(clauses)}"  # noqa: S608

    out: dict[LocalityKey, list[StreetRow]] = {k: [] for k in unique}
    with mysql.session_scope() as s:
        for r in s.execute(text(sql), params).mappings():
            row = StreetRow(
                state=r["state"],
                locality=r["locality"],
                postcode=str(r["postcode"]).rjust(4, "0"),
                street_key=r["street_key"],
                street_name=r["street_name"],
                street_type=r["street_type"],
                street_suffix=r["street_suffix"],
                address_count=int(r["address_count"] or 0),
                lat=float(r["lat"]) if r["lat"] is not None else None,
                lng=float(r["lng"]) if r["lng"] is not None else None,
            )
            out.setdefault(row.key, []).append(row)

    # Biggest first, so a caller taking the head gets the most-addressed street
    # and two runs never disagree.
    return {
        k: tuple(sorted(v, key=lambda r: (-r.address_count, r.street_key))) for k, v in out.items()
    }


def streets_for(key: LocalityKey) -> tuple[StreetRow, ...]:
    """One locality's streets. A convenience over the batch form, not a second
    query path."""
    normalised = _normalise_key(key)
    return streets_for_many([normalised]).get(normalised, ())


# ---------------------------------------------------------------------------
# street type table
# ---------------------------------------------------------------------------
#
# G-NAF ships street types as codes, and `locatron_street.street_type` stores
# them unchanged -- `sql/build_locatron_street.sql` only TRIMs and UPPERs. So the
# stored key for Clifton Park Drive is 'CLIFTON PARK DR' and the spelled form is
# absent from the data entirely.
#
# ReferenceDB has no street-type authority table (no STREET_TYPE_AUT; the only
# street-bearing tables are `address_ref` and `locatron_street`), and the build
# SQL applies no mapping, so there is nothing to derive this from. It is
# hand-written, and `tests/parse/test_street.py` asserts it covers every distinct
# code in the table so a new one fails loudly rather than silently mismatching.
#
# This is a deliberate match-time exception to the one-normalize() rule, recorded
# in CLAUDE.md. It is NOT normalisation:
#   - it is only ever consulted for the input's TRAILING token;
#   - it generates alternative readings, never rewrites anything;
#   - it is never applied to a stored key or to a norm_key;
#   - `normalize()` is untouched, and no norm_key in the database depends on it.
# The last point is what makes it safe. Rewriting 'DRIVE' to 'DR' destructively
# would corrupt THE HORSLEY DRIVE, whose *name* is 'THE HORSLEY DRIVE' with a
# blank type -- 46 keys end in ' DRIVE', 319 in ' ROAD', 127 in ' AVENUE', and
# they are names.

#: Code -> extra spellings an input might use for it.
TYPE_SPELLINGS: dict[str, frozenset[str]] = {
    "ACCS": frozenset({"ACCESS"}),
    "ALLY": frozenset({"ALLEY"}),
    "AMBL": frozenset({"AMBLE"}),
    "APP": frozenset({"APPROACH"}),
    "ARC": frozenset({"ARCADE"}),
    "ARTL": frozenset({"ARTERIAL"}),
    "AV": frozenset({"AVENUE", "AVE"}),
    "BCH": frozenset({"BEACH"}),
    "BDWY": frozenset({"BROADWAY"}),
    "BR": frozenset({"BRACE"}),
    "BRK": frozenset({"BREAK"}),
    "BVD": frozenset({"BOULEVARD", "BLVD"}),
    "BVDE": frozenset({"BOULEVARDE"}),
    "BWLK": frozenset({"BOARDWALK"}),
    "BYPA": frozenset({"BYPASS"}),
    "CCT": frozenset({"CIRCUIT"}),
    "CH": frozenset({"CHASE"}),
    "CIR": frozenset({"CIRCLE"}),
    "CL": frozenset({"CLOSE"}),
    "CMMN": frozenset({"COMMON"}),
    "CMMNS": frozenset({"COMMONS"}),
    "CNR": frozenset({"CORNER"}),
    "CON": frozenset({"CONCOURSE"}),
    "CPS": frozenset({"COPSE"}),
    "CR": frozenset({"CRESCENT", "CRES"}),
    "CRCS": frozenset({"CIRCUS"}),
    "CRSE": frozenset({"COURSE"}),
    "CRSG": frozenset({"CROSSING"}),
    "CRSS": frozenset({"CROSS"}),
    "CRST": frozenset({"CREST"}),
    "CSAC": frozenset({"CUL-DE-SAC"}),
    "CSWY": frozenset({"CAUSEWAY"}),
    "CT": frozenset({"COURT"}),
    "CTR": frozenset({"CENTRE"}),
    "CTYD": frozenset({"COURTYARD"}),
    "CUTT": frozenset({"CUTTING"}),
    "DE": frozenset({"DEVIATION"}),
    "DIV": frozenset({"DIVIDE"}),
    "DOM": frozenset({"DOMAIN"}),
    "DR": frozenset({"DRIVE", "DRV"}),
    "DSTR": frozenset({"DISTRIBUTOR"}),
    "DVWY": frozenset({"DRIVEWAY"}),
    "ELB": frozenset({"ELBOW"}),
    "ENT": frozenset({"ENTRANCE"}),
    "ESMT": frozenset({"EASEMENT"}),
    "ESP": frozenset({"ESPLANADE"}),
    "EST": frozenset({"ESTATE"}),
    "EXP": frozenset({"EXPRESSWAY"}),
    "EXTN": frozenset({"EXTENSION"}),
    "FAWY": frozenset({"FAIRWAY"}),
    "FITR": frozenset({"FIRETRAIL"}),
    "FLNE": frozenset({"FIRELINE"}),
    "FOLW": frozenset({"FOLLOW"}),
    "FRTG": frozenset({"FRONTAGE"}),
    "FSHR": frozenset({"FORESHORE"}),
    "FTRK": frozenset({"FIRETRACK"}),
    "FWY": frozenset({"FREEWAY"}),
    "GDN": frozenset({"GARDEN"}),
    "GDNS": frozenset({"GARDENS"}),
    "GLDE": frozenset({"GLADE"}),
    "GLY": frozenset({"GULLY"}),
    "GR": frozenset({"GROVE"}),
    "GRA": frozenset({"GRANGE"}),
    "GRN": frozenset({"GREEN"}),
    "GTE": frozenset({"GATE"}),
    "GWY": frozenset({"GATEWAY"}),
    "HLLW": frozenset({"HOLLOW"}),
    "HRBR": frozenset({"HARBOUR"}),
    "HTH": frozenset({"HEATH"}),
    "HTS": frozenset({"HEIGHTS"}),
    "HVN": frozenset({"HAVEN"}),
    "HWY": frozenset({"HIGHWAY"}),
    "ID": frozenset({"ISLAND"}),
    "JNC": frozenset({"JUNCTION"}),
    "LDG": frozenset({"LANDING"}),
    "LKT": frozenset({"LOOKOUT"}),
    "LNKWAY": frozenset({"LINKWAY"}),
    "LNWY": frozenset({"LANEWAY"}),
    "MANR": frozenset({"MANOR"}),
    "MNDR": frozenset({"MEANDER"}),
    "MTWY": frozenset({"MOTORWAY"}),
    "NTH": frozenset({"NORTH"}),
    "OTLK": frozenset({"OUTLOOK"}),
    "OTLT": frozenset({"OUTLET"}),
    "PDE": frozenset({"PARADE"}),
    "PKT": frozenset({"POCKET"}),
    "PL": frozenset({"PLACE"}),
    "PLZA": frozenset({"PLAZA"}),
    "PNT": frozenset({"POINT"}),
    "PREC": frozenset({"PRECINCT"}),
    "PROM": frozenset({"PROMENADE"}),
    "PRST": frozenset({"PURSUIT"}),
    "PSGE": frozenset({"PASSAGE"}),
    "PWAY": frozenset({"PATHWAY"}),
    "PWY": frozenset({"PARKWAY"}),
    "QDRT": frozenset({"QUADRANT"}),
    "QY": frozenset({"QUAY"}),
    "QYS": frozenset({"QUAYS"}),
    "RCH": frozenset({"REACH"}),
    "RD": frozenset({"ROAD"}),
    "RDGE": frozenset({"RIDGE"}),
    "RES": frozenset({"RESERVE"}),
    "RMBL": frozenset({"RAMBLE"}),
    "RND": frozenset({"ROUND"}),
    "RSNG": frozenset({"RISING"}),
    "RTE": frozenset({"ROUTE"}),
    "RTN": frozenset({"RETURN"}),
    "RTT": frozenset({"RETREAT"}),
    "RVR": frozenset({"RIVER"}),
    "SBWY": frozenset({"SUBWAY"}),
    "SLPE": frozenset({"SLOPE"}),
    "SPUR": frozenset({"SPUR"}),
    "SQ": frozenset({"SQUARE"}),
    "ST": frozenset({"STREET"}),
    "STAI": frozenset({"STAIRS"}),
    "STH": frozenset({"SOUTH"}),
    "STRP": frozenset({"STRIP"}),
    "SVWY": frozenset({"SERVICEWAY"}),
    "TCE": frozenset({"TERRACE"}),
    "THRU": frozenset({"THROUGHWAY"}),
    "TKWY": frozenset({"TRUCKWAY"}),
    "TRK": frozenset({"TRACK"}),
    "TRL": frozenset({"TRAIL"}),
    "VLLY": frozenset({"VALLEY"}),
    "VSTA": frozenset({"VISTA"}),
    "VWS": frozenset({"VIEWS"}),
    "WDS": frozenset({"WOODS"}),
    "WHRF": frozenset({"WHARF"}),
    "WKWY": frozenset({"WALKWAY"}),
    "WTRS": frozenset({"WATERS"}),
    "WTWY": frozenset({"WATERWAY"}),
}

#: Codes that already are the English word, so they need no expansion.
SELF_SPELLED_TYPES: frozenset[str] = frozenset(
    {
        "ANNEX",
        "BANK",
        "BAY",
        "BEND",
        "BOWL",
        "BRAE",
        "BROW",
        "COVE",
        "CSO",
        "DALE",
        "DASH",
        "DELL",
        "DENE",
        "DIP",
        "DOCK",
        "DOWN",
        "EAST",
        "EDGE",
        "END",
        "FLAT",
        "FORD",
        "FORK",
        "GAP",
        "GLEN",
        "HILL",
        "HUB",
        "KEY",
        "KEYS",
        "LANE",
        "LINE",
        "LINK",
        "LOOP",
        "LYNN",
        "MALL",
        "MEAD",
        "MEWS",
        "NOOK",
        "PARK",
        "PASS",
        "PATH",
        "PORT",
        "RAMP",
        "REST",
        "RIDE",
        "RISE",
        "ROW",
        "RUN",
        "TARN",
        "TOP",
        "TOR",
        "TURN",
        "TWIST",
        "VALE",
        "VIEW",
        "WALK",
        "WAY",
        "WEST",
        "WYND",
    }
)

#: Codes whose expansion is not derivable from anything in ReferenceDB.
#: Accepted as written only; an input spelling them out gets no type credit.
#: Filled in when a source appears.
UNVERIFIED_TYPES: frozenset[str] = frozenset({"BA", "BIDI", "CLR", "CNTN", "CNWY", "CRF", "VLLA"})


def known_types() -> frozenset[str]:
    """Every code the table accounts for. The coverage test compares this
    against the distinct street_type values in locatron_street."""
    return frozenset(TYPE_SPELLINGS) | SELF_SPELLED_TYPES | UNVERIFIED_TYPES


def type_forms(code: str) -> frozenset[str]:
    """Every token that denotes `code`, the code itself included."""
    if not code:
        return frozenset()
    return frozenset({code}) | TYPE_SPELLINGS.get(code, frozenset())


def codes_for_token(token: str) -> frozenset[str]:
    """Every stored code the input token could denote.

    The reverse of the table. 'STREET' and 'ST' both give {'ST'}; 'COURT' gives
    {'CT'}. An unknown token gives an empty set, which is how a street name that
    happens to sit last ('THE HORSLEY DRIVE' read as name+type) fails reading A
    and falls through to reading B.
    """
    if not token:
        return frozenset()
    out = {code for code, forms in TYPE_SPELLINGS.items() if token in forms}
    if token in SELF_SPELLED_TYPES or token in UNVERIFIED_TYPES:
        out.add(token)
    if token in TYPE_SPELLINGS:
        out.add(token)
    return frozenset(out)


# ---------------------------------------------------------------------------
# street matching
# ---------------------------------------------------------------------------


def name_similarity(a: str, b: str) -> float:
    """0-100 similarity between two street names.

    Levenshtein, not Jaro-Winkler, and applied to the name part only.

    To be clear about which problem this solves: it is NOT the CR/CT/ST tie.
    Neither metric separates those -- 'HAMILTON CRESCENT' scores 92.94 against
    HAMILTON CR, HAMILTON CT and HAMILTON ST under Jaro-Winkler and 64.71
    against all three under Levenshtein. The type is settled by TYPE_SPELLINGS,
    not by similarity, which is the whole reason name and type are scored apart.

    What Levenshtein fixes is the name comparison. Jaro-Winkler weights a shared
    prefix, so it badly over-scores a name whose tail differs: 'CLIFTON PARK'
    against 'CLIFTON' is 91.67 under Jaro-Winkler and 58.33 under Levenshtein,
    and 'SMITH' against 'SMITHFIELD' is 90.00 against 50.00. At any usable
    threshold the prefix-weighted metric would treat a shorter or longer street
    name as the same street. Levenshtein counts the difference wherever it falls.
    """
    if not a or not b:
        return 0.0
    return Levenshtein.normalized_similarity(a, b) * 100.0


@dataclass(frozen=True, slots=True)
class StreetMatch:
    """One street a run of tokens could name, and how well."""

    row: StreetRow
    span: Span
    name_score: float
    """0-100 Levenshtein on the name part."""
    reading: str
    """'name+type' when the trailing token was read as the type, 'whole-name'
    when the entire run was matched against the name alone."""
    type_matched: bool
    """The trailing token denoted the stored type, exactly or via the table."""
    type_mismatch: bool
    """The name matched but the type did not, and no same-name street in this
    locality carries the input's type. Scored with a penalty, not rejected."""

    @property
    def street_key(self) -> str:
        return self.row.street_key

    @property
    def match_score(self) -> float:
        """Name similarity as 0-1, less the penalty if the type disagreed.

        Ordering has to go by this rather than by category, or a perfect name
        with the wrong type loses to a poor name with no type at all:
        'CLIFTON STREET' in Carrum Downs reached CLIFTON PARK DR at 64.29 on the
        whole-name reading and ranked it above CLIFTON GR, whose name matches
        exactly and is only a Grove rather than a Street.
        """
        score = self.name_score / 100.0
        if self.type_mismatch:
            score += TYPE_MISMATCH_PENALTY
        return score


def _best_reading(
    tokens: tuple[str, ...], row: StreetRow, intent: frozenset[str], blocked: bool
) -> tuple[float, str, bool, bool] | None:
    """(name_score, reading, type_matched, type_mismatch) for one candidate.

    Two readings per candidate, the better one wins:

    A  the trailing token is the type and the rest is the name. Needs at least
       two tokens, and needs the candidate to have a type at all.
    B  the whole run is the name. This is what matches THE HORSLEY DRIVE, whose
       type is blank and whose name ends in the word DRIVE.
    """
    whole = " ".join(tokens)
    best: tuple[float, str, bool, bool] | None = None

    if len(tokens) > 1 and row.street_type:
        head = " ".join(tokens[:-1])
        score = name_similarity(head, row.street_name)
        if row.street_type in intent:
            best = (score, "name+type", True, False)
        elif intent and not blocked:
            # Name may be right, type is not. Allowed, penalised in scoring.
            best = (score, "name+type", False, True)
        elif not intent:
            # The trailing token denotes no known type, so reading A is not
            # really a name+type split. Leave it to reading B.
            best = None

    score_b = name_similarity(whole, row.street_name)
    if best is None or score_b > best[0]:
        best = (score_b, "whole-name", False, False)
    return best


def match_streets(
    tokens: tuple[str, ...],
    span: Span,
    rows: Sequence[StreetRow],
    *,
    min_name_score: float = NAME_SIMILARITY_MIN,
) -> tuple[StreetMatch, ...]:
    """Every street in `rows` that this contiguous run could name, best first.

    `tokens` must be contiguous -- the caller passes one run from
    `TokenStream.runs()`, so a street is never stitched out of tokens on both
    sides of the locality.

    Type mismatch follows one rule: if a same-name street carrying the input's
    type exists in this locality, it wins outright and the mismatched types are
    not offered at all. Only when no such street exists is a mismatch allowed,
    and then it carries TYPE_MISMATCH_PENALTY.
    """
    if not tokens or not rows:
        return ()

    intent = codes_for_token(tokens[-1]) if len(tokens) > 1 else frozenset()
    head = " ".join(tokens[:-1]) if len(tokens) > 1 else ""

    # Does this locality hold a street whose name matches the head *and* whose
    # type is what the input asked for? If so, mismatches are blocked outright.
    blocked = bool(
        intent
        and head
        and any(
            r.street_type in intent and name_similarity(head, r.street_name) >= min_name_score
            for r in rows
        )
    )

    out: list[StreetMatch] = []
    for row in rows:
        reading = _best_reading(tokens, row, intent, blocked)
        if reading is None:
            continue
        score, kind, matched, mismatch = reading
        if score < min_name_score:
            continue
        out.append(
            StreetMatch(
                row=row,
                span=span,
                name_score=score,
                reading=kind,
                type_matched=matched,
                type_mismatch=mismatch,
            )
        )

    # By effective score, so the type penalty competes with name similarity
    # rather than partitioning ahead of it. Exact type breaks a tie, then size.
    return tuple(
        sorted(
            out,
            key=lambda m: (
                -m.match_score,
                not m.type_matched,
                -m.row.address_count,
                m.street_key,
            ),
        )
    )


# ---------------------------------------------------------------------------
# joint scoring
# ---------------------------------------------------------------------------


def _net_contribution(
    ts: TokenStream,
    claimed: Sequence[Span],
    match: StreetMatch | None,
    weights: Weights,
) -> float:
    """What choosing `match` is worth: the street score it earns, less the cost
    of every token it leaves for nobody to explain.

    Span selection goes by this rather than by match score alone. Match score
    alone preferred a shorter span that looked better in isolation --
    'CLIFTON STREET' matched the single token CLIFTON to CLIFTON GR at a clean
    1.00 and orphaned STREET, rather than matching both tokens at 0.70 and
    reporting the type mismatch. Same street either way, but a mismatch has to
    stay visible instead of being relabelled as noise.
    """
    spans = [*claimed, match.span] if match is not None else list(claimed)
    tokens_left = sum(len(sp) for sp in ts.runs(spans))
    earned = weights.street_match_weight * match.match_score if match is not None else 0.0
    return earned + weights.unexplained_token_penalty * tokens_left


@dataclass(frozen=True, slots=True)
class StreetHypothesis:
    """A locality reading plus the street it explains, scored together."""

    locality: LocalityHypothesis
    street: StreetMatch | None
    """None when nothing cleared NAME_SIMILARITY_MIN. The hypothesis is then
    locality-only and its street tokens count as unexplained."""
    score: float
    signals: dict[str, float]
    unexplained: tuple[Span, ...]
    """Runs no stage explained. Empty is the goal."""

    @property
    def granularity(self) -> str:
        return "street" if self.street is not None else "locality"

    @property
    def unexplained_tokens(self) -> int:
        return sum(len(s) for s in self.unexplained)

    def consumed(self) -> tuple[Span, ...]:
        spans = list(self.locality.consumed)
        if self.street is not None:
            spans.append(self.street.span)
        return tuple(sorted(spans))


def resolve_streets(
    ts: TokenStream,
    hypotheses: Sequence[LocalityHypothesis],
    *,
    consumed: Sequence[Span] = (),
    source: StreetSource = streets_for_many,
    top_n: int = STREET_HYPOTHESES,
    weights: Weights = DEFAULT_WEIGHTS,
) -> tuple[StreetHypothesis, ...]:
    """Match streets against the top `top_n` locality hypotheses and rerank.

    `consumed` are the spans Prompt A's extractors claimed -- street number,
    unit, PO box -- which never become part of a street name.

    All the localities are fetched in one call to `source`, so a resolve costs
    one round trip rather than one per hypothesis. `source` is an argument so a
    test can inject a fixture and so the SQLite mirror, when it lands, replaces
    one function.

    Reranking is the point: a weaker locality whose streets contain the input can
    overtake a stronger one whose streets do not.
    """
    if not hypotheses:
        return ()

    considered = list(hypotheses[:top_n])
    keys = [(h.candidate.state, h.candidate.locality, h.candidate.postcode) for h in considered]
    store = source(keys)

    out: list[StreetHypothesis] = []
    for h, key in zip(considered, keys, strict=True):
        claimed = [*consumed, *h.consumed]
        rows = store.get(key, ())

        # Every contiguous span of what is left, longest first. Contiguity is
        # structural: ngram_spans generates within runs, so a street can never be
        # stitched out of tokens on both sides of the locality.
        # Chosen on net contribution, not match score. See _net_contribution.
        best: StreetMatch | None = None
        best_net = _net_contribution(ts, claimed, None, weights)
        for gram in ngram_spans(ts, claimed):
            # match_streets is sorted, so its head is this span's best.
            matches = match_streets(tuple(t.text for t in ts.slice(gram.span)), gram.span, rows)
            if not matches:
                continue
            candidate_net = _net_contribution(ts, claimed, matches[0], weights)
            if candidate_net > best_net:
                best, best_net = matches[0], candidate_net

        street_claimed = [*claimed, best.span] if best is not None else claimed
        leftover = ts.runs(street_claimed)

        parts = dict(h.signals)
        if best is not None:
            parts["street_match"] = weights.street_match_weight * best.match_score
            if best.type_mismatch:
                # Already inside match_score; surfaced so a breakdown shows why.
                parts["street_type_mismatch"] = weights.type_mismatch_penalty
                parts["street_match"] -= weights.type_mismatch_penalty * (
                    weights.street_match_weight
                )
        n_left = sum(len(s) for s in leftover)
        if n_left:
            parts["unexplained_tokens"] = weights.unexplained_token_penalty * n_left

        out.append(
            StreetHypothesis(
                locality=h,
                street=best,
                score=sum(parts.values()),
                signals=parts,
                unexplained=leftover,
            )
        )

    return tuple(
        sorted(
            out,
            key=lambda s: (
                -s.score,
                s.unexplained_tokens,
                -s.locality.candidate.address_count,
                s.locality.candidate.state,
                s.locality.candidate.postcode,
            ),
        )
    )
