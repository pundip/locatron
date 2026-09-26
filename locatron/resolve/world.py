"""The loose-place resolver: a free-text string to a country, admin1, and city.

Handles use case 1 — "Greater Melbourne", "Sydney Australia", "Delhi",
"Remote / Work from home". Knows nothing about street addresses; that is the AU
address path and it lives elsewhere.

## Why this is not "localities, then cities"

The obvious reading is to try one gazetteer and fall back to the other. It does
not work in either order. MELBOURNE is 15 rows in `locatron_locality` and 2 in
`Cities`; PERTH is 21 and 2. Localities-first answers "Melbourne" with a
postcode-level suburb when the caller wanted the city, and cities-first answers
"Carrum Downs" with nothing.

So both gazetteers propose candidates and the scorer decides, with one
structural rule:

    An Australian locality only competes with a world city of the same name
    when the input carries an Australian signal — a four-digit postcode or an
    explicit state token.

That rule is doing real work on two golden rows. "Springfield" has no AU
signal, so the ten `SPRINGFIELD` localities are suppressed and the answer is a
deliberately low-confidence US city rather than a confident Australian suburb.
"Ryde NSW 2112" has both signals, so the locality beats the city of the same
name. Scoring alone cannot express this: without the rule, an AU suburb that is
unique in the gazetteer ties exactly with a world city of the same name, and the
winner comes down to dict ordering.

## Peeling, not positional parsing

Input order varies. "Sydney Australia" has no delimiter at all, so splitting on
commas is not enough — the country has to be peeled off the token run. Each
peel takes the longest matching n-gram that still leaves something behind,
which is what keeps "Maryborough Victoria Australia" (a real `country_bucket`
value, matching the whole string) from collapsing to a bare country when it
should yield a locality in Victoria.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from rapidfuzz.distance import JaroWinkler

from locatron.config import Settings, get_settings
from locatron.gazetteer.au import AuGazetteer, LocalityRow, load_au
from locatron.gazetteer.cities import CityGazetteer, CityRow, load_cities
from locatron.gazetteer.countries import CountryGazetteer, CountryRow, load_countries
from locatron.normalize import ngrams, normalize, strip_qualifiers, tokens
from locatron.resolve import scoring
from locatron.resolve.scoring import MatchKind, ScoreParts
from locatron.schemas import (
    Admin1,
    Candidate,
    Country,
    Geo,
    GeoSource,
    Granularity,
    MatchMethod,
    at_least,
)

#: Segment delimiters. Applied to the raw string, because normalize() turns
#: punctuation into spaces and would erase the boundaries first.
_SEGMENT_SPLIT = re.compile(r"[,;|\n\r]+")

_POSTCODE = re.compile(r"^[0-9]{4}$")
_HAS_DIGIT = re.compile(r"[0-9]")

#: "United Kingdom of Great Britain and Northern Ireland" is eight tokens.
_MAX_COUNTRY_TOKENS = 8
#: "Australian Capital Territory" is three.
_MAX_STATE_TOKENS = 4

#: Fuzzy matching is a last resort, and a long token run is not a place name —
#: it is an address, which this resolver does not handle. Letting rapidfuzz
#: near-miss a 6-token street address onto a suburb produces exactly the kind of
#: confidently wrong answer the golden set exists to catch.
_FUZZY_MAX_TOKENS = 4


# ---------------------------------------------------------------------------
# evidence extraction
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Evidence:
    """What the input string states outright, separated from what it implies."""

    query: str
    norm: str
    country: CountryRow | None = None
    state: str | None = None
    postcode: str | None = None
    place_texts: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)

    @property
    def has_au_signal(self) -> bool:
        """Whether the input gives Australia-specific corroboration.

        A stated country is deliberately not enough. "Sydney Australia" names a
        country but still means the city, not a postcode-level suburb.
        """
        return bool(self.state or self.postcode)

    @property
    def implied_alpha3(self) -> str | None:
        """AUS when a state or postcode was found, since both are AU-only."""
        if self.country:
            return self.country.alpha3
        return "AUS" if self.has_au_signal else None


def _peel[T](
    toks: list[str], lookup: Callable[[str], T | None], *, max_len: int
) -> tuple[T | None, list[str]]:
    """Remove the best-matching n-gram from a token list.

    Preference order: a match that leaves tokens behind beats one that consumes
    everything, then the longest match, then the rightmost. Trailing position
    wins the final tiebreak because the broadest qualifier tends to come last —
    "Sydney Australia", not "Australia Sydney".
    """
    best: tuple[tuple[bool, int, int], T, int, int] | None = None
    for start, end, span in ngrams(toks, max(max_len, 1)):
        hit = lookup(span)
        if hit is None:
            continue
        key = (len(toks) - (end - start) > 0, end - start, start)
        if best is None or key > best[0]:
            best = (key, hit, start, end)

    if best is None:
        return None, toks
    _, hit, start, end = best
    return hit, toks[:start] + toks[end:]


def extract_evidence(raw: str, *, countries: CountryGazetteer, au: AuGazetteer) -> Evidence:
    """Split the input and peel off whatever it states explicitly."""
    ev = Evidence(query=raw, norm=normalize(raw))
    if not ev.norm:
        return ev

    places: list[str] = []
    # Later segments are the broadest, so peel the country from those first and
    # let it apply to the whole string.
    segments = [normalize(part) for part in _SEGMENT_SPLIT.split(raw or "")]
    segments = [seg for seg in segments if seg]

    for seg in reversed(segments):
        toks = tokens(seg)

        if ev.country is None:
            hit, toks = _peel(toks, countries.lookup_token, max_len=_MAX_COUNTRY_TOKENS)
            if hit is None and toks:
                # A bare ISO code counts only where a country abbreviation
                # actually goes: at the end. See countries.lookup_trailing_code.
                hit = countries.lookup_trailing_code(toks[-1])
                if hit is not None:
                    toks = toks[:-1]
            if hit is not None:
                ev.country = hit

        if ev.state is None:
            hit_state, toks = _peel(toks, au.state_token, max_len=_MAX_STATE_TOKENS)
            if hit_state is not None:
                ev.state = hit_state

        if ev.postcode is None:
            pc = next((t for t in toks if _POSTCODE.match(t)), None)
            if pc is not None:
                ev.postcode = pc
                toks = [t for t in toks if t != pc]

        remainder = " ".join(toks).strip()
        if remainder:
            places.append(remainder)

    # Back to input order: the leftmost segment is the most specific.
    ev.place_texts = tuple(reversed(places))
    return ev


# ---------------------------------------------------------------------------
# candidates
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Proposal:
    """One possible answer, with its score breakdown kept for debugging."""

    granularity: Granularity
    match_method: MatchMethod
    parts: ScoreParts
    label: str
    country: CountryRow | None = None
    admin1_name: str | None = None
    admin1_code: str | None = None
    locality: str | None = None
    postcode: str | None = None
    geo: Geo | None = None
    score: float = 0.0

    def finalise(self) -> Proposal:
        self.score = self.parts.total
        return self

    @property
    def alpha3(self) -> str | None:
        return self.country.alpha3 if self.country else None


def _city_geo(city: CityRow) -> Geo | None:
    if city.lat is None or city.lng is None:
        return None
    return Geo(lat=city.lat, lng=city.lng, source=GeoSource.CITY_POINT)


def _locality_geo(row: LocalityRow) -> Geo | None:
    if row.lat is not None and row.lng is not None:
        return Geo(lat=row.lat, lng=row.lng, source=GeoSource.LOCALITY_CENTROID)
    if row.postcode_lat is not None and row.postcode_lng is not None:
        return Geo(lat=row.postcode_lat, lng=row.postcode_lng, source=GeoSource.POSTCODE_CENTROID)
    return None


def _city_proposals(
    text: str,
    ev: Evidence,
    *,
    kind: str,
    fuzzy_ratio: float,
    cities: CityGazetteer,
    countries: CountryGazetteer,
    au: AuGazetteer,
    s: Settings,
) -> list[Proposal]:
    hits = cities.lookup(text)
    if ev.country:
        hits = tuple(c for c in hits if c.iso3 == ev.country.alpha3)
    if not hits:
        return []

    # Dominance is measured after filtering: once the input names a country,
    # candidates in other countries are not rivals and must not make the answer
    # look ambiguous.
    sizes = sorted((c.population for c in hits), reverse=True)
    runner_up = sizes[1] if len(sizes) > 1 else 0

    method = (
        MatchMethod.CITY_FUZZY
        if kind == MatchKind.FUZZY
        else MatchMethod.CITY_POPULATION_TIEBREAK
        if len(hits) > 1
        else MatchMethod.CITY_EXACT
    )

    out = []
    for city in hits:
        admin_code = au.code_for_state_name(city.admin_name) if city.iso3 == "AUS" else None
        parts = ScoreParts(base=scoring.base_score(kind, s, fuzzy_ratio=fuzzy_ratio))
        if ev.country and ev.country.alpha3 == city.iso3:
            parts.bonuses["country"] = s.score_explicit_country_bonus
        if ev.state and admin_code == ev.state:
            parts.bonuses["admin1"] = s.score_explicit_admin1_bonus
        rival = runner_up if city.population == sizes[0] else sizes[0]
        dom = scoring.dominance(city.population, rival)
        parts.penalties["ambiguous"] = scoring.ambiguity_penalty(dom, s)

        country = countries.get(city.iso3)
        out.append(
            Proposal(
                granularity=Granularity.CITY,
                match_method=method,
                parts=parts,
                label=(
                    f"{city.name}, {city.admin_name or ''} {city.iso3} (pop {city.population:,})"
                ).replace("  ", " "),
                country=country
                or CountryRow(alpha3=city.iso3, alpha2=None, name=city.country_name),
                admin1_name=city.admin_name,
                admin1_code=admin_code,
                geo=_city_geo(city),
            ).finalise()
        )
    return out


def _locality_proposals(
    text: str,
    ev: Evidence,
    *,
    kind: str,
    fuzzy_ratio: float,
    au: AuGazetteer,
    countries: CountryGazetteer,
    s: Settings,
) -> list[Proposal]:
    """AU locality candidates, by canonical name then through the alias table."""
    if ev.country and ev.country.alpha3 != "AUS":
        return []

    via_alias = False
    hits = au.lookup(text, state=ev.state)
    if not hits:
        hits = au.lookup_alias(text, state=ev.state)
        via_alias = bool(hits)
    if not hits:
        return []

    if ev.postcode:
        # A stated postcode is hard corroboration. Prefer the localities that
        # agree with it, but do not discard the rest — G-NAF and Australia Post
        # disagree on postcode boundaries often enough to matter.
        agreeing = tuple(r for r in hits if r.postcode == ev.postcode)
        if agreeing:
            hits = agreeing

    sizes = sorted((r.size for r in hits), reverse=True)
    runner_up = sizes[1] if len(sizes) > 1 else 0
    aus = countries.get("AUS")

    if kind == MatchKind.FUZZY:
        method = MatchMethod.LOCALITY_FUZZY
    elif via_alias:
        method = MatchMethod.LOCALITY_ALIAS
    else:
        method = MatchMethod.LOCALITY_EXACT

    out = []
    for row in hits:
        base_kind = MatchKind.ALIAS if via_alias and kind == MatchKind.EXACT else kind
        parts = ScoreParts(base=scoring.base_score(base_kind, s, fuzzy_ratio=fuzzy_ratio))
        if via_alias:
            parts.multiplier = au.alias_confidence(text, row.locality_id)
        if ev.country:
            parts.bonuses["country"] = s.score_explicit_country_bonus
        if ev.state and ev.state == row.state:
            parts.bonuses["admin1"] = s.score_explicit_admin1_bonus
        if ev.postcode and ev.postcode == row.postcode:
            parts.bonuses["postcode"] = s.score_postcode_bonus
        dom = scoring.dominance(row.size, runner_up if row.size == sizes[0] else sizes[0])
        parts.penalties["ambiguous"] = scoring.ambiguity_penalty(dom, s)
        if not ev.has_au_signal:
            parts.penalties["unqualified"] = s.score_locality_unqualified_penalty

        out.append(
            Proposal(
                granularity=Granularity.LOCALITY,
                match_method=MatchMethod.POSTAL_ONLY if row.is_postal_only else method,
                parts=parts,
                label=f"{row.locality} {row.state} {row.postcode} ({row.size:,} addr)",
                country=aus,
                admin1_name=au.state_name(row.state),
                admin1_code=row.state,
                locality=row.locality,
                postcode=row.postcode,
                geo=_locality_geo(row),
            ).finalise()
        )
    return out


def _place_proposals(
    ev: Evidence,
    *,
    cities: CityGazetteer,
    countries: CountryGazetteer,
    au: AuGazetteer,
    s: Settings,
    include_candidates: bool,
) -> list[Proposal]:
    """Exact and alias matches for every place text, raw then qualifier-stripped."""
    out: list[Proposal] = []

    for text in ev.place_texts:
        attempts = [(text, MatchKind.EXACT)]
        stripped = strip_qualifiers(text)
        if stripped and stripped != text:
            attempts.append((stripped, MatchKind.QUALIFIER_STRIPPED))

        for candidate_text, kind in attempts:
            city = _city_proposals(
                candidate_text,
                ev,
                kind=kind,
                fuzzy_ratio=0.0,
                cities=cities,
                countries=countries,
                au=au,
                s=s,
            )
            locality = _locality_proposals(
                candidate_text,
                ev,
                kind=kind,
                fuzzy_ratio=0.0,
                au=au,
                countries=countries,
                s=s,
            )

            # The structural rule. See the module docstring.
            if city and locality and not ev.has_au_signal:
                if include_candidates:
                    ev.warnings.append(
                        f"suppressed {len(locality)} AU localit"
                        f"{'y' if len(locality) == 1 else 'ies'} for {candidate_text!r}: "
                        "no postcode or state in the input to corroborate"
                    )
                locality = []

            out.extend(city)
            out.extend(locality)

        if out:
            # The first place text to match wins. Leading segments are the most
            # specific, and a later one is usually the qualifier, not the place.
            break

    return out


def _explains_every_token(query: str, key: str, min_token_sim: float) -> bool:
    """Whether every token of the input is accounted for by the matched name.

    Jaro-Winkler weights the start of a string, which is right for single-word
    place names and badly wrong for multi-word ones. "MELBOURNE FLORIDA" scores
    90.6 against "MELBOURNE" and 90.1 against "MELBOURNE AIRPORT" — both clear
    the threshold, and neither is the city in Florida. No whole-string score can
    separate those, because the shared prefix is doing all the work.

    Comparing token by token can. The rule is asymmetric on purpose:

        every token of the INPUT must have a close counterpart in the match
        the MATCH may carry tokens the input did not

    So "KU-RING-GAI" still reaches "KU-RING-GAI CHASE" — generalising to a
    longer canonical name is a reasonable answer — while "MELBOURNE FLORIDA" is
    rejected, because ignoring a token the caller wrote is not.
    """
    key_tokens = key.split(" ")
    return all(
        any(JaroWinkler.normalized_similarity(q, k) >= min_token_sim for k in key_tokens)
        for q in query.split(" ")
        if q
    )


def _fuzzy_proposals(
    ev: Evidence,
    *,
    cities: CityGazetteer,
    countries: CountryGazetteer,
    au: AuGazetteer,
    s: Settings,
) -> list[Proposal]:
    out: list[Proposal] = []
    for text in ev.place_texts:
        toks = tokens(text)
        if not toks or len(toks) > _FUZZY_MAX_TOKENS or _HAS_DIGIT.search(text):
            continue

        def close_enough(key: str, text: str = text) -> bool:
            return _explains_every_token(text, key, s.fuzzy_token_min / 100.0)

        for key, ratio in au.fuzzy(text, min_score=s.fuzzy_locality_min):
            if not close_enough(key):
                continue
            out.extend(
                _locality_proposals(
                    key,
                    ev,
                    kind=MatchKind.FUZZY,
                    fuzzy_ratio=ratio / 100.0,
                    au=au,
                    countries=countries,
                    s=s,
                )
            )
        for key, ratio in cities.fuzzy(text, min_score=s.fuzzy_city_min):
            if not close_enough(key):
                continue
            out.extend(
                _city_proposals(
                    key,
                    ev,
                    kind=MatchKind.FUZZY,
                    fuzzy_ratio=ratio / 100.0,
                    cities=cities,
                    countries=countries,
                    au=au,
                    s=s,
                )
            )
        if out:
            break
    return out


def _coarse_proposals(
    ev: Evidence, *, au: AuGazetteer, countries: CountryGazetteer, s: Settings
) -> list[Proposal]:
    """Postcode, state, and country answers, for when no place name matched.

    These are the "at least a country" floor from CLAUDE.md: an incomplete
    answer beats an exception, and beats nothing.

    They are only ever built when no place matched, because a place proposal
    already carries the country and admin1 these would report. Ranking them
    alongside a place match lets a bare ADMIN1 (0.74) outscore a corroborated
    fuzzy locality (0.62 x ratio) and throw away the more specific answer —
    which is what sent "Ku-ring-gai NSW" to admin1 instead of a locality.
    """
    out: list[Proposal] = []
    aus = countries.get("AUS")

    if ev.postcode:
        rows = au.lookup_postcode(ev.postcode)
        if ev.state:
            rows = tuple(r for r in rows if r.state == ev.state)
        if rows:
            top = rows[0]
            parts = ScoreParts(base=s.score_exact)
            if ev.state:
                parts.bonuses["admin1"] = s.score_explicit_admin1_bonus
            else:
                # A bare four-digit number is a weak signal on its own. It could
                # be a year, a street number, or an employee count.
                parts.penalties["unqualified"] = s.score_locality_unqualified_penalty
            out.append(
                Proposal(
                    granularity=Granularity.POSTCODE,
                    match_method=MatchMethod.POSTAL_ONLY
                    if top.is_postal_only
                    else MatchMethod.LOCALITY_EXACT,
                    parts=parts,
                    label=f"{ev.postcode} {top.state}",
                    country=aus,
                    admin1_name=au.state_name(top.state),
                    admin1_code=top.state,
                    postcode=ev.postcode,
                    geo=_locality_geo(top),
                ).finalise()
            )

    if ev.state:
        parts = ScoreParts(base=s.score_admin1_only)
        if ev.country:
            parts.bonuses["country"] = s.score_explicit_country_bonus
        out.append(
            Proposal(
                granularity=Granularity.ADMIN1,
                match_method=MatchMethod.STATE_BUCKET,
                parts=parts,
                label=f"{au.state_name(ev.state)} ({ev.state}) AUS",
                country=aus,
                admin1_name=au.state_name(ev.state),
                admin1_code=ev.state,
            ).finalise()
        )

    if ev.country:
        out.append(
            Proposal(
                granularity=Granularity.COUNTRY,
                match_method=MatchMethod.COUNTRY_BUCKET,
                parts=ScoreParts(base=s.score_country_only),
                label=ev.country.name,
                country=ev.country,
            ).finalise()
        )
    elif not out:
        # Last resort: a whole string that implies a country without naming one.
        # 'The land down under' and 'Greater Melbourne Area' are real
        # country_bucket values. Whole strings only — see countries.lookup_hint.
        for text in (ev.norm, *ev.place_texts):
            hint = countries.lookup_hint(text)
            if hint is not None:
                out.append(
                    Proposal(
                        granularity=Granularity.COUNTRY,
                        match_method=MatchMethod.COUNTRY_BUCKET,
                        parts=ScoreParts(base=s.score_country_implied),
                        label=hint.name,
                        country=hint,
                    ).finalise()
                )
                break

    return out


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def _rank(
    proposals: list[Proposal], ev: Evidence, bias_alpha3: str | None, s: Settings
) -> list[Proposal]:
    """Sort best first, applying country bias only as a near-equal tiebreak.

    The bias is never applied when the input stated a country: config must not
    override what the caller actually wrote. See CLAUDE.md.
    """
    if bias_alpha3 and not ev.country:
        for p in proposals:
            p.score = scoring.apply_country_bias(p.score, p.alpha3, bias_alpha3, s)

    # Bonuses can sum past 1.0 on a well-qualified locality. Saturating at 1.0
    # would report a gazetteer lookup as certainty, so cap below it.
    for p in proposals:
        p.score = min(p.score, s.score_max)

    # Granularity breaks exact score ties toward the more specific answer, then
    # the label, so the result is stable across dict orderings.
    order = list(Granularity)
    return sorted(proposals, key=lambda p: (-p.score, order.index(p.granularity), p.label))


def resolve_place(
    raw: str,
    *,
    country_bias: str | None = None,
    min_granularity: Granularity | None = None,
    include_candidates: bool = False,
) -> tuple[Proposal | None, list[Candidate], list[str], str]:
    """Resolve a loose place string.

    Returns (winner, candidates, warnings, normalized). A `None` winner means
    unresolvable, which is a normal outcome, not an error — callers turn it into
    granularity=UNRESOLVED with confidence 0.
    """
    s = get_settings()
    countries = load_countries()
    cities = load_cities()
    au = load_au()

    ev = extract_evidence(raw, countries=countries, au=au)
    if not ev.norm:
        return None, [], [], ev.norm

    bias = countries.resolve_bias(country_bias if country_bias is not None else s.country_bias)

    proposals = _place_proposals(
        ev, cities=cities, countries=countries, au=au, s=s, include_candidates=include_candidates
    )

    satisfied = bool(proposals) and (
        min_granularity is None or any(at_least(p.granularity, min_granularity) for p in proposals)
    )
    if not satisfied:
        proposals += _fuzzy_proposals(ev, cities=cities, countries=countries, au=au, s=s)

    if not proposals:
        proposals = _coarse_proposals(ev, au=au, countries=countries, s=s)
    if not proposals:
        return None, [], ev.warnings, ev.norm

    ranked = _rank(proposals, ev, bias, s)
    winner = ranked[0]

    # Runner-ups are emitted on request, and always when the answer is shaky:
    # a low-confidence winner with no alternatives shown is the hardest kind of
    # result to debug.
    show = include_candidates or winner.score < s.low_confidence_threshold
    candidates = (
        scoring.to_candidates(
            [
                (
                    p.label,
                    p.score,
                    p.granularity,
                    p.alpha3,
                    p.admin1_code or p.admin1_name,
                    p.locality,
                    p.parts.reason,
                )
                for p in ranked[1:]
            ],
            winner_score=winner.score,
            s=s,
            apply_margin=not include_candidates,
        )
        if show
        else []
    )

    warnings = list(ev.warnings)
    if winner.score < s.low_confidence_threshold:
        warnings.append(f"low confidence {winner.score:.2f}: {winner.parts.reason}")

    return winner, candidates, warnings, ev.norm


def to_country_schema(row: CountryRow | None) -> Country | None:
    return row.to_schema() if row else None


def to_admin1_schema(p: Proposal) -> Admin1 | None:
    if not p.admin1_name and not p.admin1_code:
        return None
    return Admin1(name=p.admin1_name or p.admin1_code or "", code=p.admin1_code)
