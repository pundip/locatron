"""Confidence assembly.

Pure functions over a `Settings`, so the whole scoring model is testable without
a database and tunable on the box without a redeploy.

A score is built from four parts:

    base        how the name was matched: exact, alias, qualifier-stripped,
                or fuzzy. An exact canonical hit must always start above the
                same name reached any other way.
    bonuses     evidence the input itself supplied — a country, a state, a
                postcode. Stating more makes the answer more certain.
    penalty     ambiguity, measured against the runner-up rather than by
                counting candidates. Two Delhis are not ambiguous when one is
                three thousand times the size of the other; six Springfields
                are, because the second is two thirds of the first.
    multiplier  alias trust, straight from locatron_locality_alias.confidence.

Counting candidates is the tempting way to measure ambiguity and it is wrong.
It makes Delhi (2 rows) look more doubtful than a unique obscure suburb, and it
cannot tell a genuine coin-flip from a landslide.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from locatron.config import Settings
from locatron.schemas import Candidate, Granularity, MatchMethod


class MatchKind:
    """How the gazetteer name was reached. Selects the base score."""

    EXACT = "exact"
    ALIAS = "alias"
    QUALIFIER_STRIPPED = "qualifier_stripped"
    FUZZY = "fuzzy"


def base_score(kind: str, s: Settings, *, fuzzy_ratio: float = 0.0) -> float:
    """Starting score for a match of this kind.

    A fuzzy match scales with how close it was, up to `score_fuzzy_max`, so a
    98%-similar near-miss is not treated the same as one that barely cleared
    the threshold.
    """
    if kind == MatchKind.EXACT:
        return s.score_exact
    if kind == MatchKind.ALIAS:
        return s.score_alias
    if kind == MatchKind.QUALIFIER_STRIPPED:
        return s.score_qualifier_stripped
    if kind == MatchKind.FUZZY:
        return s.score_fuzzy_max * max(0.0, min(1.0, fuzzy_ratio))
    raise ValueError(f"unknown match kind {kind!r}")


def dominance(winner: int, runner_up: int) -> float:
    """The winner's share of size against its nearest rival, in [0.5, 1.0].

    0.5 is a dead heat, 1.0 means the runner-up is negligible. Size is
    population for world cities and address_count for AU localities.

    With no runner-up the answer is unambiguous, so 1.0. With no size
    information on either side there is nothing to separate them, so 0.5 — the
    honest answer is "ambiguous", not "certain".
    """
    if runner_up <= 0:
        return 1.0
    total = winner + runner_up
    if total <= 0:
        return 0.5
    return winner / total


def ambiguity_penalty(dom: float, s: Settings) -> float:
    """Scale the ambiguity penalty by how decisively the winner won.

    Full penalty at a dead heat, tapering to nothing at
    `score_dominance_clear`. Linear, because nothing in the data suggests a
    particular curve and a straight line is easier to reason about when tuning.
    """
    clear = s.score_dominance_clear
    if dom >= clear:
        return 0.0
    span = clear - 0.5
    if span <= 0:
        return 0.0
    progress = max(0.0, (dom - 0.5) / span)
    return s.score_ambiguity_penalty_max * (1.0 - progress)


@dataclass(slots=True)
class ScoreParts:
    """An auditable breakdown. `reason` is what shows up in Candidate.reason."""

    base: float
    bonuses: dict[str, float] = field(default_factory=dict)
    penalties: dict[str, float] = field(default_factory=dict)
    multiplier: float = 1.0

    @property
    def total(self) -> float:
        score = self.base * self.multiplier
        score += sum(self.bonuses.values())
        score -= sum(self.penalties.values())
        return max(0.0, min(1.0, score))

    @property
    def reason(self) -> str:
        bits = [f"base={self.base:.2f}"]
        if self.multiplier != 1.0:
            bits.append(f"alias x{self.multiplier:.2f}")
        bits += [f"+{k} {v:.2f}" for k, v in self.bonuses.items() if v]
        bits += [f"-{k} {v:.2f}" for k, v in self.penalties.items() if v]
        return " ".join(bits)


def apply_country_bias(
    score: float, candidate_alpha3: str | None, bias_alpha3: str | None, s: Settings
) -> float:
    """Nudge a candidate from the biased country.

    Deliberately additive and tiny. This may only separate candidates that are
    already near-equal; the caller is responsible for never applying it when
    the input stated a country outright. See CLAUDE.md.
    """
    if not bias_alpha3 or candidate_alpha3 != bias_alpha3:
        return score
    return min(1.0, score + s.country_bias_weight)


def to_candidates(
    rows: list[tuple[str, float, Granularity, str | None, str | None, str | None, str]],
    *,
    winner_score: float,
    s: Settings,
    apply_margin: bool = True,
) -> list[Candidate]:
    """Build the runner-up list, keeping only what is close enough to matter.

    A candidate list that includes everything is noise. One that includes
    nothing hides the fact that the answer was a coin-flip. The cut is
    `candidate_margin` below the winner.

    `apply_margin=False` bypasses the cut for a caller that asked for
    candidates outright: "show me what else you considered" is a different
    question from "warn me when this was close", and answering the first with
    an empty list because the runner-up was distant is unhelpful.
    """
    cutoff = (winner_score - s.candidate_margin) if apply_margin else float("-inf")
    out = [
        Candidate(
            label=label,
            confidence=round(score, 4),
            granularity=gran,
            country=country,
            admin1=admin1,
            locality=locality,
            reason=reason,
        )
        for label, score, gran, country, admin1, locality, reason in rows
        if score >= cutoff
    ]
    return out[: s.candidate_max]


def unresolved_method() -> MatchMethod:
    return MatchMethod.NONE
