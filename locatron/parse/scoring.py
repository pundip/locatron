"""Hypothesis scoring for locality and state. Pure, no database.

Every weight is a named constant below with a line saying why it has the value
it does, and the logic contains no bare numbers. They are also gathered into a
`Weights` dataclass that defaults to those constants, because CLAUDE.md says
scoring weights belong in config so they can be tuned without a redeploy and
`locatron/resolve/scoring.py` already takes a `Settings` for exactly that. The
constants satisfy the first requirement, the dataclass keeps the second one step
away: building a `Weights` from `Settings` needs no change to any function here.

The model is additive, so a breakdown is a dict that sums to the score and can
be read line by line. Alias trust is the one thing that is naturally a
multiplier, and it is expressed as a deduction from parity to keep the sum
honest.

Ambiguity is measured against the runner-up, not by counting candidates. Six
Richmonds where the first is six times the second are not a coin flip; two where
they are level are. Counting rows cannot tell those apart.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from locatron.resolve.scoring import MatchKind

# --- base scores, by how the gazetteer name was reached ---------------------

#: An exact canonical hit. Everything else is defined relative to this.
BASE_EXACT = 1.00

#: An alias hit, deliberately just below exact: 'NSW WORLD SQUARE' reaching
#: WORLD SQUARE is real evidence, but the principal's own name is better.
BASE_ALIAS = 0.92

#: Ceiling for a fuzzy hit, scaled by similarity so a 0.98 near-miss is not
#: treated like one that barely cleared the threshold. Below BASE_ALIAS at every
#: similarity, so no fuzzy match can ever outrank an exact or alias one.
BASE_FUZZY_MAX = 0.80

# --- postcode ---------------------------------------------------------------

#: The strongest signal in the model. A locality and a postcode that agree are
#: two independent statements naming one row, which is what makes
#: '65 clifton park drive 3201 carrum downs' parseable without word order.
POSTCODE_AGREE = 0.90

#: Evidence against, not a rejection: a scrape pairs a correct locality with a
#: stale postcode often enough that discarding the candidate loses more than it
#: saves. Large enough that agreement beats the ambiguity prior outright, which
#: is what makes 'Perth 7300' resolve to Tasmania over the much bigger WA one.
POSTCODE_DISAGREE = -0.25

#: A postcode token that no candidate anywhere explains. Uniform across every
#: hypothesis, so it lowers confidence in the parse without reordering it.
POSTCODE_UNEXPLAINED = -0.10

# --- state ------------------------------------------------------------------

#: A state the input actually stated, agreeing with the candidate.
STATE_AGREE = 0.55

#: A stated state that disagrees. Close to disqualifying on purpose: nobody
#: writes 'Richmond VIC' meaning the Tasmanian one. Still a score and not a
#: filter, so a candidate survives to be inspected and explained.
STATE_DISAGREE = -1.20

#: A hit in the wider aus_state_bucket, which is not a stated state. Only 23 of
#: its 65,614 rows are real state tokens; the rest map bare locality names, so
#: 'Toronto' resolves to NSW there and says nothing about what was written.
#: Small enough to break a tie and never to decide one.
STATE_HINT_AGREE = 0.05

# --- priors and shape -------------------------------------------------------

#: The ambiguity prior, over log(address_count) -- the AU analogue of the
#: population tiebreak that makes Delhi India rather than California.
#: Logarithmic so 24k beats 4k clearly while a 200k locality does not swamp the
#: rest of the model.
AMBIGUITY_PRIOR_WEIGHT = 0.02

#: Per token of the matched name. A longer name is better evidence, so
#: CARRUM DOWNS outranks the CARRUM inside it and ST KILDA EAST outranks
#: ST KILDA.
NGRAM_TOKEN_BONUS = 0.15

#: Postal-only localities have no G-NAF addresses at all, so against a real
#: suburb of the same name they lose.
POSTAL_ONLY_PENALTY = -0.60

#: Unless a PO box was found, in which case the postal-only row is precisely
#: what was asked for. Big enough to clear the ambiguity prior of a large
#: same-named suburb: RYDE NSW/1680 must beat RYDE NSW/2112 with its 19,853
#: addresses once 'PO BOX 45' is on the input.
POSTAL_ONLY_WITH_BOX_BONUS = 1.40

#: Alias trust, from locatron_locality_alias.confidence. Applied as a deduction
#: from parity -- zero for a direct hit, negative for a 0.80 auspost_variant --
#: so the breakdown stays additive and still sums to the score.
ALIAS_TRUST_WEIGHT = 0.50

# --- turning a score into a confidence --------------------------------------

#: What a clean, fully corroborated two-token hit scores: exact name, agreeing
#: postcode, stated state. Used only to map scores onto 0..1 so confidence means
#: something absolute rather than only relative to the runner-up.
SCORE_FULL = BASE_EXACT + POSTCODE_AGREE + STATE_AGREE + NGRAM_TOKEN_BONUS * 2

#: Most that ambiguity can take off. Capped below 1.0 because a genuine tie
#: between two real localities is still a resolution, just an uncertain one, and
#: the caller needs a number it can threshold rather than a zero.
AMBIGUITY_PENALTY_MAX = 0.45


@dataclass(frozen=True, slots=True)
class Weights:
    """Every weight in one place, defaulting to the constants above.

    Exists so the model can later be driven from `Settings` -- see the module
    docstring -- without touching the functions that use it.
    """

    base_exact: float = BASE_EXACT
    base_alias: float = BASE_ALIAS
    base_fuzzy_max: float = BASE_FUZZY_MAX
    postcode_agree: float = POSTCODE_AGREE
    postcode_disagree: float = POSTCODE_DISAGREE
    postcode_unexplained: float = POSTCODE_UNEXPLAINED
    state_agree: float = STATE_AGREE
    state_disagree: float = STATE_DISAGREE
    state_hint_agree: float = STATE_HINT_AGREE
    ambiguity_prior_weight: float = AMBIGUITY_PRIOR_WEIGHT
    ngram_token_bonus: float = NGRAM_TOKEN_BONUS
    postal_only_penalty: float = POSTAL_ONLY_PENALTY
    postal_only_with_box_bonus: float = POSTAL_ONLY_WITH_BOX_BONUS
    alias_trust_weight: float = ALIAS_TRUST_WEIGHT
    score_full: float = SCORE_FULL
    ambiguity_penalty_max: float = AMBIGUITY_PENALTY_MAX
    name_similarity_min: float = 58.0
    type_mismatch_penalty: float = -0.30
    street_match_weight: float = 1.20
    unexplained_token_penalty: float = -0.60


DEFAULT_WEIGHTS = Weights()


@dataclass(frozen=True, slots=True)
class Signals:
    """A scored hypothesis: the total, and every named contribution to it."""

    score: float
    parts: dict[str, float] = field(default_factory=dict)

    def sums(self) -> bool:
        """Whether the parts actually add up. The breakdown is only useful if
        it is the score, not a commentary alongside it."""
        return math.isclose(self.score, sum(self.parts.values()), abs_tol=1e-9)


def base_for(match: str, fuzzy_ratio: float, w: Weights = DEFAULT_WEIGHTS) -> float:
    """Starting score for a match reached this way."""
    if match == MatchKind.EXACT:
        return w.base_exact
    if match == MatchKind.ALIAS:
        return w.base_alias
    return w.base_fuzzy_max * fuzzy_ratio


def ambiguity_prior(address_count: int, w: Weights = DEFAULT_WEIGHTS) -> float:
    """log(address_count), weighted. Zero for a locality with no addresses."""
    return w.ambiguity_prior_weight * math.log1p(max(address_count, 0))


def score_candidate(
    *,
    match: str,
    fuzzy_ratio: float,
    ngram_tokens: int,
    address_count: int,
    is_postal_only: bool,
    alias_confidence: float,
    postcode_agrees: bool,
    postcode_token_present: bool,
    postcode_unexplained: bool,
    stated_state: str | None,
    hinted_state: str | None,
    candidate_state: str,
    po_box_found: bool,
    w: Weights = DEFAULT_WEIGHTS,
) -> Signals:
    """Score one candidate, returning the total and the breakdown.

    Every argument is a plain value rather than a Candidate, so the whole model
    is exercisable without a gazetteer and a weight can be checked by hand.
    """
    parts: dict[str, float] = {"base": base_for(match, fuzzy_ratio, w)}

    if ngram_tokens:
        parts["ngram_length"] = w.ngram_token_bonus * ngram_tokens

    if postcode_agrees:
        parts["postcode_agree"] = w.postcode_agree
    elif postcode_token_present:
        parts["postcode_disagree"] = w.postcode_disagree
    if postcode_unexplained:
        parts["postcode_unexplained"] = w.postcode_unexplained

    if stated_state is not None:
        key = "state_agree" if stated_state == candidate_state else "state_disagree"
        parts[key] = w.state_agree if stated_state == candidate_state else w.state_disagree
    elif hinted_state is not None and hinted_state == candidate_state:
        parts["state_hint_agree"] = w.state_hint_agree

    prior = ambiguity_prior(address_count, w)
    if prior:
        parts["ambiguity_prior"] = prior

    if is_postal_only:
        parts["postal_only"] = (
            w.postal_only_with_box_bonus if po_box_found else w.postal_only_penalty
        )

    if alias_confidence != 1.0:
        parts["alias_trust"] = w.alias_trust_weight * (alias_confidence - 1.0)

    return Signals(score=sum(parts.values()), parts=parts)


def confidence(score: float, runner_up: float | None, w: Weights = DEFAULT_WEIGHTS) -> float:
    """Map a winning score and its margin onto 0..1.

    Two independent reductions: how much of a fully corroborated score this
    reached, and how close the runner-up was. A lone candidate is not
    automatically certain -- a bare locality name with nothing corroborating it
    still scores well under SCORE_FULL.
    """
    absolute = max(0.0, min(1.0, score / w.score_full)) if w.score_full else 0.0
    if runner_up is None or score <= 0.0:
        return absolute

    margin = max(0.0, (score - runner_up) / score)
    return absolute * (1.0 - w.ambiguity_penalty_max * (1.0 - min(margin, 1.0)))


# ---------------------------------------------------------------------------
# street matching
# ---------------------------------------------------------------------------

#: Levenshtein similarity a street NAME must reach, 0-100, after the type has
#: been settled by the table rather than by similarity.
#:
#: Set from the near-miss table. The tightest true positive is a two-character
#: transposition in a short name -- 'SMTIH ST' against SMITH ST scores 60.00,
#: because Levenshtein counts both moved characters in a five-letter name. The
#: closest false positive on the same data is 'CLIFTON STREET' reaching
#: CLIFTON GR at 75.00 on its name alone, which is a type mismatch and handled
#: by TYPE_MISMATCH_PENALTY rather than by this threshold.
#:
#: 58 keeps the transposition and still rejects unrelated names, which cluster
#: in the 30s and 40s. It is deliberately low because the type carries most of
#: the discrimination: the cost of a loose name threshold is a penalised
#: candidate the joint score can discard, while the cost of a tight one is a
#: misspelled street silently becoming locality-only.
NAME_SIMILARITY_MIN = 58.0

#: Applied when the name matched but the type did not, and no same-name street
#: in that locality carries the input's type. 'Richmond Road' in Carrum Downs
#: reaches RICHMOND AV this way.
#:
#: Bounded from both sides by real cases. It must be small enough that an exact
#: name with the wrong type beats a poor name with no type: 'CLIFTON STREET'
#: should reach CLIFTON GR (name 100.00) rather than CLIFTON PARK DR (64.29), so
#: the penalty has to stay under 0.36. It must be large enough that an exact-type
#: match is never displaced by a mismatch on a marginally better name. 0.30 sits
#: in that band with room either side.
TYPE_MISMATCH_PENALTY = -0.30


#: How many locality hypotheses get street matching. Not only the top one: a
#: weaker locality whose streets contain the input must be able to overtake a
#: stronger one whose streets do not. Three covers the homonym cases the golden
#: set probes without turning one resolve into a sweep of every Richmond.
STREET_HYPOTHESES = 3

#: Weight on a street match's own 0-1 score. Large enough that a confirmed street
#: can overturn a locality ordering -- which is the whole reason for matching
#: against three hypotheses -- and not so large that it outweighs an agreeing
#: postcode plus a stated state.
STREET_MATCH_WEIGHT = 1.20

#: Per token that no stage explained: not a number, unit, postcode, PO box,
#: state, locality or street.
#:
#: This is what makes CARRUM DOWNS beat CARRUM on 'Carrum Downs VIC'. Without it
#: the two differ only by one n-gram token bonus, 0.15, because both are real
#: localities and both agree with the state. With it, reading CARRUM leaves DOWNS
#: explained by nothing -- Carrum has no street called DOWNS -- and the gap
#: becomes wide enough to be safe.
#:
#: Sized above NGRAM_TOKEN_BONUS so a longer locality name always beats a
#: shorter one plus a loose end, and below BASE_EXACT so a single stray token
#: cannot annihilate an otherwise good parse.
UNEXPLAINED_TOKEN_PENALTY = -0.60
