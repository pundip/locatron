"""Tokenisation tests.

Two things matter here. Offsets must land on the normalised string exactly, or
every span a later stage reports is off by something. And tokenisation must
stay delegated to `locatron.normalize` — the assertions below compare against
`normalize()` directly rather than restating what it does, so a change there
shows up as a failure here instead of a silent divergence.
"""

from __future__ import annotations

import pytest

from locatron.normalize import normalize
from locatron.parse.tokens import Span, Token, tokenize

# ---------------------------------------------------------------------------
# Span
# ---------------------------------------------------------------------------


def test_span_length_and_indices() -> None:
    s = Span(2, 5)
    assert len(s) == 3
    assert s.indices == (2, 3, 4)
    assert list(s) == [2, 3, 4]


def test_empty_span_is_representable() -> None:
    s = Span(3, 3)
    assert len(s) == 0
    assert s.indices == ()


@pytest.mark.parametrize(("start", "end"), [(-1, 0), (2, 1), (0, -1)])
def test_invalid_spans_are_rejected(start: int, end: int) -> None:
    with pytest.raises(ValueError):
        Span(start, end)


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (Span(0, 2), Span(1, 3), True),
        (Span(0, 2), Span(2, 4), False),  # half-open: touching is not overlapping
        (Span(2, 4), Span(0, 2), False),
        (Span(0, 5), Span(1, 2), True),
        (Span(1, 2), Span(0, 5), True),
        (Span(0, 1), Span(0, 1), True),
        (Span(0, 0), Span(0, 1), False),  # empty span claims nothing
    ],
)
def test_span_overlap(a: Span, b: Span, expected: bool) -> None:
    assert a.overlaps(b) is expected
    assert b.overlaps(a) is expected


# ---------------------------------------------------------------------------
# tokenize
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("65 Clifton Park Dr", ("65", "CLIFTON", "PARK", "DR")),
        ("  St Kilda   East ", ("ST", "KILDA", "EAST")),
        ("5/12 Smith Street", ("5/12", "SMITH", "STREET")),
        ("14-40 Wills Street", ("14-40", "WILLS", "STREET")),
        ("Ku-ring-gai NSW", ("KU-RING-GAI", "NSW")),
        # normalize() expands '&', so the token count grows.
        ("Ryde & Eastwood", ("RYDE", "AND", "EASTWOOD")),
        # Periods become separators, which is why 'P.O.' arrives as two tokens.
        ("P.O. BOX 45", ("P", "O", "BOX", "45")),
        ("PO Box 45", ("PO", "BOX", "45")),
        # A lone slash survives as its own token.
        ("Remote / Work from home", ("REMOTE", "/", "WORK", "FROM", "HOME")),
        ("Zürich", ("ZURICH",)),
    ],
)
def test_texts(raw: str, expected: tuple[str, ...]) -> None:
    assert tokenize(raw).texts == expected


@pytest.mark.parametrize("raw", ["", "   ", "!!!", None])
def test_empty_and_punctuation_only_yield_no_tokens(raw: str | None) -> None:
    ts = tokenize(raw)
    assert ts.items == ()
    assert len(ts) == 0
    assert ts.texts == ()
    assert ts.runs([]) == ()


def test_raw_is_preserved_and_none_becomes_empty() -> None:
    assert tokenize("65 Smith St").raw == "65 Smith St"
    assert tokenize(None).raw == ""


def test_norm_matches_normalize_exactly() -> None:
    """The contract: this module adds nothing to normalisation."""
    for raw in ["65 Clifton Park Dr", "Ryde & Eastwood", "Zürich", "P.O. BOX 45", ""]:
        assert tokenize(raw).norm == normalize(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "65 Clifton Park Dr Carrum Downs VIC 3201",
        "5/12 Smith Street Fitzroy VIC 3065",
        "Ryde & Eastwood",
        "Ku-ring-gai NSW",
        "P.O. BOX 45 World Square NSW 2002",
    ],
)
def test_offsets_index_the_normalised_string(raw: str) -> None:
    """Every token's [start:end] must slice its own text out of `norm`."""
    ts = tokenize(raw)
    for t in ts:
        assert ts.norm[t.start : t.end] == t.text, t


def test_offsets_are_monotonic_with_single_separators() -> None:
    ts = tokenize("65 Clifton Park Dr")
    assert [(t.start, t.end) for t in ts] == [(0, 2), (3, 10), (11, 15), (16, 18)]


