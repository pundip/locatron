"""The Australian address path.

`resolve_au()` turns one input into as much of a G-NAF address as the input
supports, and `takes_au_path()` decides whether an input belongs here at all. The
world place path stays the default and the fallback; see the Routing section of
CLAUDE.md for the rule and the cases that shaped it.

Stages, in order, each already built and tested on its own:

    tokenize            -> tokens.py
    extract_components  -> components.py, arbitrated here
    generate_hypotheses -> locality.py, against the in-memory AU gazetteer
    resolve_streets     -> street.py, against the local SQLite mirror
    lookup              -> lookup.py, one exact dive into address_ref

Nothing here queries address_ref itself. That is lookup.py's job, and it is the
only stage allowed to, with exact index-backed keys only.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from locatron.gazetteer.au import AuGazetteer, load_au
from locatron.gazetteer.countries import CountryRow, load_countries
from locatron.parse.components import (
    PoBox,
    PostcodeCandidate,
    StreetNumber,
    UnitLevel,
    find_po_boxes,
    find_postcodes,
    find_street_numbers,
    find_units_and_levels,
)
from locatron.parse.locality import StateToken, find_state_tokens, generate_hypotheses
from locatron.parse.lookup import GnafRecord, LookupResult, lookup
from locatron.parse.lookup import Granularity as Rung
from locatron.parse.scoring import au_confidence
from locatron.parse.street import StreetHypothesis, resolve_streets
from locatron.parse.tokens import Span, TokenStream, tokenize
from locatron.schemas import (
    Admin1,
    AuAddress,
    Candidate,
    Geo,
    GeoSource,
    Granularity,
    MatchMethod,
    Principal,
)

#: Similarity a fuzzy locality must reach. The same value `locatron parse` uses,
#: so the developer command and the served answer cannot disagree.
FUZZY_MIN = 88

#: Most alternates to report. Three is what the street stage reranks, so a fourth
#: would be a candidate nothing ever compared against the winner.
MAX_CANDIDATES = 3

#: Floor on the winning hypothesis's joint score. Not a tuning knob: a negative
#: score means the parse explains less than it fails to.
AU_MIN_SCORE = 0.0


@lru_cache(maxsize=1)
def known_postcodes() -> frozenset[str]:
    """Every postcode the AU gazetteer knows, for validating a numeric token.

    Cached because it is 18.5k keys and the alternative is rebuilding the set on
    every request.
    """
    return frozenset(load_au().by_postcode)


# ---------------------------------------------------------------------------
# components
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Components:
    """What the extractors found, after the roles have been arbitrated."""

    boxes: tuple[PoBox, ...]
    units: tuple[UnitLevel, ...]
    postcodes: tuple[PostcodeCandidate, ...]
    numbers: tuple[StreetNumber, ...]
    claimed: tuple[Span, ...]
    """Every span an extractor took, so the street stage never reads one."""

    @property
    def unit(self) -> str | None:
        return next((u.value for u in self.units if u.kind == "unit"), None)

    @property
    def number(self) -> StreetNumber | None:
        return self.numbers[0] if self.numbers else None


def extract_components(ts: TokenStream, postcodes_known: frozenset[str]) -> Components:
    """Run the extractors and decide which token plays which role.

    Two rules, both load-bearing:

    A four-digit postcode token is not also offered as a street number, because
    it is a postcode far more often than a house number. A padded three-digit one
    is left available for both, because '810 Stuart Highway Winnellie' means a
    house number even though 0810 is a real postcode.
    """
    boxes = find_po_boxes(ts)
    units = find_units_and_levels(ts)
    postcodes = find_postcodes(ts, postcodes_known)

    claimed = [b.span for b in boxes] + [u.span for u in units]
    four_digit_starts = {c.span.start for c in postcodes if not c.padded}

    # A slash form is one token carrying a unit and a street number at once, which
    # is the whole point of it. The unit has already claimed that token, so
    # without this the slash form reports no number while the spelled form
    # 'UNIT 5 12' reports 12.
    slash_spans = {u.span for u in units if u.street_number_hint}

    numbers = tuple(
        n
        for n in find_street_numbers(ts)
        if n.span.start not in four_digit_starts
        and (
            not any(n.span.overlaps(s) for s in claimed) or (n.from_slash and n.span in slash_spans)
        )
    )
    claimed += [n.span for n in numbers]
    return Components(
        boxes=boxes,
        units=units,
        postcodes=postcodes,
        numbers=numbers,
        claimed=tuple(claimed),
    )


# ---------------------------------------------------------------------------
# one parse
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuParse:
    """Everything one parse produced, so routing and resolution share the work."""

    text: str
    ts: TokenStream
    components: Components
    states: tuple[StateToken, ...]
    hypotheses: tuple[StreetHypothesis, ...]

    @property
    def top(self) -> StreetHypothesis | None:
        return self.hypotheses[0] if self.hypotheses else None

    @property
    def runner_up(self) -> float | None:
        return self.hypotheses[1].score if len(self.hypotheses) > 1 else None


def parse(text: str, au: AuGazetteer | None = None, **street_kwargs: Any) -> AuParse:
    """Run every stage up to but not including the address_ref lookup.

    Cheap enough to run before routing has been decided: tokenising and the
    locality gazetteer are in memory, and the street stage is one indexed query
    against a local SQLite file that workers share through the page cache.
    """
    gaz = au if au is not None else load_au()
    ts = tokenize(text)
    components = extract_components(ts, known_postcodes())
    states = find_state_tokens(ts, gaz)
    hyps = generate_hypotheses(
        ts,
        gaz,
        consumed=components.claimed,
        postcodes=components.postcodes,
        po_box_found=bool(components.boxes),
        fuzzy_min=FUZZY_MIN,
    )
    joint = resolve_streets(ts, hyps, consumed=components.claimed, **street_kwargs)
    return AuParse(
        text=text,
        ts=ts,
        components=components,
        states=states,
        hypotheses=tuple(joint),
    )


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------


def triggers(p: AuParse) -> tuple[str, ...]:
    """Signals that this is an Australian address rather than a place name.

    The street triggers are narrow on purpose. A bare street match is not enough:
    'New York' matches street NEW ST in locality YORK at score 2.555 with nothing
    left unexplained, and routing that here would answer a New York query with a
    street in York, WA. What distinguishes a real Australian street is either an
    explicit type word in the input ('Clifton Park DRIVE') or a number in front
    of it.
    """
    out: list[str] = []
    if p.components.postcodes:
        out.append("postcode")
    if any(s.strong for s in p.states):
        out.append("state")
    if p.components.boxes:
        out.append("pobox")
    top = p.top
    if top is not None and top.street is not None:
        if top.street.reading == "name+type":
            out.append("street+type")
        if p.components.numbers:
            out.append("number+street")
    return tuple(out)


def takes_au_path(p: AuParse) -> tuple[bool, str]:
    """(route here, why not). See the Routing section of CLAUDE.md."""
    if not triggers(p):
        return False, "no AU signal"
    top = p.top
    if top is None:
        return False, "no locality hypothesis"
    if top.score < AU_MIN_SCORE:
        return False, f"joint score {top.score:.2f} below the floor"
    if top.locality.candidate.match == "fuzzy":
        # A fuzzy locality has to be evidence of its own, and there are two ways
        # it can fail to be.
        #
        # It leaves part of the input unexplained: 'Victoria Australia'
        # fuzzy-matches TORRITA and has AUSTRALIA spare.
        if top.unexplained:
            return False, "fuzzy locality with unexplained tokens"
        # Or it explains nothing the state token had not already explained, which
        # is a state name being re-read as a suburb. Every full state name does
        # this -- 'New South Wales' reaches SOUTH BOWENFELS, 'Western Australia'
        # reaches AUSTRALIND, 'Tasmania' reaches MATHINNA -- and each leaves
        # nothing unexplained, because the state token accounted for the tokens.
        # 'Ku-ring-gai NSW' also overlaps its state token but reaches a token
        # beyond it, which is the difference.
        state_span = top.locality.state_span
        if state_span is not None and top.locality.locality_span.within(state_span):
            return False, "fuzzy locality inside the state token"
        # Or it is a country name. Same error one level up: 'New South Wales
        # Australia' reaches locality AUSTRAL by fuzzy-matching AUSTRALIA, and
        # that token names the country, not a suburb of it.
        name = p.ts.text_of(top.locality.locality_span)
        if name and load_countries().lookup_token(name) is not None:
            return False, "fuzzy locality is a country name"
    return True, ""


# ---------------------------------------------------------------------------
# the answer
# ---------------------------------------------------------------------------

#: The ladder's rungs mapped onto the response vocabulary. `locality` is absent
#: because it splits; see `granularity_for`.
_RUNG_TO_GRANULARITY = {
    Rung.UNIT: Granularity.UNIT,
    Rung.ADDRESS: Granularity.ADDRESS,
    Rung.STREET: Granularity.STREET,
    # G-NAF holds no PO boxes, so a postal answer's postcode is the finest truth
    # available. There is no `postal` member, and adding one would break
    # consumers.
    Rung.POSTAL: Granularity.POSTCODE,
}

_GEO_SOURCE = {
    Granularity.UNIT: GeoSource.GNAF_PROPERTY_CENTROID,
    Granularity.ADDRESS: GeoSource.GNAF_PROPERTY_CENTROID,
    Granularity.STREET: GeoSource.GNAF_STREET_CENTROID,
    Granularity.LOCALITY: GeoSource.LOCALITY_CENTROID,
    Granularity.POSTCODE: GeoSource.POSTCODE_CENTROID,
}

_LOCALITY_METHOD = {
    "exact": MatchMethod.LOCALITY_EXACT,
    "alias": MatchMethod.LOCALITY_ALIAS,
    "fuzzy": MatchMethod.LOCALITY_FUZZY,
}


def names_its_locality(h: StreetHypothesis) -> bool:
    """Whether the input named the locality, or only implied it.

    A candidate that came from `candidates_for_postcode()` carries an empty
    `locality_span`: the input gave a postcode and nothing else, and a postcode
    can cover several localities -- 2000 covers nine. An exact test, not a
    heuristic.
    """
    return len(h.locality.locality_span) > 0


def granularity_for(h: StreetHypothesis, result: LookupResult) -> Granularity:
    """Where the ladder landed, in the response vocabulary."""
    if result.granularity != Rung.LOCALITY:
        return _RUNG_TO_GRANULARITY[result.granularity]
    return Granularity.LOCALITY if names_its_locality(h) else Granularity.POSTCODE


def match_method_for(h: StreetHypothesis, g: Granularity, result: LookupResult) -> MatchMethod:
    if g in (Granularity.UNIT, Granularity.ADDRESS):
        return MatchMethod.GNAF_EXACT
    if g is Granularity.STREET:
        return MatchMethod.GNAF_STREET_CENTROID
    if result.granularity is Rung.POSTAL:
        return MatchMethod.POSTAL_ONLY
    return _LOCALITY_METHOD.get(h.locality.candidate.match, MatchMethod.NONE)


def _blank_to_none(value: str) -> str | None:
    """address_ref uses '' for absent. A consumer should not have to know that."""
    return value.strip() or None


def to_au_address(rec: GnafRecord) -> AuAddress:
    """Every G-NAF column, blanks normalised to None."""
    return AuAddress(
        address_detail_pid=_blank_to_none(rec.address_detail_pid),
        flat_type=_blank_to_none(rec.flat_type),
        flat_number=_blank_to_none(rec.flat_number),
        level_type=_blank_to_none(rec.level_type),
        level_number=_blank_to_none(rec.level_number),
        number_first=_blank_to_none(rec.number_first),
        number_last=_blank_to_none(rec.number_last),
        lot_number=_blank_to_none(rec.lot_number),
        street_name=_blank_to_none(rec.street_name),
        street_type=_blank_to_none(rec.street_type),
        street_suffix=_blank_to_none(rec.street_suffix),
        locality_name=_blank_to_none(rec.locality_name),
        state=_blank_to_none(rec.state),
        postcode=_blank_to_none(rec.postcode),
        building_name=_blank_to_none(rec.building_name),
        address_site_name=_blank_to_none(rec.address_site_name),
        mb_code=_blank_to_none(rec.mb_code),
        legal_parcel_id=_blank_to_none(rec.legal_parcel_id),
        geocode_type=_blank_to_none(rec.geocode_type),
        alias_principal=_blank_to_none(rec.alias_principal),
        principal_pid=_blank_to_none(rec.principal_pid),
        primary_secondary=_blank_to_none(rec.primary_secondary),
        primary_pid=_blank_to_none(rec.primary_pid),
        date_created=_blank_to_none(rec.date_created),
        formatted=_blank_to_none(rec.address_label),
    )


def to_candidates(p: AuParse, winner_granularity: Granularity) -> list[Candidate]:
    """The runners-up, and for a bare postcode the localities it could mean."""
    if not p.hypotheses:
        return []
    winner_score = p.hypotheses[0].score
    out: list[Candidate] = []
    for h in p.hypotheses[1 : MAX_CANDIDATES + 1]:
        c = h.locality.candidate
        out.append(
            Candidate(
                label=" ".join(x for x in (c.locality, c.state, c.postcode) if x),
                confidence=au_confidence(h.score, winner_score, has_street=h.street is not None),
                granularity=winner_granularity,
                country="AUS",
                admin1=c.state or None,
                locality=c.locality or None,
                postcode=c.postcode or None,
                score=round(h.score, 4),
                reason=c.match,
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class AuAnswer:
    """The AU path's conclusion, in response terms but not yet an envelope.

    Separate from ResolveResponse so the pipeline stays the only place that builds
    one, and so this module needs to know nothing about timing or normalisation.
    """

    granularity: Granularity
    confidence: float
    match_method: MatchMethod
    admin1: Admin1 | None
    locality: str | None
    postcode: str | None
    geo: Geo | None
    au_address: AuAddress | None
    canonical_pid: str | None
    principal: Principal | None
    warnings: list[str]
    candidates: list[Candidate]
    unexplained: tuple[str, ...]
    """Input tokens no stage accounted for. Feeds the unresolved log."""


def resolve_au(p: AuParse, au: AuGazetteer | None = None, **lookup_kwargs: Any) -> AuAnswer:
    """Walk the ladder for the winning hypothesis and shape the answer.

    Assumes `takes_au_path()` already said yes, so a missing hypothesis here is a
    programming error rather than an input problem.
    """
    gaz = au if au is not None else load_au()
    top = p.top
    if top is None:  # pragma: no cover - guarded by takes_au_path
        raise ValueError("resolve_au called on a parse with no hypothesis")

    number = p.components.number
    street_centroid = (top.street.row.lat, top.street.row.lng) if top.street else None
    result = lookup(
        top,
        number_first=number.number_first if number else None,
        number_last=number.number_last if number else None,
        unit=p.components.unit,
        po_box=bool(p.components.boxes),
        street_centroid=street_centroid,
        **lookup_kwargs,
    )

    granularity = granularity_for(top, result)
    candidate = top.locality.candidate
    geo = (
        Geo(lat=result.lat, lng=result.lng, source=_GEO_SOURCE[granularity])
        if result.lat is not None and result.lng is not None
        else None
    )
    state = candidate.state or None
    return AuAnswer(
        granularity=granularity,
        confidence=au_confidence(
            top.score,
            p.runner_up,
            has_street=top.street is not None,
            cap=result.confidence_cap,
        ),
        match_method=match_method_for(top, granularity, result),
        admin1=Admin1(name=gaz.state_name(state) or state or "", code=state) if state else None,
        # A postcode-only answer names no locality: the input did not, and the
        # postcode may cover several. They are alternates, so they go in
        # `candidates`.
        locality=candidate.locality if names_its_locality(top) else None,
        postcode=candidate.postcode or None,
        geo=geo,
        au_address=to_au_address(result.record) if result.record is not None else None,
        canonical_pid=result.canonical_pid or None,
        principal=(
            Principal(pid=result.principal.pid, formatted=result.principal.address)
            if result.principal is not None
            else None
        ),
        warnings=list(result.warnings),
        candidates=to_candidates(p, granularity),
        unexplained=tuple(p.ts.text_of(s) for s in top.unexplained),
    )


def australia() -> CountryRow | None:
    """The AUS row from the same gazetteer the world path uses, so the two never
    disagree on how Australia is spelled."""
    return load_countries().get("AUS")
