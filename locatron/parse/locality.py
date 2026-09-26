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
from locatron.parse.scoring import (
    DEFAULT_WEIGHTS,
    Weights,
    confidence,
    score_candidate,
)
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


# ---------------------------------------------------------------------------
# hypotheses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Hypothesis:
    """One reading of the input's locality, with everything behind the score."""

    candidate: Candidate
    locality_span: Span
    score: float
    signals: dict[str, float]
    confidence: float
    state_span: Span | None = None
    postcode_span: Span | None = None

    @property
    def consumed(self) -> tuple[Span, ...]:
        """Spans this reading accounts for, so the street stage knows what is
        left. The locality, the state token and the postcode token it agreed
        with -- never a token some other hypothesis claimed."""
        spans = [self.locality_span]
        if self.state_span is not None:
            spans.append(self.state_span)
        if self.postcode_span is not None:
            spans.append(self.postcode_span)
        return tuple(sorted(spans))

    def remaining(self, ts: TokenStream, also: Sequence[Span] = ()) -> tuple[Span, ...]:
        """Contiguous runs left for the street stage, given this reading."""
        return ts.runs([*self.consumed, *also])


def generate_hypotheses(
    ts: TokenStream,
    au: AuGazetteer,
    *,
    consumed: Sequence[Span] = (),
    postcodes: Sequence[tuple[str, Span]] = (),
    po_box_found: bool = False,
    fuzzy_min: int | None = None,
    weights: Weights = DEFAULT_WEIGHTS,
) -> tuple[Hypothesis, ...]:
    """Every locality reading of `ts`, ranked best first.

    `consumed` are spans Prompt A's extractors claimed -- street number, unit,
    PO box -- so no locality key is built across them. `postcodes` are the
    postcode candidates with their spans, which is how a candidate's own
    postcode gets corroborated.

    Fuzzy matching is only attempted when nothing matched exactly, and only
    against the gazetteer. Ranked below every exact and alias hit by
    construction, since BASE_FUZZY_MAX sits under BASE_ALIAS.
    """
    postcode_values = {pc for pc, _ in postcodes}
    postcode_span_of = {pc: span for pc, span in postcodes}
    states = find_state_tokens(ts, au, consumed)
    stated = next((s for s in states if s.strong), None)
    hinted = next((s for s in states if not s.strong), None)

    grams = ngram_spans(ts, consumed)
    found: list[tuple[Candidate, Span]] = []
    for gram in grams:
        for cand in candidates_for_key(au, gram.key):
            found.append((cand, gram.span))

    # The postcode path: every locality sharing a postcode token, carrying an
    # empty locality span because it consumed no name.
    #
    # Suppressed only when a named candidate already agrees with the postcode.
    # That is the case where the path adds nothing but noise: on
    # 'Hamilton Crescent Ryde NSW 2112', RYDE itself sits in 2112, so PUTNEY and
    # DENISTONE EAST would arrive as near-ties for it while leaving RYDE's own
    # token unconsumed. Corroborating a named locality is POSTCODE_AGREE's job.
    #
    # When no named candidate agrees, the path is the only thing that can find
    # the right answer, and it runs even though names were matched. On
    # '12 Clifton Street 3201' the named candidates are CLIFTON localities in
    # other postcodes -- CLIFTON is a street here, not the suburb -- and
    # CARRUM DOWNS is reachable only through 3201. Scoring then ranks them:
    # an agreeing postcode outweighs a name whose postcode contradicts it.
    named_postcodes = {c.postcode for c, _ in found}
    if not (postcode_values & named_postcodes):
        for pc, _span in postcodes:
            for cand in candidates_for_postcode(au, pc):
                found.append((cand, Span(0, 0)))

    if not found and fuzzy_min is not None:
        for gram in grams:
            for cand in fuzzy_candidates_for_key(au, gram.key, min_score=fuzzy_min):
                found.append((cand, gram.span))

    # A postcode token nothing explains lowers confidence in the whole parse.
    explained = {c.postcode for c, _ in found}
    unexplained = bool(postcode_values) and not (postcode_values & explained)

    hyps: list[Hypothesis] = []
    for cand, span in found:
        agrees = cand.postcode in postcode_values
        sig = score_candidate(
            match=cand.match,
            fuzzy_ratio=cand.fuzzy_ratio,
            ngram_tokens=len(span),
            address_count=cand.address_count,
            is_postal_only=cand.is_postal_only,
            alias_confidence=cand.alias_confidence,
            postcode_agrees=agrees,
            postcode_token_present=bool(postcode_values),
            postcode_unexplained=unexplained,
            stated_state=stated.code if stated else None,
            hinted_state=hinted.code if hinted else None,
            candidate_state=cand.state,
            po_box_found=po_box_found,
            w=weights,
        )
        hyps.append(
            Hypothesis(
                candidate=cand,
                locality_span=span,
                score=sig.score,
                signals=sig.parts,
                confidence=0.0,
                state_span=stated.span if stated and stated.code == cand.state else None,
                postcode_span=postcode_span_of.get(cand.postcode) if agrees else None,
            )
        )

    return _rank_hypotheses(hyps, weights)


def _rank_hypotheses(hyps: list[Hypothesis], w: Weights) -> tuple[Hypothesis, ...]:
    """Sort best first and fill in confidence from the margin to the runner-up.

    The tiebreaks after score exist so two runs never disagree: a bigger
    locality, then a longer matched name, then state and postcode.
    """
    if not hyps:
        return ()

    ordered = sorted(
        hyps,
        key=lambda h: (
            -h.score,
            -h.candidate.address_count,
            -len(h.locality_span),
            h.candidate.state,
            h.candidate.postcode,
        ),
    )

    # One hypothesis per locality, keeping the best-supported reading. A place
    # is routinely reached twice -- named by an n-gram and again through its
    # postcode -- and leaving both in makes a locality its own runner-up, which
    # reads as a near-tie and quietly halves the confidence.
    seen: set[int] = set()
    unique: list[Hypothesis] = []
    for h in ordered:
        if h.candidate.locality_id in seen:
            continue
        seen.add(h.candidate.locality_id)
        unique.append(h)
    ordered = unique

    runner_up = ordered[1].score if len(ordered) > 1 else None
    return tuple(
        Hypothesis(
            candidate=h.candidate,
            locality_span=h.locality_span,
            score=h.score,
            signals=h.signals,
            confidence=confidence(h.score, runner_up if i == 0 else None, w),
            state_span=h.state_span,
            postcode_span=h.postcode_span,
        )
        for i, h in enumerate(ordered)
    )
