"""Locality hypothesis tests.

Two halves. The scoring model is pure, so those tests take plain values and run
without a database. The candidate lookup and hypothesis generation need the real
gazetteer, because the thing most worth testing is that the model ranks the
actual data correctly -- that Perth Tasmania really does beat Perth WA once 7300
is on the input. Those skip rather than fail when ReferenceDB is unreachable,
following tests/test_gazetteer.py.

Assertions are either about ordering or are loose bounds, so upstream growth in
address_count does not turn them red.
"""

from __future__ import annotations

import pytest

from locatron.db import mysql
from locatron.parse import scoring
from locatron.parse.components import (
    find_po_boxes,
    find_postcodes,
    find_street_numbers,
    find_units_and_levels,
)
from locatron.parse.locality import (
    MAX_NGRAM_TOKENS,
    generate_hypotheses,
    ngram_spans,
)
from locatron.parse.tokens import Span, tokenize
from locatron.resolve.scoring import MatchKind


def _db_available() -> bool:
    try:
        return bool(mysql.health().get("connected"))
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_available(), reason="ReferenceDB unreachable")


# ---------------------------------------------------------------------------
# n-gram generation — no database
# ---------------------------------------------------------------------------


def test_ngrams_are_longest_first() -> None:
    grams = ngram_spans(tokenize("Carrum Downs VIC"))
    assert [g.key for g in grams][:3] == ["CARRUM DOWNS VIC", "CARRUM DOWNS", "DOWNS VIC"]
    lengths = [len(g) for g in grams]
    assert lengths == sorted(lengths, reverse=True)


def test_ngrams_never_cross_a_consumed_token() -> None:
    """The postcode between DRIVE and CARRUM ends the run, so 'DRIVE CARRUM' is
    never offered as a locality key."""
    ts = tokenize("65 clifton park drive 3201 carrum downs")
    keys = {g.key for g in ngram_spans(ts, [Span(0, 1), Span(4, 5)])}
    assert "CARRUM DOWNS" in keys
    assert "CLIFTON PARK DRIVE" in keys
    assert not any("DRIVE CARRUM" in k for k in keys)
    assert not any("3201" in k for k in keys)


def test_ngrams_respect_the_length_cap() -> None:
    ts = tokenize("one two three four five six")
    assert max(len(g) for g in ngram_spans(ts)) == MAX_NGRAM_TOKENS
    assert max(len(g) for g in ngram_spans(ts, max_len=2)) == 2


def test_ngram_spans_are_absolute_token_indices() -> None:
    ts = tokenize("65 CLIFTON PARK DRIVE")
    gram = next(g for g in ngram_spans(ts, [Span(0, 1)]) if g.key == "CLIFTON PARK DRIVE")
    assert (gram.span.start, gram.span.end) == (1, 4)
    assert ts.text_of(gram.span) == gram.key


def test_no_ngrams_when_everything_is_consumed() -> None:
    ts = tokenize("65 SMITH")
    assert ngram_spans(ts, [Span(0, 2)]) == ()
    assert ngram_spans(tokenize("")) == ()


# ---------------------------------------------------------------------------
# scoring model — no database
# ---------------------------------------------------------------------------


def _score(**kw: object) -> scoring.Signals:
    base: dict[str, object] = {
        "match": MatchKind.EXACT,
        "fuzzy_ratio": 0.0,
        "ngram_tokens": 1,
        "address_count": 0,
        "is_postal_only": False,
        "alias_confidence": 1.0,
        "postcode_agrees": False,
        "postcode_token_present": False,
        "postcode_unexplained": False,
        "stated_state": None,
        "hinted_state": None,
        "candidate_state": "VIC",
        "po_box_found": False,
    }
    base.update(kw)
    return scoring.score_candidate(**base)  # type: ignore[arg-type]


def test_breakdown_always_sums_to_the_score() -> None:
    """The breakdown is only useful if it *is* the score."""
    for kw in [
        {},
        {"postcode_agrees": True},
        {"postcode_token_present": True},
        {"stated_state": "VIC"},
        {"stated_state": "NSW"},
        {"is_postal_only": True},
        {"is_postal_only": True, "po_box_found": True},
        {"match": MatchKind.ALIAS, "alias_confidence": 0.8},
        {"match": MatchKind.FUZZY, "fuzzy_ratio": 0.93},
        {"address_count": 24075, "postcode_unexplained": True},
    ]:
        sig = _score(**kw)
        assert sig.sums(), f"{kw} -> {sig.parts}"


