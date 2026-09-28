"""Australian locality, alias, and state lookup.

Three indexes come out of `aus_state_bucket`, and keeping them apart is the
whole point of this module.

CLAUDE.md describes the table as "string variants -> AU state", which is true
but hides a trap. Only 17,513 of its 65,614 rows are the documented
"<STATE> <LOCALITY>" shape. Many more are "<LOCALITY> <STATE>", and the largest
block is bare locality names with no state token at all:

    value='GREEN LAKE'  -> VIC          value='Toronto'   -> NSW
    value='BOTOBOLAR'   -> NSW          value='York'      -> WA
                                        value='Newcastle' -> NSW

So a hit on this table is NOT evidence that the input named a state. Treating
it that way sends "Toronto" to New South Wales and "York" to Western Australia.
The split:

`state_tokens`  23 entries, derived rather than hardcoded: rows whose value,
                once normalised and stripped of spaces, equals the state code,
                plus rows matching one of the eight full state names that
                `Cities.admin_name` supplies for iso3='AUS'. Yields VIC,
                VICTORIA, A.C.T, AUSTRALIAN CAPITAL TERRITORY and so on. Only
                these count as "the input stated a state".

`state_hints`   all 65k. Weak evidence: picks a state for a locality already
                matched by name, and says "this string is probably Australian".
                Never promotes a result to admin1 on its own.

Two normalised keys do map to two states, both of them upstream data errors
that only surface after normalisation folds the punctuation away:

    'N.S.W.'         -> NSW     'HAYMARKET NSW'  -> NSW
    'N.S.W'          -> VIC     'HAYMARKET, NSW' -> VIC

Neither is resolvable by scan order without the answer depending on MyISAM
returning rows in insertion order. `_resolve_state_votes` settles them
deterministically instead.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from rapidfuzz import process
from rapidfuzz.distance import JaroWinkler

from locatron.gazetteer.loader import as_float, cached_gazetteer, rows
from locatron.normalize import normalize

#: External territories. Present in locatron_locality, absent from
#: aus_state_bucket, and excluded from Australian bounding-box checks.
OT = "OT"


@dataclass(frozen=True, slots=True)
class LocalityRow:
    locality_id: int
    locality: str
    state: str
    postcode: str
    address_count: int
    street_count: int
    is_postal_only: bool
    in_gnaf: bool
    lat: float | None
    lng: float | None
    postcode_lat: float | None
    postcode_lng: float | None

    @property
    def size(self) -> int:
        """Tiebreak weight, the AU analogue of a city's population.

        Postal-only localities have no G-NAF addresses at all, so their count is
        zero. Give them a nominal 1 so a real PO Box locality still outranks
        nothing, without ever outranking a populated suburb.
        """
        return self.address_count or (1 if self.is_postal_only else 0)


@dataclass(frozen=True, slots=True)
class AliasRow:
    alias_display: str
    locality_id: int
    alias_type: str
    confidence: float
    """Score multiplier from the alias table. Keeps a low-trust auspost_variant
    (0.80) from outranking an exact canonical hit."""


@dataclass(frozen=True, slots=True)
class AuGazetteer:
    by_norm: dict[str, tuple[LocalityRow, ...]]
    by_id: dict[int, LocalityRow]
    by_postcode: dict[str, tuple[LocalityRow, ...]]
    aliases: dict[str, tuple[AliasRow, ...]]
    state_tokens: dict[str, str]
    state_hints: dict[str, str]
    state_display: dict[str, str]
    _keys: tuple[str, ...]

    # --- localities ----------------------------------------------------------

    def lookup(self, norm: str, *, state: str | None = None) -> tuple[LocalityRow, ...]:
        hits = self.by_norm.get(norm, ())
        if state:
            hits = tuple(r for r in hits if r.state == state)
        return hits

    def lookup_alias(self, norm: str, *, state: str | None = None) -> tuple[LocalityRow, ...]:
        """Localities reachable from `norm` through the alias table."""
        out = []
        for alias in self.aliases.get(norm, ()):
            row = self.by_id.get(alias.locality_id)
            if row and (state is None or row.state == state):
                out.append(row)
        return tuple(sorted(out, key=lambda r: (-r.size, r.state, r.postcode)))

    def alias_confidence(self, norm: str, locality_id: int) -> float:
        """The best confidence multiplier among aliases pointing at a locality."""
        scores = [a.confidence for a in self.aliases.get(norm, ()) if a.locality_id == locality_id]
        return max(scores) if scores else 1.0

    def lookup_postcode(self, postcode: str) -> tuple[LocalityRow, ...]:
        return self.by_postcode.get(postcode, ())

    def fuzzy(self, norm: str, *, min_score: int, limit: int = 5) -> list[tuple[str, float]]:
        if not norm:
            return []
        hits = process.extract(
            norm,
            self._keys,
            scorer=JaroWinkler.normalized_similarity,
            limit=limit,
            score_cutoff=min_score / 100.0,
        )
        return [(key, score * 100.0) for key, score, _ in hits]

    # --- states --------------------------------------------------------------

    def state_token(self, norm: str) -> str | None:
        """State code, but only for a string that really is a state token.

        This is the one that may override other evidence.
        """
        return self.state_tokens.get(norm)

    def state_hint(self, norm: str) -> str | None:
        """State code from the wider bucket. Weak evidence only."""
        return self.state_hints.get(norm)

    def state_name(self, code: str | None) -> str | None:
        """Display name for a state code, e.g. VIC -> 'Victoria'."""
        if not code:
            return None
        return self.state_display.get(code, code)

    def code_for_state_name(self, name: str | None) -> str | None:
        """'Victoria' -> 'VIC'. Fills Admin1.code for Australian cities.

        Cities.admin_name holds the full state name, but the golden set and
        every Australian consumer expect the code.
        """
        if not name:
            return None
        return self.state_tokens.get(normalize(name))


_ALIAS_SQL = """
SELECT alias_norm_key, alias_display, locality_id, alias_type, confidence
FROM locatron_locality_alias
WHERE alias_norm_key IS NOT NULL AND alias_norm_key <> ''
"""

_LOCALITY_SQL = """
SELECT locality_id, norm_key, locality, state, postcode,
       lat, lng, postcode_lat, postcode_lng,
       address_count, street_count, is_postal_only, in_gnaf
