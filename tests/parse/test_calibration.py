"""Confidence calibration: score and margin onto 0..1, then the ceilings.

These assert relationships rather than exact numbers wherever the relationship is
the point. A weight change should be free to move 0.94 to 0.92; it must not be
free to make a street centroid as confident as a matched address.
"""

from __future__ import annotations

import pytest

from locatron.parse.lookup import (
    ALIAS_CONFIDENCE_CAP,
    NUMBER_NOT_FOUND_CONFIDENCE_CAP,
    RANGE_MISMATCH_CONFIDENCE_CAP,
    SUBSTITUTED_TYPE_CONFIDENCE_CAP,
    UNIT_NOT_FOUND_CONFIDENCE_CAP,
)
from locatron.parse.scoring import (
    SCORE_FULL,
    SCORE_FULL_WITH_STREET,
    au_confidence,
)

ALL_CAPS = (
    SUBSTITUTED_TYPE_CONFIDENCE_CAP,
    RANGE_MISMATCH_CONFIDENCE_CAP,
    UNIT_NOT_FOUND_CONFIDENCE_CAP,
    NUMBER_NOT_FOUND_CONFIDENCE_CAP,
    ALIAS_CONFIDENCE_CAP,
)


# ---------------------------------------------------------------------------
# the 0..1 mapping
# ---------------------------------------------------------------------------


def test_confidence_is_bounded_whatever_the_score() -> None:
    for score in (-10.0, 0.0, 0.5, 4.2, 100.0):
        for runner in (None, -5.0, 0.0, 4.19):
            c = au_confidence(score, runner, has_street=True)
            assert 0.0 <= c <= 1.0, (score, runner, c)


def test_a_street_answer_is_measured_against_the_street_reference() -> None:
    """Measuring an address score against the locality-only reference saturates:
    every golden address scores 3.4 to 4.2 against SCORE_FULL of 2.75, so they
    would all read 1.000 and the scale would carry no information."""
    assert SCORE_FULL_WITH_STREET > SCORE_FULL
    assert au_confidence(3.0, None, has_street=True) < au_confidence(3.0, None, has_street=False)


def test_more_corroboration_means_more_confidence() -> None:
    """'65 Clifton Park Dr Carrum Downs VIC 3201' states the postcode and the
    state; the lowercase ordering states the postcode only; 'Clifton Park Drive
    Carrum Downs' states neither. Confidence has to follow that order."""
    both = au_confidence(4.140, 0.560, has_street=True)
    postcode_only = au_confidence(3.640, 0.030, has_street=True)
    neither = au_confidence(2.740, 0.880, has_street=True)
    assert both > postcode_only > neither
    assert neither < 0.7, "an uncorroborated street parse is not a confident answer"


def test_a_thin_margin_is_visibly_low() -> None:
    """'200' pads to postcode 0200, which ANU and AUSTRALIAN NATIONAL UNIVERSITY
    share at identical scores. A dead tie is still a resolution, but a caller
    thresholding at 0.5 must not take it."""
    tie = au_confidence(0.800, 0.800, has_street=False)
    clear = au_confidence(0.800, 0.0, has_street=False)
    assert tie < clear
    assert tie < 0.25, f"a dead tie should read low, got {tie}"


def test_the_margin_only_ever_reduces() -> None:
    lone = au_confidence(2.090, None, has_street=False)
    for runner in (0.0, 1.0, 2.089):
        assert au_confidence(2.090, runner, has_street=False) <= lone


def test_a_wide_margin_is_not_penalised() -> None:
    """A runner-up pushed negative by STATE_DISAGREE is not ambiguity. 'Richmond
    VIC' scores 1.902 against -0.483, and the margin term must not dock it."""
    assert au_confidence(1.902, -0.483, has_street=False) == au_confidence(
        1.902, None, has_street=False
    )


# ---------------------------------------------------------------------------
# the ceilings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cap", ALL_CAPS)
def test_every_cap_is_a_ceiling_below_one(cap: float) -> None:
    assert 0.0 < cap < 1.0


@pytest.mark.parametrize("cap", ALL_CAPS)
def test_a_cap_bounds_even_a_perfect_parse(cap: float) -> None:
    assert au_confidence(4.2, -3.0, has_street=True, cap=cap) == cap


@pytest.mark.parametrize("cap", ALL_CAPS)
def test_a_cap_never_raises_a_low_confidence(cap: float) -> None:
    """It is a ceiling, not a target. A weak parse that also had a substituted
    type must not be promoted to 0.70 by it."""
    uncapped = au_confidence(0.9, 0.85, has_street=False)
    assert uncapped < cap, "premise: pick a case below the cap"
    assert au_confidence(0.9, 0.85, has_street=False, cap=cap) == uncapped


def test_the_caps_are_ordered_by_how_much_they_doubt() -> None:
    """The ordering is the design, so a future tweak cannot quietly make a
    missing number less alarming than an alias.

    Nothing found below the street is worst; then a stated flat that is absent;
    then a range that is not the stored one; then a street type we substituted;
    an alias is mildest, because the address is real and only its canonical id
    differs.
    """
    assert (
        NUMBER_NOT_FOUND_CONFIDENCE_CAP
        < UNIT_NOT_FOUND_CONFIDENCE_CAP
        < RANGE_MISMATCH_CONFIDENCE_CAP
        < ALIAS_CONFIDENCE_CAP
    )
    assert SUBSTITUTED_TYPE_CONFIDENCE_CAP < RANGE_MISMATCH_CONFIDENCE_CAP


def test_a_degraded_answer_cannot_outrank_a_matched_one() -> None:
    """The case that made NUMBER_NOT_FOUND necessary: '14-40 Wills Street
    Melbourne VIC 3000' parses perfectly and then finds no row at 14, so it
    returns a street centroid. It scored higher than '65 clifton park drive 3201
    carrum downs', which is a real address."""
    degraded = au_confidence(4.034, -0.155, has_street=True, cap=NUMBER_NOT_FOUND_CONFIDENCE_CAP)
    matched = au_confidence(3.640, 0.030, has_street=True)
    assert degraded < matched