def test_exact_outranks_alias_outranks_fuzzy() -> None:
    exact = _score(match=MatchKind.EXACT).score
    alias = _score(match=MatchKind.ALIAS).score
    best_fuzzy = _score(match=MatchKind.FUZZY, fuzzy_ratio=1.0).score
    assert exact > alias > best_fuzzy


def test_no_fuzzy_similarity_can_reach_an_alias_hit() -> None:
    """BASE_FUZZY_MAX sits under BASE_ALIAS, so this holds by construction."""
    alias = _score(match=MatchKind.ALIAS, alias_confidence=1.0).score
    for ratio in (0.88, 0.95, 0.99, 1.0):
        assert _score(match=MatchKind.FUZZY, fuzzy_ratio=ratio).score < alias


def test_postcode_agreement_beats_the_ambiguity_prior() -> None:
    """The 'Perth 7300' shape: a small locality whose postcode agrees must beat
    a large one whose postcode does not."""
    small_agrees = _score(address_count=2173, postcode_agrees=True, postcode_token_present=True)
    large_disagrees = _score(address_count=20735, postcode_token_present=True)
    assert small_agrees.score > large_disagrees.score


def test_unmatched_postcode_is_a_weak_negative_not_a_rejection() -> None:
    disagreeing = _score(postcode_token_present=True)
    silent = _score()
    assert disagreeing.score < silent.score
    assert disagreeing.score > 0.0, "a disagreeing postcode must not disqualify"


def test_stated_state_disagreement_is_a_strong_negative() -> None:
    agree = _score(stated_state="VIC", candidate_state="VIC")
    disagree = _score(stated_state="NSW", candidate_state="VIC")
    assert agree.score > disagree.score
    # Strong enough to overturn any plausible ambiguity prior.
    assert disagree.score < _score(address_count=0).score - scoring.AMBIGUITY_PRIOR_WEIGHT * 20


def test_state_hint_is_weaker_than_a_stated_state() -> None:
    hint = _score(hinted_state="VIC", candidate_state="VIC")
    stated = _score(stated_state="VIC", candidate_state="VIC")
    plain = _score()
    assert plain.score < hint.score < stated.score


def test_ambiguity_prior_is_logarithmic() -> None:
    a = scoring.ambiguity_prior(1_000)
    b = scoring.ambiguity_prior(10_000)
    c = scoring.ambiguity_prior(100_000)
    assert a < b < c
    # Each tenfold step adds nearly the same amount, which linear would not.
    # Not exactly the same: the prior is over log1p, so the +1 shows up here.
    assert abs((b - a) - (c - b)) < 1e-4
    assert scoring.ambiguity_prior(0) == 0.0


def test_postal_only_ranks_last_without_a_box_and_first_with_one() -> None:
    populated = _score(address_count=19853)
    postal_no_box = _score(address_count=0, is_postal_only=True)
    postal_with_box = _score(address_count=0, is_postal_only=True, po_box_found=True)
    assert postal_no_box.score < populated.score
    assert postal_with_box.score > populated.score


def test_longer_name_is_better_evidence() -> None:
    assert _score(ngram_tokens=2).score > _score(ngram_tokens=1).score


def test_low_trust_alias_scores_below_a_full_trust_one() -> None:
    assert (
        _score(match=MatchKind.ALIAS, alias_confidence=0.8).score
        < _score(match=MatchKind.ALIAS, alias_confidence=1.0).score
    )


@pytest.mark.parametrize(
    ("score", "runner_up", "expected"),
    [
        (scoring.SCORE_FULL, None, 1.0),
        (0.0, None, 0.0),
        (-1.0, None, 0.0),
        (scoring.SCORE_FULL * 2, None, 1.0),  # clamped
    ],
)
def test_confidence_bounds(score: float, runner_up: float | None, expected: float) -> None:
    assert scoring.confidence(score, runner_up) == pytest.approx(expected)


