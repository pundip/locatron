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

from collections.abc import Sequence
from dataclasses import dataclass

from locatron.gazetteer.au import AuGazetteer, LocalityRow
from locatron.normalize import ngrams
from locatron.parse.tokens import Span, TokenStream
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


#: How strongly each path counts as evidence. Exact beats alias beats fuzzy.
_MATCH_ORDER = {MatchKind.EXACT: 0, MatchKind.ALIAS: 1, MatchKind.FUZZY: 2}


def _strength(c: Candidate) -> tuple[int, float]:
    """Sort key for "reached the better way". Lower wins.

    Negated fuzzy_ratio so that within one path the closer match sorts first.
    """
    return (_MATCH_ORDER[c.match], -c.fuzzy_ratio)


def _dedupe(cands: list[Candidate]) -> tuple[Candidate, ...]:
    """One candidate per locality, keeping the strongest way it was reached.

    A locality is often reachable both directly and through an alias -- 2,595
    alias keys are also locality keys -- and counting it twice would make it
    look like two pieces of evidence for the same place.
    """
    best: dict[int, Candidate] = {}
    for c in cands:
        prev = best.get(c.locality_id)
        if prev is None or _strength(c) < _strength(prev):
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


# ---------------------------------------------------------------------------
# n-gram generation and consumed-span bookkeeping
# ---------------------------------------------------------------------------

#: Longest locality name worth trying. 'ST KILDA EAST' is three tokens and
#: 'CHARLES DARWIN UNIVERSITY' is three; four covers the tail without turning
#: every input into a combinatorial sweep.
MAX_NGRAM_TOKENS = 4


@dataclass(frozen=True, slots=True)
class NGram:
    """A contiguous run of unconsumed tokens, and the key it forms."""

    span: Span
    key: str

    def __len__(self) -> int:
        return len(self.span)


def ngram_spans(
    ts: TokenStream,
    consumed: Sequence[Span] = (),
    *,
    max_len: int = MAX_NGRAM_TOKENS,
) -> tuple[NGram, ...]:
    """Every contiguous n-gram of the unconsumed tokens, longest first.

    Generated within each unconsumed run rather than across the whole stream, so
    a span never straddles a token some extractor already claimed. In
    '65 CLIFTON PARK DRIVE 3201 CARRUM DOWNS' with the number and postcode
    claimed, that means 'DRIVE CARRUM' is never offered as a locality key --
    the postcode between them ends the run.

    Longest first because a longer name is better evidence: 'CARRUM DOWNS' must
    be tried before 'CARRUM', and 'ST KILDA EAST' before 'ST KILDA'.

    >>> from locatron.parse.tokens import tokenize
    >>> ts = tokenize("Carrum Downs VIC")
    >>> [(g.key, g.span.start, g.span.end) for g in ngram_spans(ts)][:3]
    [('CARRUM DOWNS VIC', 0, 3), ('CARRUM DOWNS', 0, 2), ('DOWNS VIC', 1, 3)]
    """
    out: list[NGram] = []
    for run in ts.runs(list(consumed)):
        texts = [t.text for t in ts.slice(run)]
        # normalize.ngrams() already does this enumeration; reusing it keeps the
        # n-gram definition in one place even though the offsets need shifting.
        for start, end, joined in ngrams(texts, max_len=max_len):
            out.append(NGram(span=Span(run.start + start, run.start + end), key=joined))

    # Longest first, then left to right, so the order is total and stable.
    return tuple(sorted(out, key=lambda g: (-len(g.span), g.span.start)))


@dataclass(frozen=True, slots=True)
class StateToken:
    """A token run that names a state, cross-checked against aus_state_bucket."""

    code: str
    """The AU state code: VIC, NSW, QLD, ...."""
    span: Span
    key: str
    strong: bool
    """True when the gazetteer's `state_tokens` recognised it, meaning the input
    really did name a state. False for a `state_hints` match, which is weak
    evidence only -- the bucket maps bare locality names to states too, so
    'Toronto' hits it and does not mean New South Wales was stated. See the
    module docstring on gazetteer/au.py."""


def find_state_tokens(
    ts: TokenStream, au: AuGazetteer, consumed: Sequence[Span] = ()
) -> tuple[StateToken, ...]:
    """Every run of unconsumed tokens that names a state, longest first.

    Goes through the existing loader's `state_token()` and `state_hint()`, which
    are built from `aus_state_bucket` at load time. No new query path, and no
    hardcoded list of state names -- the eight full names come from
    Cities.admin_name for iso3='AUS' so they stay correct if upstream renames
    one.

    Only `strong` results should be allowed to contradict a candidate. A hint is
    the bucket saying "this string is probably Australian", not "the input
    stated a state".
    """
    out: list[StateToken] = []
    for gram in ngram_spans(ts, consumed):
        code = au.state_token(gram.key)
        if code:
            out.append(StateToken(code=code, span=gram.span, key=gram.key, strong=True))
            continue
        hint = au.state_hint(gram.key)
        if hint:
            out.append(StateToken(code=hint, span=gram.span, key=gram.key, strong=False))
    return tuple(out)
