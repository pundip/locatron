"""Address component extractors. Pure functions over a `TokenStream`.

No database, no gazetteer, no I/O. Every function here proposes candidates and
validates nothing, because validation needs the gazetteer and that belongs to a
later stage. The parser generates hypotheses and scores them against real data;
this module's job is to generate them generously and say exactly which tokens
each one consumed.

Two consequences of that split are deliberate and worth stating up front:

Candidates overlap. '5/12' is a unit *and* a street number, and '3201' is a
postcode *and* a plausible street number. Both readings are returned and the
scoring stage picks. Suppressing one here would silently decide the very thing
the hypothesis scorer exists to decide.

Nothing is rejected for being implausible. A four-digit token is a postcode
candidate whether or not 9999 is a real postcode, because the check against
`AustralianPostcodes` happens where that table is in scope.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from locatron.parse.tokens import Span, TokenStream

# A street-number-shaped atom: digits with an optional single alpha suffix.
# G-NAF keeps the suffix inside NUMBER_FIRST ('59B', with NUMBER_LAST blank),
# so it is one atom here too rather than a number plus a separate suffix field.
_NUMBER = r"[0-9]{1,6}[A-Z]?"

_POSTCODE_RE = re.compile(r"^[0-9]{4}$")
#: Three digits, which is what a postcode looks like after a careless load has
#: dropped its leading zero. NT is 0800-0899, so the whole territory is exposed
#: to it. See `PostcodeCandidate.padded`.
_SHORT_POSTCODE_RE = re.compile(r"^[0-9]{3}$")
_BOX_NUMBER_RE = re.compile(rf"^{_NUMBER}$")


@dataclass(frozen=True, slots=True)
class PostcodeCandidate:
    """A token that could be a postcode. Unvalidated."""

    postcode: str
    """Always four characters. Never an int: 0800 must not become 800."""
    span: Span
    padded: bool = False
    """True when recovered from a three-digit token by left-padding a zero.

    Separate from the rest so a caller can ignore these entirely. They are
    genuinely ambiguous -- the 800 in 'Level 3 800 Bourke St' pads to 0800,
    which is a real Darwin postcode -- so a scorer should weight them well below
    a four-digit hit rather than treat them alike.
    """


@dataclass(frozen=True, slots=True)
class PoBox:
    """A postal box. Never matches G-NAF; resolves via is_postal_only = 1."""

    kind: str
    """'PO' or 'GPO'."""
    number: str
    span: Span


def find_postcodes(ts: TokenStream) -> tuple[PostcodeCandidate, ...]:
    """Every token that could be a postcode, in token order.

    Returns all of them. '65 CLIFTON PARK DRIVE 3201 CARRUM DOWNS' and
    '65 CLIFTON PARK DR CARRUM DOWNS VIC 3201' both yield 3201 from different
    positions, which is the point: the parser is not positional.

    >>> from locatron.parse.tokens import tokenize
    >>> [c.postcode for c in find_postcodes(tokenize("Darwin NT 0800"))]
    ['0800']
    """
    out: list[PostcodeCandidate] = []
    for token in ts:
        if _POSTCODE_RE.match(token.text):
            out.append(PostcodeCandidate(postcode=token.text, span=token.span))
        elif _SHORT_POSTCODE_RE.match(token.text):
            out.append(
                PostcodeCandidate(postcode=token.text.rjust(4, "0"), span=token.span, padded=True)
            )
    return tuple(out)


#: Token sequences that introduce a postal box, longest first so 'G P O BOX'
#: is not consumed as a bare 'O BOX'. The split forms are not alternatives to
#: the joined ones, they are what normalize() produces: 'P.O. BOX' has its
#: periods turned into separators and arrives as ['P', 'O', 'BOX'].
_BOX_PREFIXES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("G", "P", "O", "BOX"), "GPO"),
    (("P", "O", "BOX"), "PO"),
    (("GPO", "BOX"), "GPO"),
    (("PO", "BOX"), "PO"),
    (("GPOBOX",), "GPO"),
    (("POBOX",), "PO"),
)


def find_po_boxes(ts: TokenStream) -> tuple[PoBox, ...]:
    """Every postal box in the stream, in token order.

    >>> from locatron.parse.tokens import tokenize
    >>> b = find_po_boxes(tokenize("P.O. Box 45 World Square NSW 2002"))[0]
    >>> (b.kind, b.number, b.span.start, b.span.end)
    ('PO', '45', 0, 4)
    """
    out: list[PoBox] = []
    i = 0
    while i < len(ts):
        for prefix, kind in _BOX_PREFIXES:
            end = i + len(prefix)
            if ts.texts[i:end] != prefix:
                continue
            number = ts.at(end)
            if number is None or not _BOX_NUMBER_RE.match(number.text):
                continue
            out.append(PoBox(kind=kind, number=number.text, span=Span(i, end + 1)))
            i = end  # the loop's own increment steps past the number
            break
        i += 1
    return tuple(out)


# ---------------------------------------------------------------------------
# unit and level
# ---------------------------------------------------------------------------

#: Keywords that introduce a sub-dwelling, mapping to the G-NAF FLAT_TYPE they
#: correspond to. Deliberately short: exactly the forms the golden set uses.
#: G-NAF's FLAT_TYPE vocabulary is far wider -- APARTMENT, SUITE, OFFICE,
#: PENTHOUSE, TOWER and about thirty more -- and adding one is a line here. But
#: every keyword added is a token taken away from the street name, and a
#: single-letter one like F costs more than it earns, so entries are added on
#: evidence from locatron_unresolved rather than on guesswork.
_UNIT_WORDS: dict[str, str] = {
    "UNIT": "UNIT",
    "U": "UNIT",
    "FLAT": "FLAT",
    "SHOP": "SHOP",
}

#: Keywords that introduce a level, mapping to the G-NAF LEVEL_TYPE.
_LEVEL_WORDS: dict[str, str] = {
    "LEVEL": "LEVEL",
    "L": "LEVEL",
}

#: A unit value: digits with an optional alpha suffix, or a letter-led form like
#: G01 that appears on ground-floor units.
_UNIT_VALUE_RE = re.compile(rf"^(?:{_NUMBER}|[A-Z][0-9]{{1,4}})$")

#: The slash form. '5/12' is a unit and a street number in one token, which is
#: the ordinary Australian way of writing it. The right-hand side is left as
#: text because it may itself be a range: '1/14-40'.
_SLASH_RE = re.compile(rf"^({_NUMBER}|[A-Z][0-9]{{1,4}})/(.+)$")


@dataclass(frozen=True, slots=True)
class UnitLevel:
    """A unit or a level, and the tokens it consumed."""

    kind: str
    """'unit' or 'level'."""
    value: str
    """As written, suffix included. '5', '12A', 'G01'."""
    span: Span
    keyword: str | None = None
    """The G-NAF type the keyword maps to: 'UNIT', 'FLAT', 'LEVEL'. None for the
    slash form, which states a unit without naming its type."""
    street_number_hint: str | None = None
    """For '5/12', the '12'. The street number extractor proposes this token
    independently; this field exists so a caller that has already consumed the
    unit does not have to re-split the token to find the number."""


def find_units_and_levels(ts: TokenStream) -> tuple[UnitLevel, ...]:
    """Every unit and level candidate, in token order.

    Three shapes, all of which appear in real input:

    >>> from locatron.parse.tokens import tokenize
    >>> [(u.kind, u.value, u.street_number_hint)
    ...  for u in find_units_and_levels(tokenize("5/12 Smith Street"))]
    [('unit', '5', '12')]
    >>> [(u.kind, u.value, u.keyword)
    ...  for u in find_units_and_levels(tokenize("Unit 5 12 Smith Street"))]
    [('unit', '5', 'UNIT')]
    >>> [(u.kind, u.value) for u in find_units_and_levels(tokenize("Level 3 Shop 2"))]
    [('level', '3'), ('unit', '2')]
    """
    out: list[UnitLevel] = []

    for token in ts:
        # The slash form stands alone: one token carries both numbers.
        slash = _SLASH_RE.match(token.text)
        if slash:
            out.append(
                UnitLevel(
                    kind="unit",
                    value=slash.group(1),
                    span=token.span,
                    street_number_hint=slash.group(2),
                )
            )
            continue

        # A keyword claims the token after it, if that token looks like a value.
        kind, words = (
            ("unit", _UNIT_WORDS) if token.text in _UNIT_WORDS else ("level", _LEVEL_WORDS)
        )
        if token.text not in words:
            continue
        nxt = ts.at(token.index + 1)
        if nxt is None:
            continue

        # 'UNIT 5/12' states the type and then the slash form. Take the unit
        # from the left of the slash and keep the hint, rather than rejecting it.
        nested = _SLASH_RE.match(nxt.text)
        if nested:
            out.append(
                UnitLevel(
                    kind=kind,
                    value=nested.group(1),
                    span=Span(token.index, nxt.index + 1),
                    keyword=words[token.text],
                    street_number_hint=nested.group(2),
                )
            )
        elif _UNIT_VALUE_RE.match(nxt.text):
            out.append(
                UnitLevel(
                    kind=kind,
                    value=nxt.text,
                    span=Span(token.index, nxt.index + 1),
                    keyword=words[token.text],
                )
            )

    return tuple(out)


# ---------------------------------------------------------------------------
# street number
# ---------------------------------------------------------------------------

#: A single number, suffix included. G-NAF stores '59B' in NUMBER_FIRST with
#: NUMBER_LAST blank, so the suffix belongs inside number_first here too rather
#: than in a field of its own.
_SINGLE_RE = re.compile(rf"^({_NUMBER})$")

#: A range: '14-40', and with suffixes '1A-1C'. Both sides must be
#: number-shaped, which is what keeps 'KU-RING-GAI' out. Hyphens survive
#: normalisation, so a hyphenated locality reaches this function intact and a
#: looser pattern would eat it.
_RANGE_RE = re.compile(rf"^({_NUMBER})-({_NUMBER})$")


@dataclass(frozen=True, slots=True)
class StreetNumber:
    """A street number candidate, shaped the way address_ref stores it."""

    number_first: str
    """Suffix included: '65', '6C'. Maps to NUMBER_FIRST."""
    span: Span
    number_last: str | None = None
    """Set only for a range. Maps to NUMBER_LAST, which is blank otherwise."""
    from_slash: bool = False
    """True when taken from the right of a slash, as the 12 in '5/12'. The unit
    extractor proposes the same token, so a caller that consumes both must
    expect one token to satisfy two components."""

    @property
    def is_range(self) -> bool:
        return self.number_last is not None


def _parse_number(text: str) -> tuple[str, str | None] | None:
    """(number_first, number_last) for a number-shaped token, else None."""
    single = _SINGLE_RE.match(text)
    if single:
        return single.group(1), None
    rng = _RANGE_RE.match(text)
    if rng:
        return rng.group(1), rng.group(2)
    return None


def find_street_numbers(ts: TokenStream) -> tuple[StreetNumber, ...]:
    """Every street number candidate, in token order.

    Unvalidated, like the rest: a four-digit token is both a postcode and a
    plausible street number, and '3201' on its own is a golden row that means
    the postcode. Deciding needs the gazetteer.

    >>> from locatron.parse.tokens import tokenize
    >>> [(n.number_first, n.number_last) for n in find_street_numbers(tokenize("65 Smith St"))]
    [('65', None)]
    >>> [(n.number_first, n.number_last)
    ...  for n in find_street_numbers(tokenize("14-40 Wills Street"))]
    [('14', '40')]
    >>> [(n.number_first, n.number_last) for n in find_street_numbers(tokenize("6C Smith St"))]
    [('6C', None)]
    """
    out: list[StreetNumber] = []

    for token in ts:
        parsed = _parse_number(token.text)
        if parsed is not None:
            first, last = parsed
            out.append(StreetNumber(number_first=first, span=token.span, number_last=last))
            continue

        # '5/12' and '1/14-40': the street number is whatever is right of the
        # slash. Reported against the same token the unit came from.
        slash = _SLASH_RE.match(token.text)
        if slash:
            parsed = _parse_number(slash.group(2))
            if parsed is not None:
                first, last = parsed
                out.append(
                    StreetNumber(
                        number_first=first,
                        span=token.span,
                        number_last=last,
                        from_slash=True,
                    )
                )

    return tuple(out)