def test_indices_are_dense_and_ordered() -> None:
    ts = tokenize("one two three four")
    assert [t.index for t in ts] == [0, 1, 2, 3]


def test_token_span_covers_only_itself() -> None:
    ts = tokenize("65 SMITH ST")
    assert ts[1].span == Span(1, 2)
    assert ts.text_of(ts[1].span) == "SMITH"


@pytest.mark.parametrize(
    ("text", "expected"),
    [("65", True), ("6C", False), ("14-40", False), ("5/12", False), ("SMITH", False)],
)
def test_is_digits(text: str, expected: bool) -> None:
    assert Token(0, text, 0, len(text)).is_digits is expected


# ---------------------------------------------------------------------------
# navigation
# ---------------------------------------------------------------------------


def test_at_returns_none_out_of_range() -> None:
    ts = tokenize("UNIT 5")
    assert ts.at(0) is not None and ts.at(0).text == "UNIT"
    assert ts.at(1) is not None and ts.at(1).text == "5"
    assert ts.at(2) is None
    assert ts.at(-1) is None


def test_slice_and_text_of() -> None:
    ts = tokenize("65 CLIFTON PARK DRIVE CARRUM DOWNS")
    assert ts.text_of(Span(1, 4)) == "CLIFTON PARK DRIVE"
    assert tuple(t.text for t in ts.slice(Span(4, 6))) == ("CARRUM", "DOWNS")
    assert ts.text_of(Span(0, 0)) == ""


def test_text_of_a_span_is_a_substring_of_norm() -> None:
    """Rejoining with single spaces has to reproduce the original run."""
    ts = tokenize("65 Clifton Park Drive 3201 Carrum Downs")
    assert ts.text_of(Span(1, 4)) in ts.norm
    assert ts.text_of(Span(0, len(ts))) == ts.norm


# ---------------------------------------------------------------------------
# remaining / runs — how the street falls out
# ---------------------------------------------------------------------------


def test_remaining_excludes_claimed_tokens() -> None:
    ts = tokenize("65 CLIFTON PARK DRIVE 3201 CARRUM DOWNS")
    left = ts.remaining([Span(0, 1), Span(4, 5)])  # number and postcode claimed
    assert tuple(t.text for t in left) == ("CLIFTON", "PARK", "DRIVE", "CARRUM", "DOWNS")


def test_remaining_with_no_spans_is_everything() -> None:
    ts = tokenize("CLIFTON PARK DRIVE")
    assert ts.remaining([]) == ts.items


def test_remaining_with_everything_claimed_is_empty() -> None:
    ts = tokenize("CLIFTON PARK DRIVE")
    assert ts.remaining([Span(0, 3)]) == ()


def test_runs_groups_contiguous_leftovers() -> None:
    """The worked example: number and postcode claimed, locality claimed,
    leaving the street as one run rather than loose tokens."""
    ts = tokenize("65 CLIFTON PARK DRIVE 3201 CARRUM DOWNS")
    runs = ts.runs([Span(0, 1), Span(4, 5), Span(5, 7)])
    assert runs == (Span(1, 4),)
    assert ts.text_of(runs[0]) == "CLIFTON PARK DRIVE"


def test_runs_splits_where_a_claim_interrupts() -> None:
    ts = tokenize("65 SMITH ST 3000 MELBOURNE")
    runs = ts.runs([Span(3, 4)])  # only the postcode claimed, mid-string
    assert runs == (Span(0, 3), Span(4, 5))


def test_runs_handles_claims_at_both_ends() -> None:
    ts = tokenize("65 SMITH ST VIC")
    assert ts.runs([Span(0, 1), Span(3, 4)]) == (Span(1, 3),)


def test_runs_with_nothing_claimed_is_one_run() -> None:
    ts = tokenize("HAMILTON CRESCENT RYDE")
    assert ts.runs([]) == (Span(0, 3),)


def test_runs_ignores_overlapping_claims() -> None:
    """Extractors may overlap, so runs() must tolerate it rather than
    double-count."""
    ts = tokenize("5/12 SMITH ST")
    assert ts.runs([Span(0, 1), Span(0, 1)]) == (Span(1, 3),)


def test_runs_and_remaining_agree() -> None:
    ts = tokenize("65 SMITH ST 3000 MELBOURNE VIC")
    spans = [Span(0, 1), Span(3, 4)]
    from_runs = [t.text for s in ts.runs(spans) for t in ts.slice(s)]
    assert from_runs == [t.text for t in ts.remaining(spans)]
