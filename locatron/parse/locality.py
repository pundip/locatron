"""Locality and state hypotheses from token n-grams.

Given the tokens Prompt A's extractors did not consume, this module asks the
in-memory AU gazetteer which localities each contiguous n-gram could name, and
returns every answer. It never picks one: choosing needs the postcode, the state
token and the ambiguity prior weighed together, which is `parse/scoring.py`.

Read-only against the gazetteer, and the gazetteer only. `address_ref` is never
touched here -- fuzzy matching happens at gazetteer level, exact matching at
address level, and 15.9M rows are only ever hit with an index-backed exact
lookup in a later stage. See CLAUDE.md.

Aliases come through the loader that is already in place: `AuGazetteer.aliases`
holds all 61,155 rows of `locatron_locality_alias` under 20,084 normalised keys,
`lookup_alias()` resolves a key to its principal `LocalityRow`, and
`alias_confidence()` gives the trust multiplier from the table. So an alias hit
is a first-class path here, scoring just below a direct hit.
"""

from __future__ import annotations

from dataclasses import dataclass

from locatron.gazetteer.au import AuGazetteer, LocalityRow
from locatron.resolve.scoring import MatchKind


@dataclass(frozen=True, slots=True)
class Candidate:
    """One locality the gazetteer offers for a key, with how it was reached.

    Wraps the gazetteer's own `LocalityRow` rather than copying its fields, so
    there is no second definition of what a locality is to drift from the first.
    """

    row: LocalityRow
    key: str
    """The normalised key that matched. For an alias hit this is the alias, not
    the principal's name, which is what makes a hit explainable after the fact."""
    match: str
    """`MatchKind.EXACT`, `.ALIAS` or `.FUZZY`, reusing the resolver's vocabulary."""
    alias_confidence: float = 1.0
    """From `locatron_locality_alias.confidence`. 1.0 for a direct hit."""
    fuzzy_ratio: float = 0.0
    """0.0-1.0 similarity for a fuzzy hit, 0.0 otherwise."""

    # --- passthroughs, so callers need not reach through .row everywhere -----

    @property
    def locality_id(self) -> int:
        return self.row.locality_id

    @property
    def locality(self) -> str:
        return self.row.locality

    @property
    def state(self) -> str:
        return self.row.state

    @property
    def postcode(self) -> str:
        """Zero-padded four-character string, never an int. NT is 0800-0899."""
        return self.row.postcode

    @property
    def address_count(self) -> int:
        return self.row.address_count

    @property
    def is_postal_only(self) -> bool:
        return self.row.is_postal_only


def _exact(au: AuGazetteer, key: str, state: str | None) -> list[Candidate]:
    return [
        Candidate(row=row, key=key, match=MatchKind.EXACT) for row in au.lookup(key, state=state)
    ]


def _alias(au: AuGazetteer, key: str, state: str | None) -> list[Candidate]:
    return [
        Candidate(
            row=row,
            key=key,
            match=MatchKind.ALIAS,
            alias_confidence=au.alias_confidence(key, row.locality_id),
        )
        for row in au.lookup_alias(key, state=state)
    ]


def _dedupe(cands: list[Candidate]) -> tuple[Candidate, ...]:
    """One candidate per locality, keeping the strongest way it was reached.

    A locality is often reachable both directly and through an alias -- 2,595
    alias keys are also locality keys -- and counting it twice would make it
    look like two pieces of evidence for the same place.
    """
    order = {MatchKind.EXACT: 0, MatchKind.ALIAS: 1, MatchKind.FUZZY: 2}
    best: dict[int, Candidate] = {}
    for c in cands:
        prev = best.get(c.locality_id)
        if prev is None or order[c.match] < order[prev.match]:
            best[c.locality_id] = c
        elif order[c.match] == order[prev.match] and c.fuzzy_ratio > prev.fuzzy_ratio:
            best[c.locality_id] = c
    return tuple(best.values())


def _ranked(cands: tuple[Candidate, ...]) -> tuple[Candidate, ...]:
    """Deterministic order: biggest first, then state and postcode.

    This is presentation only, not selection -- every candidate is still here.
    Scoring reorders them properly; this just means two runs never disagree.
    """
    return tuple(sorted(cands, key=lambda c: (-c.row.size, c.state, c.postcode, c.locality_id)))


def candidates_for_key(
    au: AuGazetteer, key: str, *, state: str | None = None
) -> tuple[Candidate, ...]:
    """Every locality `key` could name, direct hits and alias hits together.

    Returns all of them. 'RICHMOND' is a real place in five states and
    'CARRUM DOWNS' in one; the difference is the scorer's to express, not this
    function's to hide.
    """
    if not key:
        return ()
    return _ranked(_dedupe(_exact(au, key, state) + _alias(au, key, state)))


def candidates_for_postcode(au: AuGazetteer, postcode: str) -> tuple[Candidate, ...]:
    """Every locality sharing a postcode, for a bare-postcode input like '3201'.

    The postcode is the key here, so `Candidate.key` carries it and the match is
    exact: a postcode is not a name that could have been misspelled.
    """
    if not postcode:
        return ()
    return _ranked(
        _dedupe(
            [
                Candidate(row=row, key=postcode, match=MatchKind.EXACT)
                for row in au.lookup_postcode(postcode)
            ]
        )
    )


def fuzzy_candidates_for_key(
    au: AuGazetteer, key: str, *, min_score: int, state: str | None = None
) -> tuple[Candidate, ...]:
    """Near-miss localities for `key`, against the gazetteer only.

    `AuGazetteer.fuzzy` searches the union of locality and alias keys, so a
    fuzzy hit still has to be resolved through the exact and alias paths to
    reach a row. Every result carries its similarity, and the scorer keeps all
    of them below any exact hit.
    """
    if not key:
        return ()
    out: list[Candidate] = []
    for hit_key, score in au.fuzzy(key, min_score=min_score):
        if hit_key == key:
            continue  # an exact hit is not a fuzzy one; the caller has it already
        for c in _dedupe(_exact(au, hit_key, state) + _alias(au, hit_key, state)):
            out.append(
                Candidate(
                    row=c.row,
                    key=hit_key,
                    match=MatchKind.FUZZY,
                    alias_confidence=c.alias_confidence,
                    fuzzy_ratio=score / 100.0,
                )
            )
    return _ranked(_dedupe(out))