FROM locatron_locality
WHERE norm_key IS NOT NULL AND norm_key <> ''
"""


@cached_gazetteer
def load_au() -> AuGazetteer:
    by_id: dict[int, LocalityRow] = {}
    grouped: dict[str, list[LocalityRow]] = {}
    by_postcode: dict[str, list[LocalityRow]] = {}

    for r in rows(_LOCALITY_SQL):
        row = LocalityRow(
            locality_id=int(r["locality_id"]),
            locality=(r["locality"] or "").strip(),
            state=(r["state"] or "").strip().upper(),
            # Defensive LPAD. The build script pads, but NT is 0800-0899 and a
            # careless reload anywhere upstream drops the leading zero.
            postcode=(r["postcode"] or "").strip().rjust(4, "0"),
            address_count=int(r["address_count"] or 0),
            street_count=int(r["street_count"] or 0),
            is_postal_only=bool(r["is_postal_only"]),
            in_gnaf=bool(r["in_gnaf"]),
            lat=as_float(r["lat"]),
            lng=as_float(r["lng"]),
            postcode_lat=as_float(r["postcode_lat"]),
            postcode_lng=as_float(r["postcode_lng"]),
        )
        by_id[row.locality_id] = row
        grouped.setdefault(r["norm_key"], []).append(row)
        by_postcode.setdefault(row.postcode, []).append(row)

    aliases: dict[str, list[AliasRow]] = {}
    for r in rows(_ALIAS_SQL):
        aliases.setdefault(r["alias_norm_key"], []).append(
            AliasRow(
                alias_display=(r["alias_display"] or "").strip(),
                locality_id=int(r["locality_id"]),
                alias_type=str(r["alias_type"]),
                confidence=float(r["confidence"] or 1.0),
            )
        )

    state_tokens, state_hints, state_display = _load_states()

    # Biggest first, so a caller taking hits[0] gets the most likely locality.
    by_norm = {
        k: tuple(sorted(v, key=lambda r: (-r.size, r.state, r.postcode)))
        for k, v in grouped.items()
    }
    keys = tuple(set(by_norm) | set(aliases))

    return AuGazetteer(
        by_norm=by_norm,
        by_id=by_id,
        by_postcode={
            k: tuple(sorted(v, key=lambda r: (-r.size, r.state, r.locality)))
            for k, v in by_postcode.items()
        },
        aliases={k: tuple(v) for k, v in aliases.items()},
        state_tokens=state_tokens,
        state_hints=state_hints,
        state_display=state_display,
        _keys=keys,
    )


def _load_states() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Split aus_state_bucket into real state tokens and weak hints.

    The set of full state names is read from Cities.admin_name for iso3='AUS'
    rather than hardcoded, so it stays correct if upstream renames one.
    """
    full_names: dict[str, str] = {}
    for r in rows("SELECT DISTINCT admin_name FROM Cities WHERE iso3 = 'AUS'"):
        name = (r["admin_name"] or "").strip()
        key = normalize(name)
        if key:
            full_names[key] = name

    state_tokens: dict[str, str] = {}
    state_display: dict[str, str] = {}
    votes: dict[str, Counter[str]] = {}

    for r in rows("SELECT state, value FROM aus_state_bucket"):
        code = (r["state"] or "").strip().upper()
        key = normalize(r["value"])
        if not code or not key:
            continue

        votes.setdefault(key, Counter())[code] += 1

        # A token is a state and nothing else: the bare code however it was
        # punctuated ('ACT', 'A.C.T', 'A C T' all squash to 'ACT'), or one of
        # the eight full names. This test is what makes state_tokens immune to
        # the 'N.S.W' -> VIC error: the mislabelled row fails it, because its
        # squashed key does not equal its own state column.
        if key.replace(" ", "") == code or key in full_names:
            state_tokens[key] = code
            if key in full_names:
                state_display.setdefault(code, full_names[key])

    state_hints = _resolve_state_votes(votes, state_tokens)
    return state_tokens, state_hints, state_display


def _resolve_state_votes(
    votes: dict[str, Counter[str]], state_tokens: dict[str, str]
) -> dict[str, str]:
    """Collapse per-key state votes to one state, without relying on row order.

    Three tiebreaks, in order:

    1. A state token inside the key itself wins. 'HAYMARKET NSW' names its
       state, so the row labelling it VIC loses regardless of how many rows
       agree with it. This is the only tiebreak that fixes bad data rather than
       just picking consistently.
    2. Otherwise the majority of rows. 'N S W' has two NSW rows to one VIC.
    3. Otherwise the alphabetically first code, purely so the result is stable.
    """
    out: dict[str, str] = {}
    for key, counter in votes.items():
        if len(counter) == 1:
            out[key] = next(iter(counter))
            continue

        parts = key.split(" ")
        from_key = next(
            (
                state_tokens[tok]
                for tok in (parts[-1], parts[0])
                if tok in state_tokens and state_tokens[tok] in counter
            ),
            None,
        )
        if from_key:
            out[key] = from_key
            continue

        out[key] = min(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    return out