def test_confidence_falls_as_the_runner_up_closes() -> None:
    clear = scoring.confidence(2.0, 0.5)
    tight = scoring.confidence(2.0, 1.99)
    alone = scoring.confidence(2.0, None)
    assert alone >= clear > tight
    assert tight > 0.0, "a near-tie is still a resolution, just an uncertain one"


# ---------------------------------------------------------------------------
# hypotheses against the real gazetteer
# ---------------------------------------------------------------------------


def _prompt_a(ts: object) -> tuple[list[Span], bool, list[tuple[str, Span]]]:
    """Run Prompt A's extractors, as the pipeline will in a later prompt.

    A four-digit token is read as a postcode rather than a street number when
    both extractors propose it, which is the one arbitration this glue makes.
    """
    boxes = find_po_boxes(ts)  # type: ignore[arg-type]
    spans: list[Span] = [b.span for b in boxes]
    spans += [u.span for u in find_units_and_levels(ts)]  # type: ignore[arg-type]
    postcodes = [(p.postcode, p.span) for p in find_postcodes(ts) if not p.padded]  # type: ignore[arg-type]
    pc_starts = {s.start for _, s in postcodes}
    spans += [
        n.span
        for n in find_street_numbers(ts)  # type: ignore[arg-type]
        if n.span.start not in pc_starts and not any(n.span.overlaps(s) for s in spans)
    ]
    return spans, bool(boxes), postcodes


def _hyps(raw: str, **kw: object):
    from locatron.gazetteer.au import load_au

    ts = tokenize(raw)
    consumed, po_box, postcodes = _prompt_a(ts)
    hyps = generate_hypotheses(
        ts,
        load_au(),
        consumed=consumed,
        postcodes=postcodes,
        po_box_found=po_box,
        **kw,  # type: ignore[arg-type]
    )
    return ts, consumed, hyps


def _street_text(raw: str, **kw: object) -> list[str]:
    ts, consumed, hyps = _hyps(raw, **kw)
    return [ts.text_of(s) for s in hyps[0].remaining(ts, consumed)]


@needs_db
def test_postcode_before_locality() -> None:
    """The worked example. Word order carries no weight."""
    _ts, _consumed, hyps = _hyps("65 clifton park drive 3201 carrum downs")
    top = hyps[0]
    assert (top.candidate.locality, top.candidate.state, top.candidate.postcode) == (
        "CARRUM DOWNS",
        "VIC",
        "3201",
    )
    assert "postcode_agree" in top.signals
    assert _street_text("65 clifton park drive 3201 carrum downs") == ["CLIFTON PARK DRIVE"]


@needs_db
def test_conventional_ordering_reaches_the_same_locality() -> None:
    a = _hyps("65 clifton park drive 3201 carrum downs")[2][0]
    b = _hyps("65 Clifton Park Dr Carrum Downs VIC 3201")[2][0]
    assert a.candidate.locality_id == b.candidate.locality_id


@needs_db
def test_locality_plus_state_no_postcode() -> None:
    _ts, _consumed, hyps = _hyps("Carrum Downs VIC")
    top = hyps[0]
    assert (top.candidate.locality, top.candidate.state) == ("CARRUM DOWNS", "VIC")
    assert "state_agree" in top.signals
    assert "postcode_agree" not in top.signals
    assert "postcode_disagree" not in top.signals


@needs_db
def test_bare_postcode_resolves_to_its_locality() -> None:
    _ts, _consumed, hyps = _hyps("3201")
    assert hyps, "a bare postcode must resolve"
    top = hyps[0]
    assert top.candidate.postcode == "3201"
    assert top.candidate.locality == "CARRUM DOWNS"
    assert "postcode_agree" in top.signals
    # Nothing was named, so no name tokens were consumed.
    assert len(top.locality_span) == 0


@needs_db
def test_ambiguous_locality_returns_every_candidate_ranked_by_size() -> None:
    _ts, _consumed, hyps = _hyps("Richmond")
    assert len(hyps) >= 5, "Richmond is a real place in several states"
    assert all(h.candidate.locality == "RICHMOND" for h in hyps)
    counts = [h.candidate.address_count for h in hyps]
    assert counts == sorted(counts, reverse=True), "ranked by the ambiguity prior"
    assert hyps[0].candidate.state == "VIC", "Melbourne's Richmond is the biggest"


@needs_db
def test_ambiguous_confidence_is_visibly_lower_than_an_unambiguous_one() -> None:
    ambiguous = _hyps("Richmond")[2][0].confidence
    narrowed = _hyps("Richmond VIC")[2][0].confidence
    corroborated = _hyps("3201")[2][0].confidence
    assert ambiguous < narrowed
    assert ambiguous < corroborated
    assert narrowed - ambiguous > 0.2, f"{ambiguous:.3f} vs {narrowed:.3f} is not visible enough"


@needs_db
def test_state_token_narrows_to_one() -> None:
    _ts, _consumed, hyps = _hyps("Richmond VIC")
    assert hyps[0].candidate.state == "VIC"
    assert "state_agree" in hyps[0].signals
    # The others survive as candidates but are pushed well down, not filtered.
    assert all("state_disagree" in h.signals for h in hyps[1:])
    assert hyps[0].score - hyps[1].score > 1.0


@needs_db
def test_street_tokens_are_left_for_the_street_stage() -> None:
    _ts, _consumed, hyps = _hyps("Hamilton Crescent Ryde NSW 2112")
    top = hyps[0]
    assert (top.candidate.locality, top.candidate.state, top.candidate.postcode) == (
        "RYDE",
        "NSW",
        "2112",
    )
    assert _street_text("Hamilton Crescent Ryde NSW 2112") == ["HAMILTON CRESCENT"]


@needs_db
def test_hamilton_does_not_win_over_ryde() -> None:
    """HAMILTON is a real locality, so it competes. The postcode settles it."""
    _ts, _consumed, hyps = _hyps("Hamilton Crescent Ryde NSW 2112")
    localities = [h.candidate.locality for h in hyps]
    assert localities.index("RYDE") < localities.index("HAMILTON")


@needs_db
def test_po_box_puts_the_postal_only_candidate_first() -> None:
    _ts, _consumed, hyps = _hyps("PO Box 45 World Square NSW 2002")
    top = hyps[0]
    assert top.candidate.is_postal_only is True
    assert top.candidate.locality == "WORLD SQUARE"
    assert top.candidate.postcode == "2002"
    assert top.signals["postal_only"] == scoring.POSTAL_ONLY_WITH_BOX_BONUS


@needs_db
def test_postal_only_loses_without_a_po_box() -> None:
    """RYDE NSW/1680 is postal-only and must lose to RYDE NSW/2112."""
    _ts, _consumed, hyps = _hyps("Ryde NSW")
    top = hyps[0]
    assert top.candidate.is_postal_only is False
    postal = [h for h in hyps if h.candidate.is_postal_only]
    assert postal and postal[0].score < top.score


@needs_db
def test_postcode_overrides_the_address_count_prior() -> None:
    """Perth Tasmania has 2,173 addresses against Perth WA's 20,735. The
    postcode is what decides it."""
    _ts, _consumed, hyps = _hyps("Perth 7300")
    top = hyps[0]
    assert (top.candidate.state, top.candidate.postcode) == ("TAS", "7300")
    wa = next(h for h in hyps if h.candidate.state == "WA" and not h.candidate.is_postal_only)
    assert top.score > wa.score
    assert wa.candidate.address_count > top.candidate.address_count, "prior really is against us"


@needs_db
def test_perth_without_a_postcode_goes_to_wa() -> None:
    """The prior is only overridden when something overrides it."""
    _ts, _consumed, hyps = _hyps("Perth")
    assert hyps[0].candidate.state == "WA"


@needs_db
@pytest.mark.parametrize("raw", ["Darwin NT 0800", "0800", "Darwin NT 0820", "Winnellie NT 0820"])
def test_nt_postcodes_keep_their_leading_zero(raw: str) -> None:
    """NT is 0800-0899. An int anywhere in this path would give 800."""
    _ts, _consumed, hyps = _hyps(raw)
    assert hyps, raw
    top = hyps[0]
    assert top.candidate.state == "NT"
    assert top.candidate.postcode.startswith("0")
    assert len(top.candidate.postcode) == 4
    assert isinstance(top.candidate.postcode, str)
    # Every postcode token reaching the model is a padded four-character string.
    assert all(len(pc) == 4 and pc.startswith("0") for pc, _ in _prompt_a(tokenize(raw))[2])


@needs_db
@pytest.mark.parametrize("raw", ["Darwin NT 0800", "0800", "Winnellie NT 0820"])
def test_nt_postcode_agreement(raw: str) -> None:
    """'Darwin NT 0820' is deliberately absent: 0820 is Winnellie's, so DARWIN
    wins on its name with the postcode disagreeing, which is the right answer
    and not an agreement."""
    _ts, _consumed, hyps = _hyps(raw)
    assert "postcode_agree" in hyps[0].signals


@needs_db
def test_darwin_reaches_darwin_city_through_the_alias() -> None:
    """G-NAF calls postcode 0800 DARWIN CITY; AusPost calls it DARWIN. The alias
    is what bridges them, and the token is still consumed."""
    ts, consumed, hyps = _hyps("Darwin NT 0800")
    top = hyps[0]
    assert top.candidate.locality == "DARWIN CITY"
    assert top.candidate.match == MatchKind.ALIAS
    assert "alias_trust" in top.signals
    assert ts.text_of(top.locality_span) == "DARWIN"
    assert top.remaining(ts, consumed) == ()


@needs_db
def test_hypotheses_are_unique_per_locality() -> None:
    """A place reached by name and again by postcode must not become its own
    runner-up, which reads as a near-tie and halves the confidence."""
    for raw in ["65 clifton park drive 3201 carrum downs", "Ryde NSW 2112", "Perth 7300"]:
        hyps = _hyps(raw)[2]
        ids = [h.candidate.locality_id for h in hyps]
        assert len(ids) == len(set(ids)), raw


@needs_db
def test_scores_are_ordered_and_signals_sum() -> None:
    for raw in [
        "65 clifton park drive 3201 carrum downs",
        "Carrum Downs VIC",
        "Richmond",
        "Richmond VIC",
        "Hamilton Crescent Ryde NSW 2112",
        "PO Box 45 World Square NSW 2002",
        "Perth 7300",
        "Darwin NT 0800",
    ]:
        hyps = _hyps(raw)[2]
        scores = [h.score for h in hyps]
        assert scores == sorted(scores, reverse=True), raw
        for h in hyps:
            assert abs(h.score - sum(h.signals.values())) < 1e-9, (raw, h.signals)


@needs_db
def test_fuzzy_is_only_reached_when_nothing_matched_exactly() -> None:
    """A misspelling resolves, and below any exact hit."""
    _ts, _consumed, hyps = _hyps("Carum Downs", fuzzy_min=88)
    assert hyps, "a one-letter typo should still resolve"
    assert hyps[0].candidate.match == MatchKind.FUZZY
    assert hyps[0].candidate.locality == "CARRUM DOWNS"
    exact = _hyps("Carrum Downs")[2][0]
    assert hyps[0].score < exact.score


@needs_db
@pytest.mark.parametrize("raw", ["", "asdfghjkl", "Remote / Work from home"])
def test_unresolvable_input_yields_nothing_on_the_exact_path(raw: str) -> None:
    assert _hyps(raw)[2] == (), raw


@needs_db
def test_fuzzy_can_reach_a_junk_string_but_only_at_low_confidence() -> None:
    """'Remote / Work from home' fuzzy-matches HOMEBUSH through 'HOME', because
    Jaro-Winkler weights a shared prefix. Suppressing that here would be the
    wrong place: this module proposes, and the confidence is what the pipeline
    thresholds to return granularity=unresolved."""
    hyps = _hyps("Remote / Work from home", fuzzy_min=88)[2]
    assert hyps, "the fuzzy path does reach something"
    assert hyps[0].candidate.match == MatchKind.FUZZY
    assert hyps[0].confidence < 0.35, hyps[0].confidence
    # Well below any corroborated hit.
    assert hyps[0].confidence < _hyps("3201")[2][0].confidence


# ---------------------------------------------------------------------------
# the postcode-path gate
# ---------------------------------------------------------------------------
#
# The path is suppressed only when a named candidate already agrees with the
# postcode. When names matched but none of them agree, the path is the only
# thing that can reach the right locality, so it runs alongside them.


@needs_db
def test_postcode_path_runs_when_no_named_candidate_agrees() -> None:
    """'12 Clifton Street 3201': CLIFTON is the street, not the suburb. The
    CLIFTON localities all sit in other postcodes, so CARRUM DOWNS is reachable
    only through 3201 and must still win."""
    ts, consumed, hyps = _hyps("12 Clifton Street 3201")
    top = hyps[0]
    assert (top.candidate.locality, top.candidate.state, top.candidate.postcode) == (
        "CARRUM DOWNS",
        "VIC",
        "3201",
    )
    assert "postcode_agree" in top.signals
    # Reached by postcode, so it consumed no name token.
    assert len(top.locality_span) == 0

    # Every CLIFTON is present as a candidate and every one of them loses.
    cliftons = [h for h in hyps if h.candidate.locality == "CLIFTON"]
    assert cliftons, "CLIFTON is a real locality and must still be proposed"
    assert all(h.score < top.score for h in cliftons)
    assert all("postcode_disagree" in h.signals for h in cliftons)

    assert _street_text("12 Clifton Street 3201") == ["CLIFTON STREET"]
    assert ts.text_of(consumed[0]) == "12"


@needs_db
def test_common_locality_name_used_as_a_street() -> None:
    """'Richmond Road 3201': the same shape with a name that is a locality in
    five states, which is the case most likely to go wrong."""
    _ts, _consumed, hyps = _hyps("Richmond Road 3201")
    top = hyps[0]
    assert (top.candidate.locality, top.candidate.postcode) == ("CARRUM DOWNS", "3201")
    richmonds = [h for h in hyps if h.candidate.locality == "RICHMOND"]
    assert len(richmonds) >= 5
    assert all(h.score < top.score for h in richmonds)
    assert _street_text("Richmond Road 3201") == ["RICHMOND ROAD"]


@needs_db
def test_postcode_path_is_suppressed_when_a_named_candidate_agrees() -> None:
    """'Hamilton Crescent Ryde NSW 2112': RYDE itself sits in 2112, so the other
    localities in that postcode must not arrive as near-ties for it."""
    _ts, _consumed, hyps = _hyps("Hamilton Crescent Ryde NSW 2112")
    assert hyps[0].candidate.locality == "RYDE"
    localities = {h.candidate.locality for h in hyps}
    assert "PUTNEY" not in localities
    assert "DENISTONE EAST" not in localities
    # Everything proposed was reached by name, not by postcode.
    assert all(len(h.locality_span) > 0 for h in hyps)


@needs_db
def test_agreement_by_any_named_candidate_suppresses_the_path() -> None:
    """The gate asks whether *some* named candidate agrees, not the top one. On
    '65 clifton park drive 3201 carrum downs' CARRUM DOWNS agrees, so the path
    stays off even though CLIFTON localities were also named."""
    _ts, _consumed, hyps = _hyps("65 clifton park drive 3201 carrum downs")
    assert all(len(h.locality_span) > 0 for h in hyps)


@needs_db
def test_bare_postcode_still_works_with_no_names_at_all() -> None:
    """The gate must not break the case it was written around."""
    _ts, _consumed, hyps = _hyps("3201")
    assert hyps[0].candidate.locality == "CARRUM DOWNS"
    assert len(hyps[0].locality_span) == 0


@needs_db
def test_street_tokens_survive_a_postcode_path_win() -> None:
    """A postcode-path hypothesis consumes the postcode and the state, never a
    name, so the street stage still gets everything it needs."""
    for raw, expected in [
        ("12 Clifton Street 3201", ["CLIFTON STREET"]),
        ("Richmond Road 3201", ["RICHMOND ROAD"]),
        ("14-40 Wills Street 3000", ["WILLS STREET"]),
    ]:
        assert _street_text(raw) == expected, raw
